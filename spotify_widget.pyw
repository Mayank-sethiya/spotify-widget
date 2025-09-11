import tkinter as tk
from tkinter import messagebox, font
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple, List
import io
import json
import os
import sys
import asyncio
import logging
import queue
from pathlib import Path
import webbrowser

# Image + network
from PIL import Image, ImageTk, ImageDraw, ImageFilter
import requests
from collections import OrderedDict

# Spotify
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Sound
try:
    from playsound import playsound
    PLAYSOUND_AVAILABLE = True
except ImportError:
    PLAYSOUND_AVAILABLE = False


WIDGET_NAME = "Melo Box"
VERSION = "v2.2-final"

# --- OS/Platform ---
IS_WINDOWS = (os.name == "nt")

# --- Native Windows Control Setup ---
if IS_WINDOWS:
    try:
        import ctypes
        from ctypes import wintypes
        USER32 = ctypes.windll.user32
        VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB3
        KEYEVENTF_KEYUP = 0x0002
        NATIVE_CONTROLS_AVAILABLE = True
    except (ImportError, AttributeError):
        NATIVE_CONTROLS_AVAILABLE = False
else:
    NATIVE_CONTROLS_AVAILABLE = False

# --- Config Files ---
CONFIG_FILE = Path("config.json")
GEOMETRY_FILE = Path("geometry.json")
SPOTIPY_CACHE_FILE = Path(".spotipy_cache")

# --- Env flags / toggles ---
LOW_END_MODE = os.getenv("LOW_END_MODE", "0") == "1"
INITIAL_WIDGET_WIDTH, INITIAL_WIDGET_HEIGHT = 250, 250
MIN_WIDGET_SIZE, MAX_WIDGET_SIZE = 180, 800
CORNER_RADIUS = 24
OVERLAY_ALPHA = 0.5
FONT_NAME = "Segoe UI"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
NETWORK_TIMEOUT = 6
PROGRESS_BAR_FPS = 4 if LOW_END_MODE else 20
NETWORK_POLL_DELAY = 1500 if LOW_END_MODE else 1000
WINDOW_DECORATIONS = os.getenv("WINDOW_DECORATIONS", "0") == "1"
DISABLE_IMAGES = os.getenv("DISABLE_IMAGES", "0") == "1"

# --- Setup Logging ---
LOG_FILE = Path("widget.log")
try:
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024: LOG_FILE.unlink()
except OSError: pass

logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG", "0") == "1" else logging.INFO,
    format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)

logging.info(f"{WIDGET_NAME} {VERSION} starting up")

def resource_path(relative_path: str) -> Path:
    try: base_path = Path(sys._MEIPASS)
    except AttributeError: base_path = Path.cwd()
    return base_path / "assets" / relative_path

def format_ms(ms: int) -> str:
    """Converts milliseconds to a MM:SS format string."""
    seconds = int(ms / 1000)
    minutes = seconds // 60
    seconds %= 60
    return f"{minutes:02d}:{seconds:02d}"

class LRUCache(OrderedDict):
    def __init__(self, max_size=10):
        super().__init__()
        self.max_size = max_size
    def __setitem__(self, key, value):
        if key in self: self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.max_size: self.popitem(last=False)

def create_rounded_image(pil_image: Image.Image, size: Tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0) + size, radius, fill=255)
    output = pil_image.resize(size, Image.Resampling.LANCZOS)
    output.putalpha(mask)
    return output

@dataclass
class AppConfig:
    CLIENT_ID: str
    CLIENT_SECRET: str
    USERNAME: str
    PLAY_STARTUP_SOUND: bool = True
    OPACITY: float = 1.0

def install_tk_excepthook(root: tk.Misc):
    def handler(exc, val, tb): logging.error("Tk callback error", exc_info=(exc, val, tb))
    root.report_callback_exception = handler

class SpotifyWidget:
    def __init__(self, root: tk.Tk, config: AppConfig):
        self.root = root
        self.config = config
        self.widget_width, self.widget_height = INITIAL_WIDGET_WIDTH, INITIAL_WIDGET_HEIGHT
        self.drag_locked, self.mouse_is_over = False, False
        self._is_resizing, self._resize_start_pos, self._start_size = False, None, None
        self._resize_mode = None
        self._cmd_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._after_ids = {}
        self.mouse_pos = (0, 0)
        
        if not WINDOW_DECORATIONS: self.root.overrideredirect(True)
        self._load_geometry()
        
        self.root.config(bg="grey")
        self.root.attributes("-alpha", self.config.OPACITY)
        if IS_WINDOWS:
            try: self.root.wm_attributes("-transparentcolor", "grey")
            except tk.TclError: logging.warning("Could not enable transparency")

        self.root.attributes("-topmost", False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.canvas = tk.Canvas(root, bg="grey", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.sp: Optional[spotipy.Spotify] = None
        self.is_playing = False
        self._state_lock, self._song_state = threading.Lock(), {}
        self.tk_image_references: Dict[str, ImageTk.PhotoImage] = {}
        self._album_art_cache, self._overlay_cache = LRUCache(max_size=20), LRUCache(max_size=20)
        self._recently_played: List[Dict] = []
        self._slideshow_index = 0
        
        self._stop_event = threading.Event()
        self._bind_events()
        
        self._run_startup_animation()
        
    def _run_startup_animation(self):
        self.canvas.delete("all")
        self._draw_rounded_rect(0, 0, self.widget_width, self.widget_height, CORNER_RADIUS, "black")
        
        def simple_fade_in():
            font_size = max(12, min(self.widget_width, self.widget_height) // 10)
            self.canvas.create_text(
                self.widget_width / 2, self.widget_height / 2, 
                text=WIDGET_NAME, fill="white", font=(FONT_NAME, font_size, "bold"), tags="intro_text"
            )
            self.root.after(1200, finish_animation)

        def drop_down_animation():
            letters = list(WIDGET_NAME)
            font_size = max(12, min(self.widget_width, self.widget_height) // 10)
            intro_font = (FONT_NAME, font_size, "bold")
            
            total_width = sum(font.Font(family=FONT_NAME, size=font_size, weight="bold").measure(l) for l in letters)
            start_x = (self.widget_width - total_width) / 2
            
            letter_positions = []
            current_x = start_x
            for letter in letters:
                letter_width = font.Font(family=FONT_NAME, size=font_size, weight="bold").measure(letter)
                letter_positions.append({'char': letter, 'x': current_x, 'y': -font_size, 'final_y': self.widget_height / 2, 'id': None})
                current_x += letter_width

            rainbow_colors = ["#ff0000", "#ff7f00", "#ffff00", "#00ff00", "#0000ff", "#4b0082", "#9400d3"]
            
            def drop_letters(letter_idx=0):
                if letter_idx >= len(letter_positions):
                    self.root.after(500, finish_animation)
                    return
                pos = letter_positions[letter_idx]
                pos['id'] = self.canvas.create_text(pos['x'], pos['y'], text=pos['char'], font=intro_font, fill=rainbow_colors[0], anchor="w")
                animate_fall(letter_idx, letter_idx)
                self.root.after(80, drop_letters, letter_idx + 1)

            def animate_fall(idx, c_idx):
                pos = letter_positions[idx]
                if pos['y'] < pos['final_y']:
                    new_y = pos['y'] + 10
                    if new_y > pos['final_y']: new_y = pos['final_y']
                    self.canvas.coords(pos['id'], pos['x'], new_y)
                    pos['y'] = new_y
                    self.canvas.itemconfig(pos['id'], fill=rainbow_colors[c_idx % len(rainbow_colors)])
                    self.root.after(15, animate_fall, idx, c_idx + 1)
            
            drop_letters()

        def finish_animation():
            if self.config.PLAY_STARTUP_SOUND:
                self._play_sound()
            self.root.after(800, start_main_app)

        def start_main_app():
            self._queue_command("refresh")
            self._start_background_tasks()
            self._schedule_task("queue_consumer", 16, self._process_cmd_queue)
            self._schedule_task("progress_bar", int(1000 / PROGRESS_BAR_FPS), self._animate_progress_bar)
        
        if LOW_END_MODE: simple_fade_in()
        else: drop_down_animation()

    def _play_sound(self):
        sound_file = resource_path("startup.wav")
        if PLAYSOUND_AVAILABLE and sound_file.exists():
            try:
                threading.Thread(target=lambda: playsound(str(sound_file)), daemon=True).start()
            except Exception as e:
                logging.error(f"Could not play startup sound: {e}")
                self.root.bell()
        else:
            self.root.bell()

    def _load_geometry(self):
        try:
            geom_path = Path(GEOMETRY_FILE.name)
            if geom_path.exists():
                with open(geom_path, "r") as f: geom = json.load(f)
                w = geom.get("width", INITIAL_WIDGET_WIDTH)
                h = geom.get("height", INITIAL_WIDGET_HEIGHT)
                x, y = geom.get("x"), geom.get("y")
                self.widget_width = max(MIN_WIDGET_SIZE, min(MAX_WIDGET_SIZE, w))
                self.widget_height = max(MIN_WIDGET_SIZE, min(MAX_WIDGET_SIZE, h))
                if x is not None and y is not None: self.root.geometry(f"{self.widget_width}x{self.widget_height}+{x}+{y}")
                else: self.root.geometry(f"{self.widget_width}x{self.widget_height}")
            else: self.root.geometry(f"{INITIAL_WIDGET_WIDTH}x{INITIAL_WIDGET_HEIGHT}+200+200")
        except (json.JSONDecodeError, IOError, TypeError) as e:
            logging.error(f"Failed to load geometry: {e}, using defaults.")
            self.root.geometry(f"{INITIAL_WIDGET_WIDTH}x{INITIAL_WIDGET_HEIGHT}+200+200")

    def _save_geometry(self):
        try:
            geom_path = Path(GEOMETRY_FILE.name)
            geom = {"width": self.root.winfo_width(), "height": self.root.winfo_height(), "x": self.root.winfo_x(), "y": self.root.winfo_y()}
            with open(geom_path, "w") as f: json.dump(geom, f, indent=4)
        except (IOError, tk.TclError) as e: logging.error(f"Failed to save geometry: {e}")

    def _schedule_task(self, name, delay_ms, callback, *args):
        if name in self._after_ids:
            try: self.root.after_cancel(self._after_ids[name])
            except(ValueError,tk.TclError): pass
        def w():
            callback(*args)
            if name not in ["toast_clear","save_geometry", "slideshow_fade"]: self._after_ids[name] = self.root.after(delay_ms,w)
        self._after_ids[name] = self.root.after(delay_ms,w)

    def _cancel_all_tasks(self):
        for i in self._after_ids.values():
            try: self.root.after_cancel(i)
            except(ValueError,tk.TclError): pass
        self._after_ids.clear()
        
    def _queue_command(self, cmd, payload=None): self._cmd_queue.put((cmd, payload))
    def _process_cmd_queue(self):
        for _ in range(5):
            try:
                cmd,payload = self._cmd_queue.get_nowait()
                if cmd=="refresh": self.update_ui_with_state()
                elif cmd=="toast": self._draw_toast(*payload)
                elif cmd=="draw_devices": self._draw_device_picker(payload)
            except queue.Empty: break
            except Exception as e: logging.exception(f"UI cmd '{cmd}' error:{e}")

    def _draw_toast(self, text, level="info", duration_ms=2000):
        self.canvas.delete("toast")
        
        h = max(28, self.widget_height // 13)
        toast_font_size = max(9, h // 2)
        toast_font = font.Font(family=FONT_NAME, size=toast_font_size)

        if len(text) <= 2:
            w = h * 1.5 
        else:
            text_width = toast_font.measure(text)
            padding = h 
            w = text_width + padding

        x = (self.widget_width - w) / 2
        y = self.widget_height / 12
        
        bg = {
            "info": "#2b2b2b", 
            "success": "#1DB954", 
            "warn": "#8a5f00", 
            "error": "#732222"
        }.get(level, "#2b2b2b")

        self._draw_rounded_rect(x, y, x + w, y + h, h / 2, bg, alpha=0.95, tags="toast")
        self.canvas.create_text(x + w / 2, y + h / 2, text=text, fill="white", font=toast_font, tags="toast")
        
        self._schedule_task("toast_clear", duration_ms, lambda: self.canvas.delete("toast"))

    def _bind_events(self):
        self.canvas.bind("<ButtonPress-1>",self._start_move_or_resize)
        self.canvas.bind("<B1-Motion>",self._do_move_or_resize)
        self.canvas.bind("<ButtonRelease-1>",self._end_move_or_resize)
        self.canvas.bind("<Button-3>",self._show_context_menu)
        self.root.bind("<Control-MouseWheel>",self._ctrl_mousewheel_resize)
        self.canvas.bind("<Enter>",self._on_mouse_enter)
        self.canvas.bind("<Leave>",self._on_mouse_leave)
        self.canvas.bind("<Motion>", self._on_mouse_move)

    def _on_mouse_move(self, e):
        self.mouse_pos = (e.x, e.y)
        w, h = self.root.winfo_width(), self.root.winfo_height()
        if self.drag_locked: return

        if e.x > w - 20 and e.y > h - 20: self.canvas.config(cursor="bottom_right_corner")
        elif e.x > w - 10: self.canvas.config(cursor="sb_h_double_arrow")
        elif e.y > h - 10: self.canvas.config(cursor="sb_v_double_arrow")
        else: self.canvas.config(cursor="")

    def _on_mouse_enter(self,e):
        if not self.mouse_is_over: self.mouse_is_over=True; self._queue_command("refresh")
    def _on_mouse_leave(self,e):
        self.mouse_pos = (-1, -1)
        self.canvas.config(cursor="")
        if self.mouse_is_over: self.mouse_is_over=False; self._queue_command("refresh")

    def _start_move_or_resize(self,e):
        clickable_tags = {"close", "lock", "prev_hitbox", "play_hitbox", "next_hitbox"}
        if self.canvas.find_withtag(f"current && ({' || '.join(clickable_tags)})"):
             return
        
        if self.drag_locked:return
        self._resize_mode = None
        w, h = self.root.winfo_width(), self.root.winfo_height()
        on_corner = (e.x > w - 20 and e.y > h - 20)
        on_right = (e.x > w - 10 and e.y < h - 20)
        on_bottom = (e.y > h - 10 and e.x < w - 20)
        if self.mouse_is_over and (on_corner or on_right or on_bottom):
            if on_corner: self._resize_mode = "both"
            elif on_right: self._resize_mode = "width"
            elif on_bottom: self._resize_mode = "height"
            self._is_resizing = True
            self._resize_start_pos = (e.x_root, e.y_root)
            self._start_size = (w, h)
        else:
            self._is_resizing = False
            self._x, self._y = e.x, e.y

    def _do_move_or_resize(self,e):
        if self._is_resizing and self._resize_mode:
            dx, dy = e.x_root - self._resize_start_pos[0], e.y_root - self._resize_start_pos[1]
            new_w, new_h = self._start_size
            if self._resize_mode in ["width", "both"]: new_w = self._start_size[0] + dx
            if self._resize_mode in ["height", "both"]: new_h = self._start_size[1] + dy
            self._resize_widget(new_w, new_h)
        elif not self.drag_locked and not self._is_resizing:
            self.root.geometry(f"+{self.root.winfo_x()+e.x-self._x}+{self.root.winfo_y()+e.y-self._y}")

    def _end_move_or_resize(self,e):
        if self._is_resizing: self._is_resizing, self._resize_start_pos, self._resize_mode = False, None, None
        self._schedule_task("save_geometry",2000,self._save_geometry)

    def _ctrl_mousewheel_resize(self,e):
        d=e.delta//120 if hasattr(e,'delta')else(1 if e.num==4 else -1)
        w, h = self.root.winfo_width(), self.root.winfo_height()
        self._resize_widget(w + 20*d, h + 20*d)

    def _resize_widget(self, w, h):
        new_w, new_h = int(w), int(h)
        new_w = max(MIN_WIDGET_SIZE, min(MAX_WIDGET_SIZE, new_w))
        new_h = max(MIN_WIDGET_SIZE, min(MAX_WIDGET_SIZE, new_h))
        if new_w != self.widget_width or new_h != self.widget_height:
            self.widget_width, self.widget_height = new_w, new_h
            self.root.geometry(f"{new_w}x{new_h}")
            self._queue_command("refresh")
            self._schedule_task("save_geometry", 2000, self._save_geometry)

    def _show_context_menu(self,e):
        cm=tk.Menu(self.root,tearoff=0)
        cm.add_command(label="Select Device",command=self._show_device_picker)
        cm.add_command(label="Refresh",command=self._manual_refresh)
        cm.add_separator()
        
        opacity_menu = tk.Menu(cm, tearoff=0)
        for val in [1.0, 0.85, 0.7]:
            opacity_menu.add_command(label=f"{int(val*100)}%", command=lambda v=val: self._set_opacity(v))
        cm.add_cascade(label="Set Opacity", menu=opacity_menu)

        sound_label = "Disable Startup Sound" if self.config.PLAY_STARTUP_SOUND else "Enable Startup Sound"
        cm.add_command(label=sound_label, command=self._toggle_startup_sound)
        cm.add_command(label=f"{'Unlock'if self.drag_locked else'Lock'} Position",command=self._toggle_drag_lock)
        cm.add_separator()
        cm.add_command(label="Exit",command=self._on_close)
        cm.post(e.x_root,e.y_root)

    def _set_opacity(self, value):
        self.config.OPACITY = value
        self.root.attributes("-alpha", self.config.OPACITY)
        try:
            with open(Path(CONFIG_FILE.name), "r+") as f:
                data = json.load(f)
                data['OPACITY'] = self.config.OPACITY
                f.seek(0)
                json.dump(data, f, indent=4)
                f.truncate()
        except Exception as e:
            logging.error(f"Failed to save opacity setting: {e}")

    def _manual_refresh(self):threading.Thread(target=self._fetch_playback_data,daemon=True,name="ManualRefresh").start()
    def _on_close(self):
        logging.info("Shutting down...")
        self._save_geometry()
        self._stop_event.set()
        self._cancel_all_tasks()
        self.root.destroy()

    def update_ui_with_state(self):
        try:
            self.widget_width, self.widget_height = self.root.winfo_width(), self.root.winfo_height()
            with self._state_lock: s, p = self._song_state.copy(), self.is_playing
            self.canvas.delete("all")
            
            self._draw_rounded_rect(0, 0, self.widget_width, self.widget_height, CORNER_RADIUS, "black", alpha=1.0)
            self._draw_rounded_outline(0, 0, self.widget_width, self.widget_height, CORNER_RADIUS, "white", 2, alpha=0.4)
            
            if p:
                self._cancel_task("slideshow")
                pil_art = self._album_art_cache.get(s.get("album_art_url")) if s.get("album_art_url") and not DISABLE_IMAGES else None
                self._draw_playing_ui(s, p, pil_art)
            else:
                self._draw_idle_ui()
                self._schedule_task("slideshow", 5000, self._run_slideshow)
        except Exception as e: logging.exception(f"UI update error:{e}")

    def _draw_playing_ui(self, s, p, pil_art):
        if pil_art:
            blur_radius = 2 if LOW_END_MODE else 4
            bg_art = pil_art.copy().filter(ImageFilter.GaussianBlur(blur_radius))
            alpha_layer = Image.new("L", bg_art.size, int(255 * 0.70))
            bg_art.putalpha(alpha_layer)
            tk_bg_art = ImageTk.PhotoImage(create_rounded_image(bg_art, (self.widget_width, self.widget_height), CORNER_RADIUS))
            self.canvas.create_image(0, 0, anchor="nw", image=tk_bg_art)
            self.tk_image_references["bg_art"] = tk_bg_art
        self._create_ui_elements(s, p, pil_art)

    def _draw_idle_ui(self): self._run_slideshow(is_initial_draw=True)

    def _run_slideshow(self, is_initial_draw=False):
        if self.is_playing or not self._recently_played:
            self._draw_placeholder_idle_ui()
            return
        if not is_initial_draw: self._slideshow_index = (self._slideshow_index + 1) % len(self._recently_played)
        track = self._recently_played[self._slideshow_index]
        art_url, pil_art = track.get("album_art_url"), None
        if art_url: pil_art = self._album_art_cache.get(art_url)
        if not pil_art:
            self._schedule_task("slideshow", 100, self._run_slideshow)
            return
        self.canvas.delete("all")
        self._draw_rounded_rect(0, 0, self.widget_width, self.widget_height, CORNER_RADIUS, "black", alpha=1.0)
        blur_radius = 2 if LOW_END_MODE else 4
        bg_art = pil_art.copy().filter(ImageFilter.GaussianBlur(blur_radius))
        alpha_layer = Image.new("L", bg_art.size, int(255 * 0.70))
        bg_art.putalpha(alpha_layer)
        tk_bg_art = ImageTk.PhotoImage(create_rounded_image(bg_art, (self.widget_width, self.widget_height), CORNER_RADIUS))
        self.canvas.create_image(0, 0, anchor="nw", image=tk_bg_art, tags="slideshow")
        self.tk_image_references["slideshow_bg"] = tk_bg_art
        self._create_ui_elements(track, False, pil_art)
        self._schedule_task("slideshow", 5000, self._run_slideshow)

    def _draw_placeholder_idle_ui(self):
        self.canvas.delete("all")
        self._draw_rounded_rect(0, 0, self.widget_width, self.widget_height, CORNER_RADIUS, "black", alpha=1.0)
        self._create_ui_elements({}, False, None)

    def _create_ui_elements(self, s, p, pil_art):
        if self.mouse_is_over:
            overlay_alpha = OVERLAY_ALPHA if p else 0.3
            self._draw_rounded_rect(0, 0, self.widget_width, self.widget_height, CORNER_RADIUS, "black", alpha=overlay_alpha, tags="hover_overlay")
        
        self._draw_persistent_top_bar()
        art_size = int(min(self.widget_width, self.widget_height) * 0.25)
        art_margin = 12
        art_y = self.widget_height - art_margin - art_size - 10 
        if pil_art:
            tk_art = ImageTk.PhotoImage(create_rounded_image(pil_art, (art_size, art_size), int(art_size * 0.1)))
            self.canvas.create_image(art_margin, art_y, anchor="nw", image=tk_art)
            self.tk_image_references["small_album_art"] = tk_art
        song, artist = s.get("song_name", "Nothing Playing"), s.get("artist_name", "Right-click for menu")
        fs_s = max(9, int(min(self.widget_width, self.widget_height) / 18))
        fs_a = max(8, int(min(self.widget_width, self.widget_height) / 22))
        text_x = art_margin + art_size + 10 if pil_art else art_margin
        text_y = art_y + art_size / 2
        self.canvas.create_text(text_x, text_y - fs_s/2, text=song, fill="white", font=(FONT_NAME,fs_s,"bold"), anchor="w", width=self.widget_width - text_x - 10)
        self.canvas.create_text(text_x, text_y + fs_a, text=artist, fill="lightgray", font=(FONT_NAME,fs_a), anchor="w", width=self.widget_width - text_x - 10)
        
        if self.mouse_is_over:
            self._draw_control_icons(p)
            self._draw_resize_grip()
            self._draw_top_right_icons()

    def _draw_persistent_top_bar(self):
        try:
            icon_size = max(16, min(self.widget_width, self.widget_height) // 12)
            margin = 12
            icon_path = resource_path("spotify_icon.png")
            if icon_path.exists():
                cache_key = f"spotify_icon_{icon_size}"
                if not (tk_icon := self.tk_image_references.get(cache_key)):
                    pil_icon = Image.open(icon_path).convert("RGBA")
                    pil_icon = pil_icon.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
                    tk_icon = ImageTk.PhotoImage(pil_icon)
                    self.tk_image_references[cache_key] = tk_icon
                self.canvas.create_image(margin, margin, anchor="nw", image=tk_icon, tags="persistent")
                font_size = max(8, icon_size // 2)
                self.canvas.create_text(margin + icon_size + 4, margin + icon_size / 2, text="Spotify", anchor="w", fill="white", font=(FONT_NAME, font_size, "bold"), tags="persistent")
            else:
                if not getattr(self, "_icon_warning_logged", False):
                    logging.warning("spotify_icon.png not found, skipping icon.")
                    self._icon_warning_logged = True
        except Exception as e: logging.error(f"Failed to load spotify icon: {e}")

    def _draw_top_right_icons(self):
        self.canvas.delete("top_right_icons")
        s, m, pad = max(16, min(self.widget_width, self.widget_height)//15), 12, 4
        x, y = self.widget_width - m - s, m
        self.canvas.create_oval(x, y, x+s, y+s, fill="#ff4444", outline="", tags=("tr_icon","close"))
        self.canvas.create_line(x+pad, y+pad, x+s-pad, y+s-pad, fill="white", width=2, tags=("tr_icon","close"))
        self.canvas.create_line(x+s-pad, y+pad, x+pad, y+s-pad, fill="white", width=2, tags=("tr_icon","close"))
        self.canvas.tag_bind("close","<Button-1>",lambda e:self._on_close())
        lx = x - s - 6
        lock_color = "#ff8800" if self.drag_locked else "#666666"
        self.canvas.create_oval(lx, y, lx+s, y+s, fill=lock_color, outline="", tags=("tr_icon","lock"))
        pin_head_r, pin_center_x, pin_center_y = s * 0.15, lx + s/2, y + s/2
        self.canvas.create_oval(pin_center_x - pin_head_r, pin_center_y - pin_head_r - 2, pin_center_x + pin_head_r, pin_center_y + pin_head_r - 2, fill="white", outline="", tags=("tr_icon","lock"))
        self.canvas.create_line(pin_center_x, pin_center_y - 2, pin_center_x, pin_center_y + s*0.2, fill="white", width=2, tags=("tr_icon","lock"))
        self.canvas.tag_bind("lock","<Button-1>",lambda e:self._toggle_drag_lock())

    def _toggle_drag_lock(self):self.drag_locked=not self.drag_locked;self._queue_command("refresh")
    def _draw_resize_grip(self):
        self.canvas.delete("resize_grip")
        s=12;x,y=self.widget_width-s-4,self.widget_height-s-4
        for i in range(3):self.canvas.create_line(x+i*3,y+s-2,x+s-2,y+i*3,fill="white",width=1,tags="resize_grip")

    def _draw_rounded_rect(self,x1,y1,x2,y2,r,fill,alpha=1.0,tags=""):
        if alpha<=0.01:return
        w,h=int(x2-x1),int(y2-y1)
        if w <= 0 or h <=0: return
        key=f"rect_{fill}_{alpha:.2f}_{w}x{h}_{r}"
        if not(img_ref:=self._overlay_cache.get(key)):
            img=Image.new("RGBA",(w,h),(0,0,0,0))
            draw=ImageDraw.Draw(img)
            draw.rounded_rectangle((0,0,w,h),r,fill=self.root.winfo_rgb(fill)+(int(alpha*255),))
            img_ref=ImageTk.PhotoImage(img)
            self._overlay_cache[key]=img_ref
        self.canvas.create_image(x1,y1,anchor="nw",image=img_ref,tags=tags)
        self.tk_image_references[key]=img_ref

    def _draw_rounded_outline(self, x1, y1, x2, y2, r, fill, width, alpha=1.0):
        if alpha <= 0.01: return
        w, h = int(x2 - x1), int(y2 - y1)
        if w <= 0 or h <= 0: return
        key = f"outline_{fill}_{alpha:.2f}_{w}x{h}_{r}_{width}"
        if not (img_ref := self._overlay_cache.get(key)):
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle((0, 0, w, h), r, outline=self.root.winfo_rgb(fill) + (int(alpha * 255),), width=width)
            img_ref = ImageTk.PhotoImage(img)
            self._overlay_cache[key] = img_ref
        self.canvas.create_image(x1, y1, anchor="nw", image=img_ref, tags="outline")
        self.tk_image_references[key] = img_ref

    def _draw_control_icons(self,p):
        self.canvas.delete("controls")
        s = min(self.widget_width, self.widget_height) / 12
        y = self.widget_height * 0.45
        c = "white"
        hitbox_w, hitbox_h = self.widget_width / 3.5, s * 3
        
        prev_x = self.widget_width * 0.25
        self.canvas.create_rectangle(prev_x - hitbox_w/2, y - hitbox_h/2, prev_x + hitbox_w/2, y + hitbox_h/2, fill="", outline="", tags="prev_hitbox")
        self.canvas.create_polygon([(prev_x + s*0.2, y - s/2), (prev_x + s*0.2, y + s/2), (prev_x - s*0.3, y)], fill=c, tags="prev_hitbox")
        self.canvas.create_rectangle(prev_x - s*0.5, y - s/2, prev_x - s*0.3, y+s/2, fill=c, outline="", tags="prev_hitbox")
        self.canvas.tag_bind("prev_hitbox","<Button-1>",lambda e:self._prev_track())

        play_x = self.widget_width / 2
        self.canvas.create_rectangle(play_x - hitbox_w/2, y - hitbox_h/2, play_x + hitbox_w/2, y + hitbox_h/2, fill="", outline="", tags="play_hitbox")
        if p:
            pw, g = s*0.7, s*0.3
            self.canvas.create_rectangle(play_x-pw/2,y-s/2,play_x-g/2,y+s/2,fill=c,outline="",tags="play_hitbox")
            self.canvas.create_rectangle(play_x+g/2,y-s/2,play_x+pw/2,y+s/2,fill=c,outline="",tags="play_hitbox")
        else:self.canvas.create_polygon([(play_x-s/2.5,y-s/2),(play_x-s/2.5,y+s/2),(play_x+s/2,y)],fill=c,tags="play_hitbox")
        self.canvas.tag_bind("play_hitbox","<Button-1>",lambda e:self._toggle_play_pause())

        next_x = self.widget_width * 0.75
        self.canvas.create_rectangle(next_x - hitbox_w/2, y - hitbox_h/2, next_x + hitbox_w/2, y + hitbox_h/2, fill="", outline="", tags="next_hitbox")
        self.canvas.create_polygon([(next_x - s*0.2, y - s/2), (next_x - s*0.2, y + s/2), (next_x + s*0.3, y)], fill=c, tags="next_hitbox")
        self.canvas.create_rectangle(next_x + s*0.5, y-s/2, next_x + s*0.3, y+s/2, fill=c, outline="", tags="next_hitbox")
        self.canvas.tag_bind("next_hitbox","<Button-1>",lambda e:self._next_track())

    def _start_background_tasks(self):
        self._spotify_thread=threading.Thread(target=self._spotify_update_loop,daemon=True,name="SpotifyLoop")
        self._spotify_thread.start()

    def _spotify_update_loop(self):
        if not self._init_spotify():return
        while not self._stop_event.is_set():
            try:
                self._fetch_playback_data()
                self._stop_event.wait(NETWORK_POLL_DELAY/1000.0)
            except Exception as e:
                logging.exception(f"Spotify poll loop error:{e}")
                self._stop_event.wait(5)

    def _init_spotify(self) -> bool:
        try:
            scope = "user-read-playback-state user-read-currently-playing user-modify-playback-state user-read-recently-played"
            auth_manager=SpotifyOAuth(client_id=self.config.CLIENT_ID,client_secret=self.config.CLIENT_SECRET,redirect_uri=REDIRECT_URI,scope=scope,username=self.config.USERNAME,cache_path=str(Path(SPOTIPY_CACHE_FILE.name)),open_browser=True)
            self.sp=spotipy.Spotify(auth_manager=auth_manager)
            return True
        except Exception as e:
            logging.critical(f"CRITICAL:Spotify Auth Error:{e}")
            with self._state_lock:self._song_state={"song_name":"Auth Error","artist_name":"Check widget.log"}
            self._queue_command("refresh")
            return False

    def _fetch_playback_data(self):
        if not self.sp:return
        try:
            playback=self.sp.current_playback()
            with self._state_lock: c_p, c_id = self.is_playing, self._song_state.get("id")
            n_p = playback.get("is_playing", False) if playback else False
            n_id = playback.get("item", {}).get("id") if playback and playback.get("item") else None
            changed = (n_p != c_p or n_id != c_id)
            if n_p and playback.get("item"):
                item=playback["item"]
                art_url=(item.get("album",{}).get("images")or[{}])[0].get("url")
                if art_url and art_url not in self._album_art_cache:
                    try:
                        r=requests.get(art_url,timeout=NETWORK_TIMEOUT);r.raise_for_status()
                        self._album_art_cache[art_url]=Image.open(io.BytesIO(r.content)).convert("RGBA")
                    except requests.RequestException as e:logging.error(f"Album art fetch error:{e}")
                with self._state_lock:
                    self.is_playing=n_p
                    self._song_state.update({"id":item.get("id"),"song_name":item.get("name",""),"artist_name":", ".join(a.get("name","")for a in item.get("artists",[])),"album_art_url":art_url,"progress_ms":playback.get("progress_ms",0),"duration_ms":item.get("duration_ms",1)or 1,"last_update_time":time.time()})
            else:
                if c_p: changed = True
                with self._state_lock: self.is_playing, self._song_state = False, {}
                if not self._recently_played or c_p: self._fetch_recently_played()
            if changed: self._queue_command("refresh")
        except spotipy.SpotifyException as e:
            logging.error(f"Spotify API Error:{e}. Token might be expired.")
            with self._state_lock:self._song_state={"song_name":"API Error","artist_name":"Check widget.log"}
            self._queue_command("refresh")
        except Exception as e:logging.exception(f"Unexpected error fetching data:{e}")

    def _fetch_recently_played(self):
        if not self.sp: return
        try:
            logging.info("Fetching recently played tracks for slideshow.")
            results = self.sp.current_user_recently_played(limit=10)
            new_tracks = []
            for item in results.get("items", []):
                track = item.get("track")
                if not track: continue
                art_url = (track.get("album", {}).get("images") or [{}])[0].get("url")
                track_info = {"song_name": track.get("name", ""),"artist_name": ", ".join(a.get("name", "") for a in track.get("artists", [])),"album_art_url": art_url}
                new_tracks.append(track_info)
                if art_url and art_url not in self._album_art_cache:
                    try:
                        r=requests.get(art_url,timeout=NETWORK_TIMEOUT);r.raise_for_status()
                        self._album_art_cache[art_url]=Image.open(io.BytesIO(r.content)).convert("RGBA")
                    except requests.RequestException as e:logging.error(f"Slideshow art fetch error:{e}")
            self._recently_played = new_tracks
            self._slideshow_index = 0
        except Exception as e: logging.error(f"Failed to fetch recently played: {e}")

    def _animate_progress_bar(self):
        self.canvas.delete("progress_bar")
        with self._state_lock:s,p=self._song_state.copy(),self.is_playing
        y1, y2 = self.widget_height - 6, self.widget_height - 2
        bar_height = y2 - y1
        self.canvas.create_rectangle(0, y1, self.widget_width, y2, fill="#404040", outline="", tags="progress_bar")
        if p and s.get("duration_ms"):
            e=(time.time()-s.get("last_update_time",0))*1000
            prog=min(s.get("progress_ms",0)+e,s["duration_ms"])
            ratio=prog/s["duration_ms"]
            bw=self.widget_width*ratio
            if bw>0:self.canvas.create_rectangle(0,y1,bw,y2,fill="#1DB954",outline="",tags="progress_bar")
            
            if self.mouse_is_over:
                ball_radius = bar_height * 0.8
                self.canvas.create_oval(bw - ball_radius, y1 - (ball_radius - bar_height)/2, bw + ball_radius, y2 + (ball_radius - bar_height)/2, fill="#1DB954", outline="white", width=1, tags="progress_bar")
                
                font_size = max(8, self.widget_height // 28)
                y_pos = y1 - (font_size / 2) - 4
                
                self.canvas.create_text(8, y_pos, text=format_ms(int(prog)), fill="lightgray", font=(FONT_NAME, font_size), anchor="w", tags="progress_bar")
                self.canvas.create_text(self.widget_width - 8, y_pos, text=format_ms(s["duration_ms"]), fill="lightgray", font=(FONT_NAME, font_size), anchor="e", tags="progress_bar")
    
    def _cancel_task(self, name):
        if name in self._after_ids:
            try:
                self.root.after_cancel(self._after_ids[name])
                del self._after_ids[name]
            except (ValueError, tk.TclError): pass

    def _send_control_command(self,action):
        if NATIVE_CONTROLS_AVAILABLE:
            if(vk:={"play_pause":VK_MEDIA_PLAY_PAUSE,"next":VK_MEDIA_NEXT_TRACK,"prev":VK_MEDIA_PREV_TRACK}.get(action)):
                threading.Thread(target=self._send_media_key,args=(vk,),daemon=True).start();return
        def task():
            try:
                if not self.sp:return
                actions={"next":self.sp.next_track,"prev":self.sp.previous_track}
                if action=="play_pause":(self.sp.pause_playback if self.is_playing else self.sp.start_playback)()
                elif(call:=actions.get(action)):call()
            except Exception as e:logging.error(f"Web API control for '{action}' failed:{e}")
        threading.Thread(target=task,daemon=True).start()

    def _send_media_key(self,vk):
        try:USER32.keybd_event(vk,0,0,0);time.sleep(0.05);USER32.keybd_event(vk,0,KEYEVENTF_KEYUP,0)
        except Exception as e:logging.exception(f"Failed to send media key:{e}")

    def _toggle_play_pause(self):self._send_control_command("play_pause")
    def _next_track(self):self._send_control_command("next")
    def _prev_track(self):self._send_control_command("prev")
    
    def _toggle_startup_sound(self):
        self.config.PLAY_STARTUP_SOUND = not self.config.PLAY_STARTUP_SOUND
        try:
            with open(Path(CONFIG_FILE.name), "r+") as f:
                data = json.load(f)
                data['PLAY_STARTUP_SOUND'] = self.config.PLAY_STARTUP_SOUND
                f.seek(0)
                json.dump(data, f, indent=4)
                f.truncate()
            
            if self.config.PLAY_STARTUP_SOUND:
                level = "success"
                toast_message = "✓"
            else:
                level = "warn"
                toast_message = "✗"
            
            self._queue_command("toast", (toast_message, level))
        except Exception as e:
            logging.error(f"Failed to save startup sound setting: {e}")
            self._queue_command("toast", ("Error saving setting", "error"))

    def _show_device_picker(self):
        def task():
            try:self._queue_command("draw_devices",self.sp.devices().get("devices",[]))
            except Exception as e:logging.exception("Device picker error")
        threading.Thread(target=task,daemon=True).start()
    
    def _draw_device_picker(self,devices):
        win=tk.Toplevel(self.root);win.title("Spotify Devices")
        tk.Label(win,text="Pick a device:",font=(FONT_NAME,10,"bold")).pack(pady=8)
        lb=tk.Listbox(win);lb.pack(fill="both",expand=True,padx=10,pady=6)
        id_map=[d.get("id")for d in devices]
        for d in devices:lb.insert(tk.END,f"{d.get('name','?')} ({d.get('type','?')})")
        def choose():
            if(idx:=lb.curselection()):
                threading.Thread(target=lambda:self._transfer_playback(id_map[idx[0]]),daemon=True).start()
                win.destroy()
        tk.Button(win,text="Set Active",command=choose).pack(pady=8)
    
    def _transfer_playback(self,dev_id):
        try:
            if self.sp:self.sp.transfer_playback(device_id=dev_id,force_play=False)
        except Exception as e:logging.exception("Transfer playback failed")

def main():
    config_path=Path(CONFIG_FILE.name)
    if not config_path.exists():
        setup_root=tk.Tk()
        setup_root.title(f"{WIDGET_NAME} {VERSION} - Setup")
        install_tk_excepthook(setup_root)
        main_frame = tk.Frame(setup_root, padx=15, pady=15)
        main_frame.pack(expand=True, fill="both")
        instr_frame = tk.LabelFrame(main_frame, text="How to Get Your Spotify Credentials", padx=10, pady=10)
        instr_frame.pack(fill="x", pady=(0, 15))
        instructions = [
            ("1.", "Go to the Spotify Developer Dashboard."),
            ("2.", "Log in and click 'Create App'."),
            ("3.", "Enter any name and description."),
            ("4.", "You'll see your 'Client ID' and 'Client Secret'."),
            ("5.", "Click 'Edit Settings', add the Redirect URI below, and click 'Save':"),
        ]
        def open_dashboard(e): webbrowser.open_new("https://developer.spotify.com/dashboard")
        link = tk.Label(instr_frame, text="https://developer.spotify.com/dashboard", fg="blue", cursor="hand2")
        link.grid(row=0, column=1, sticky="w", padx=5)
        link.bind("<Button-1>", open_dashboard)
        for i, (num, text) in enumerate(instructions, 1):
            tk.Label(instr_frame, text=num).grid(row=i, column=0, sticky="nw")
            tk.Label(instr_frame, text=text, wraplength=450, justify="left").grid(row=i, column=1, sticky="w", padx=5)
        redirect_uri_val = tk.Entry(instr_frame, relief="flat", justify="center")
        redirect_uri_val.insert(0, REDIRECT_URI)
        redirect_uri_val.config(state="readonly", font=font.Font(family=FONT_NAME, weight="bold"))
        redirect_uri_val.grid(row=len(instructions)+1, column=1, sticky="w", padx=5, pady=(5,10))
        
        if not PLAYSOUND_AVAILABLE:
            tk.Label(instr_frame, text="NOTE: 'playsound' not found. Install with 'pip install playsound==1.2.2' for custom startup sounds.", fg="red").grid(row=len(instructions)+2, columnspan=2, sticky="w", padx=5)

        creds_frame = tk.LabelFrame(main_frame, text="Enter Your Credentials", padx=10, pady=10)
        creds_frame.pack(fill="x")
        tk.Label(creds_frame, text="Client ID:").grid(row=0, column=0, sticky="e", pady=2)
        id_entry = tk.Entry(creds_frame, width=55); id_entry.grid(row=0, column=1, pady=2, padx=5)
        tk.Label(creds_frame, text="Client Secret:").grid(row=1, column=0, sticky="e", pady=2)
        secret_entry = tk.Entry(creds_frame, width=55, show="*"); secret_entry.grid(row=1, column=1, pady=2, padx=5)
        tk.Label(creds_frame, text="Spotify Username:").grid(row=2, column=0, sticky="e", pady=2)
        user_entry = tk.Entry(creds_frame, width=55); user_entry.grid(row=2, column=1, pady=2, padx=5)
        
        confirmed = tk.BooleanVar(value=True)
        play_sound = tk.BooleanVar(value=True)
        def toggle_save_button(): save_button.config(state="normal" if confirmed.get() else "disabled")
        confirm_check = tk.Checkbutton(main_frame, text="I have set the Redirect URI in my Spotify App settings.", variable=confirmed, command=toggle_save_button)
        confirm_check.pack(pady=10)
        sound_check = tk.Checkbutton(main_frame, text="Play startup sound", variable=play_sound)
        sound_check.pack(pady=5)
        def save_and_start():
            data={"CLIENT_ID":id_entry.get().strip(),"CLIENT_SECRET":secret_entry.get().strip(),"USERNAME":user_entry.get().strip(),"PLAY_STARTUP_SOUND": play_sound.get(), "OPACITY": 1.0}
            if not all(data[k] for k in ["CLIENT_ID", "CLIENT_SECRET", "USERNAME"]):
                messagebox.showerror("Error","All credential fields are required.")
                return
            os.makedirs("assets", exist_ok=True)
            with open(config_path,"w",encoding="utf-8")as f:json.dump(data,f,indent=4)
            messagebox.showinfo("Success",f"Configuration saved! {WIDGET_NAME} will now start.")
            setup_root.destroy()
        save_button = tk.Button(main_frame, text="Save and Start Widget", command=save_and_start)
        save_button.pack(pady=5)
        setup_root.mainloop()

    if config_path.exists():
        with open(config_path,"r",encoding="utf-8")as f:
            try:
                config_data = json.load(f)
                if 'PLAY_STARTUP_SOUND' not in config_data: config_data['PLAY_STARTUP_SOUND'] = True
                if 'OPACITY' not in config_data: config_data['OPACITY'] = 1.0
                config=AppConfig(**config_data)
            except(json.JSONDecodeError,TypeError)as e:
                messagebox.showerror("Config Error",f"Your {config_path.name} is invalid. Please delete it and restart.")
                return
        widget_root=tk.Tk()
        install_tk_excepthook(widget_root)
        SpotifyWidget(widget_root,config)
        widget_root.mainloop()

if __name__=="__main__":
    main()
