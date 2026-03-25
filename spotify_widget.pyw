import tkinter as tk
from tkinter import font
import threading
import time
from typing import Optional, Dict, Any, Tuple
import io
import json
import os
import sys
import asyncio
import logging
import queue
import re
import random
from pathlib import Path

# Image + network
from PIL import Image, ImageTk, ImageDraw, ImageFilter
from collections import OrderedDict

# Sound
try:
    from playsound import playsound
    PLAYSOUND_AVAILABLE = True
except ImportError:
    PLAYSOUND_AVAILABLE = False

# Windows Media API
try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
    from winsdk.windows.storage.streams import DataReader
    WIN_MEDIA_AVAILABLE = True
except ImportError:
    WIN_MEDIA_AVAILABLE = False

WIDGET_NAME = "Melo Box"
VERSION = "v20.0"

# --- OS/Platform ---
IS_WINDOWS = (os.name == "nt")

if IS_WINDOWS:
    try:
        import ctypes
        USER32 = ctypes.windll.user32
        VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK = 0xB0, 0xB1
        VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAE, 0xAF
        KEYEVENTF_KEYUP = 0x0002
        NATIVE_CONTROLS_AVAILABLE = True
    except (ImportError, AttributeError):
        NATIVE_CONTROLS_AVAILABLE = False
else:
    NATIVE_CONTROLS_AVAILABLE = False

# --- ABSOLUTE PATH FIX FOR WINDOWS .PYW ---
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent.absolute()

CONFIG_FILE = BASE_DIR / "config.json"
GEOMETRY_FILE = BASE_DIR / "geometry.json"

# ASSETS FOLDERS
ASSETS_DIR = BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)

COVERS_DIR = ASSETS_DIR / "covers"
COVERS_DIR.mkdir(parents=True, exist_ok=True)

ICON_CACHE_DIR = BASE_DIR / ".icon_cache"
ICON_CACHE_DIR.mkdir(exist_ok=True)

# --- Env flags / toggles ---
LOW_END_MODE = os.getenv("LOW_END_MODE", "0") == "1"
INITIAL_WIDGET_WIDTH, INITIAL_WIDGET_HEIGHT = 280, 280
MIN_WIDGET_SIZE, MAX_WIDGET_SIZE = 180, 800
CORNER_RADIUS = 24
OVERLAY_ALPHA = 0.5
FONT_NAME = "Segoe UI"
DISABLE_IMAGES = os.getenv("DISABLE_IMAGES", "0") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def resource_path(relative_path: str) -> Path:
    try: base_path = Path(sys._MEIPASS)
    except AttributeError: base_path = ASSETS_DIR
    return base_path / relative_path

class LRUCache(OrderedDict):
    def __init__(self, max_size=10):
        super().__init__()
        self.max_size = max_size
    def __setitem__(self, key, value):
        if key in self: self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.max_size: self.popitem(last=False)

def format_ms(ms: float) -> str:
    seconds = int(max(0, ms) / 1000)
    minutes = seconds // 60
    seconds %= 60
    return f"{minutes:02d}:{seconds:02d}"

def safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name)

def create_rounded_image(pil_image: Image.Image, size: Tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0) + size, radius, fill=255)
    output = pil_image.resize(size, Image.Resampling.LANCZOS)
    output.putalpha(mask)
    return output

def crop_center_fill(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    img_ratio = img.width / img.height
    target_ratio = target_w / target_h
    if img_ratio > target_ratio:
        new_w = int(target_ratio * img.height)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, img.height))
    else:
        new_h = int(img.width / target_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, img.width, top + new_h))
    return img.resize((target_w, target_h), Image.Resampling.LANCZOS)

def get_app_domain_and_name(app_id: str) -> Tuple[str, str]:
    app_id = str(app_id).lower()
    if "spotify" in app_id: return "spotify", "Spotify"
    if "brave" in app_id: return "brave", "Brave"
    if "chrome" in app_id: return "chrome", "Chrome"
    if "edge" in app_id or "msedge" in app_id: return "edge", "Edge"
    if "firefox" in app_id: return "firefox", "Firefox"
    if "vlc" in app_id: return "vlc", "VLC"
    clean_name = app_id.split("!")[-1].split(".")[0].capitalize()
    return clean_name.lower(), clean_name

def fetch_local_app_icon(app_key: str) -> Optional[Image.Image]:
    if not app_key: return None
    for ext in ['.png', '.jpg', '.ico']:
        path = ASSETS_DIR / f"{app_key}{ext}"
        if path.exists():
            try: return Image.open(path).convert("RGBA")
            except: pass
    return None

def get_local_cover_art(title: str) -> Optional[Image.Image]:
    if not title: return None
    safe_title = safe_filename(title)
    for ext in ['.png', '.jpg', '.jpeg']:
        path = COVERS_DIR / f"{safe_title}{ext}"
        if path.exists():
            try: return Image.open(path).convert("RGBA")
            except: pass
    return None

def truncate_text(text: str, font_tuple: Tuple, max_width: int) -> str:
    if not text: return ""
    f = font.Font(family=font_tuple[0], size=font_tuple[1], weight=font_tuple[2] if len(font_tuple)>2 else "normal")
    if f.measure(text) <= max_width: return text
    while len(text) > 0 and f.measure(text + "...") > max_width: text = text[:-1]
    return text + "..."

def install_tk_excepthook(root: tk.Misc):
    def handler(exc, val, tb): logging.error("Tk callback error", exc_info=(exc, val, tb))
    root.report_callback_exception = handler

class UniversalMediaWidget:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.widget_width, self.widget_height = INITIAL_WIDGET_WIDTH, INITIAL_WIDGET_HEIGHT
        self.mouse_is_over = False
        self.is_pinned = False
        self.drag_locked = False
        self._is_resizing, self._resize_start_pos, self._start_size = False, None, None
        self._resize_mode = None
        self._cmd_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._after_ids = {}
        
        self.opacity = 1.0
        self.play_startup_sound = True
        self._load_config()

        self.root.overrideredirect(True)
        self.root.update_idletasks() # Ensure window is created in memory
        self._load_geometry()

        # Cache the OS-level HWND here for our background thread
        if IS_WINDOWS:
            self._hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())

        # --- HIDE FROM ALT-TAB ---
        if IS_WINDOWS:
            try:
                GWL_EXSTYLE = -20
                WS_EX_TOOLWINDOW = 0x00000080
                WS_EX_APPWINDOW = 0x00040000
                style = ctypes.windll.user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
                style = (style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
                ctypes.windll.user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, style)
            except Exception as e:
                logging.error(f"Failed to set ToolWindow style: {e}")
        
        self.root.config(bg="grey")
        self.root.attributes("-alpha", self.opacity)
        if IS_WINDOWS:
            try: self.root.wm_attributes("-transparentcolor", "grey")
            except tk.TclError: pass

        # Only pin if explicitly pinned
        self.root.wm_attributes("-topmost", self.is_pinned)
        
        # State for Win+D survival
        self._is_surviving_wind = False
        
        self.canvas = tk.Canvas(root, bg="grey", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.is_playing = False
        self._forced_app_id = None  
        self._available_sessions = []
        
        self._last_user_action_time = 0.0
        self._action_lock_time = 0.0 
        self._locked_app_id = None
        
        self._state_lock, self._media_state = threading.Lock(), {}
        self.tk_image_references: Dict[str, ImageTk.PhotoImage] = {}
        self._overlay_cache = LRUCache(max_size=20)
        
        self._song_history = []
        self._slideshow_idx = 0
        self.menu_icons = {} 
        
        self._stop_event = threading.Event()
        self._bind_events()
        
        self._run_startup_animation()

    def _load_config(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                    self.opacity = data.get("OPACITY", 1.0)
                    self.is_pinned = data.get("PINNED", False)
                    self.drag_locked = data.get("LOCKED", False)
                    self.play_startup_sound = data.get("PLAY_STARTUP_SOUND", True)
            except Exception: pass

    def _save_config(self):
        with open(CONFIG_FILE, "w") as f: json.dump({"OPACITY": self.opacity, "PINNED": self.is_pinned, "LOCKED": self.drag_locked, "PLAY_STARTUP_SOUND": self.play_startup_sound}, f)

    def _run_startup_animation(self):
        self.canvas.delete("all")
        self._draw_rounded_rect(0, 0, self.widget_width, self.widget_height, CORNER_RADIUS, "black", alpha=0.9)
        
        letters = list(WIDGET_NAME)
        font_size = max(14, min(self.widget_width, self.widget_height) // 10)
        intro_font = (FONT_NAME, font_size, "bold")
        
        total_width = sum(font.Font(family=FONT_NAME, size=font_size, weight="bold").measure(l) for l in letters)
        start_x = (self.widget_width - total_width) / 2
        
        letter_positions = []
        current_x = start_x
        for letter in letters:
            letter_width = font.Font(family=FONT_NAME, size=font_size, weight="bold").measure(letter)
            letter_positions.append({'char': letter, 'x': current_x, 'y': -font_size, 'final_y': self.widget_height / 2 - 10, 'id': None})
            current_x += letter_width

        rainbow_colors = ["#1DB954", "#ff4444", "#ff8800", "#1DB954", "#4b0082", "#9400d3"]
        
        def drop_letters(letter_idx=0):
            if letter_idx >= len(letter_positions):
                self.root.after(200, show_credit)
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

        def show_credit():
            font_size_small = max(8, min(self.widget_width, self.widget_height) // 25)
            self.canvas.create_text(self.widget_width - 12, self.widget_height - 12, text="Made by Mayank", fill="#555555", font=(FONT_NAME, font_size_small), anchor="se", tags="credit")
            self.root.after(500, self._finish_animation)
        
        if LOW_END_MODE:
            self.canvas.create_text(self.widget_width / 2, self.widget_height / 2 - 10, text=WIDGET_NAME, fill="white", font=intro_font)
            show_credit()
        else:
            drop_letters()

    def _finish_animation(self):
        if self.play_startup_sound: self._play_sound()
        self.root.after(500, self._start_main_app)

    def _play_sound(self):
        sound_file = resource_path("startup.wav")
        if PLAYSOUND_AVAILABLE and sound_file.exists():
            threading.Thread(target=lambda: playsound(str(sound_file)), daemon=True).start()

    def _start_main_app(self):
        self._queue_command("refresh")
        self._start_background_tasks()
        self._schedule_task("queue_consumer", 16, self._process_cmd_queue)
        self._schedule_task("progress_bar", 50, self._animate_progress_bar)
        self._schedule_task("eq_anim", 100, self._animate_eq_bars)

    def _fast_win_d_monitor(self):
        """Dedicated high-speed thread to catch Win+D instantly, bypassing Tkinter GUI lag."""
        HWND_TOPMOST = -1
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        
        while not self._stop_event.is_set():
            try:
                lwin = USER32.GetAsyncKeyState(0x5B) & 0x8000
                rwin = USER32.GetAsyncKeyState(0x5C) & 0x8000
                d_key = USER32.GetAsyncKeyState(0x44) & 0x8000
                
                if (lwin or rwin) and d_key:
                    if not self.is_pinned and not self._is_surviving_wind:
                        self._is_surviving_wind = True
                        
                        # Instantly force to OS TopMost (Zero delay)
                        USER32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
                        
                        # Queue the unpinning back on the main thread
                        self.root.after(800, self._restore_after_wind)
            except Exception:
                pass
            time.sleep(0.005) # 5ms loop - completely invisible CPU usage but blazingly fast

    def _restore_after_wind(self):
        """Unpins the widget and securely places it back on the desktop layer."""
        self._is_surviving_wind = False
        
        if not self.is_pinned:
            HWND_NOTOPMOST = -2
            HWND_BOTTOM = 1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            
            # Step 1: Remove TopMost flag
            USER32.SetWindowPos(self._hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            # Step 2: Push firmly to the bottom layer
            USER32.SetWindowPos(self._hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            self.root.wm_attributes("-topmost", False)
        else:
            self.root.wm_attributes("-topmost", True)

    def _load_geometry(self):
        try:
            if GEOMETRY_FILE.exists():
                with open(GEOMETRY_FILE, "r") as f: geom = json.load(f)
                w, h = geom.get("width", INITIAL_WIDGET_WIDTH), geom.get("height", INITIAL_WIDGET_HEIGHT)
                x, y = geom.get("x"), geom.get("y")
                self.widget_width = max(MIN_WIDGET_SIZE, min(MAX_WIDGET_SIZE, w))
                self.widget_height = max(MIN_WIDGET_SIZE, min(MAX_WIDGET_SIZE, h))
                if x is not None and y is not None: self.root.geometry(f"{self.widget_width}x{self.widget_height}+{x}+{y}")
                else: self.root.geometry(f"{self.widget_width}x{self.widget_height}")
            else: self.root.geometry(f"{INITIAL_WIDGET_WIDTH}x{INITIAL_WIDGET_HEIGHT}+200+200")
        except Exception: self.root.geometry(f"{INITIAL_WIDGET_WIDTH}x{INITIAL_WIDGET_HEIGHT}+200+200")

    def _save_geometry(self):
        try:
            geom = {"width": self.root.winfo_width(), "height": self.root.winfo_height(), "x": self.root.winfo_x(), "y": self.root.winfo_y()}
            with open(GEOMETRY_FILE, "w") as f: json.dump(geom, f, indent=4)
        except Exception: pass

    def _schedule_task(self, name, delay_ms, callback, *args):
        if name in self._after_ids:
            try: self.root.after_cancel(self._after_ids[name])
            except: pass
        def w():
            callback(*args)
            if name not in ["save_geometry", "slideshow"]: self._after_ids[name] = self.root.after(delay_ms, w)
        self._after_ids[name] = self.root.after(delay_ms, w)

    def _queue_command(self, cmd, payload=None): self._cmd_queue.put((cmd, payload))
    
    def _process_cmd_queue(self):
        for _ in range(5):
            try:
                cmd, payload = self._cmd_queue.get_nowait()
                if cmd == "refresh": self.update_ui_with_state()
            except queue.Empty: break

    def _bind_events(self):
        self.canvas.bind("<ButtonPress-1>", self._start_move_or_resize)
        self.canvas.bind("<B1-Motion>", self._do_move_or_resize)
        self.canvas.bind("<ButtonRelease-1>", self._end_move_or_resize)
        self.canvas.bind("<Button-3>", self._show_context_menu)
        
        self.canvas.bind("<Double-Button-1>", lambda e: self._focus_or_launch_app(self._media_state.get("app", "")))
        
        self.root.bind("<Control-MouseWheel>", self._ctrl_mousewheel_resize)
        self.canvas.bind("<Enter>", self._on_mouse_enter)
        self.canvas.bind("<Leave>", self._on_mouse_leave)
        self.canvas.bind("<Motion>", self._on_mouse_move)

    def _on_mouse_move(self, e):
        w, h = self.root.winfo_width(), self.root.winfo_height()
        if self.drag_locked: return
        if e.x > w - 20 and e.y > h - 20: self.canvas.config(cursor="bottom_right_corner")
        elif e.x > w - 10: self.canvas.config(cursor="sb_h_double_arrow")
        elif e.y > h - 10: self.canvas.config(cursor="sb_v_double_arrow")
        else: self.canvas.config(cursor="")

    def _on_mouse_enter(self, e):
        if not self.mouse_is_over: 
            self.mouse_is_over = True
            self._queue_command("refresh")
        
    def _on_mouse_leave(self, e):
        self.canvas.config(cursor="")
        if self.mouse_is_over: 
            self.mouse_is_over = False
            self._queue_command("refresh")

    def _start_move_or_resize(self, e):
        if self.canvas.find_withtag("current && (close || lock || pin || app_badge || prev_hitbox || play_hitbox || next_hitbox || vol_up || vol_down || progress_hitbox)"): return
        if self.drag_locked: return
        self._resize_mode = None
        w, h = self.root.winfo_width(), self.root.winfo_height()
        if self.mouse_is_over and (e.x > w - 20 and e.y > h - 20): self._resize_mode = "both"
        elif self.mouse_is_over and (e.x > w - 10 and e.y < h - 20): self._resize_mode = "width"
        elif self.mouse_is_over and (e.y > h - 10 and e.x < w - 20): self._resize_mode = "height"
        
        if self._resize_mode:
            self._is_resizing = True
            self._resize_start_pos, self._start_size = (e.x_root, e.y_root), (w, h)
        else:
            self._is_resizing, self._x, self._y = False, e.x, e.y

    def _do_move_or_resize(self, e):
        if self._is_resizing and self._resize_mode:
            dx, dy = e.x_root - self._resize_start_pos[0], e.y_root - self._resize_start_pos[1]
            new_w = self._start_size[0] + dx if self._resize_mode in ["width", "both"] else self._start_size[0]
            new_h = self._start_size[1] + dy if self._resize_mode in ["height", "both"] else self._start_size[1]
            self._resize_widget(new_w, new_h)
        elif not self._is_resizing and not self.drag_locked:
            self.root.geometry(f"+{self.root.winfo_x()+e.x-self._x}+{self.root.winfo_y()+e.y-self._y}")

    def _end_move_or_resize(self, e):
        self._is_resizing = False
        self._schedule_task("save_geometry", 2000, self._save_geometry)

    def _ctrl_mousewheel_resize(self, e):
        d = e.delta // 120 if hasattr(e, 'delta') else (1 if e.num == 4 else -1)
        self._resize_widget(self.root.winfo_width() + 20*d, self.root.winfo_height() + 20*d)

    def _resize_widget(self, w, h):
        new_w, new_h = max(MIN_WIDGET_SIZE, min(MAX_WIDGET_SIZE, int(w))), max(MIN_WIDGET_SIZE, min(MAX_WIDGET_SIZE, int(h)))
        if new_w != self.widget_width or new_h != self.widget_height:
            self.widget_width, self.widget_height = new_w, new_h
            self.root.geometry(f"{new_w}x{new_h}")
            self._queue_command("refresh")
            self._schedule_task("save_geometry", 2000, self._save_geometry)

    def _focus_or_launch_app(self, app_name):
        if not app_name: return
        app_name = app_name.lower()
        
        def task():
            found_window = False
            if IS_WINDOWS and NATIVE_CONTROLS_AVAILABLE:
                keywords = []
                if "chrome" in app_name: keywords = ["Google Chrome"]
                elif "spotify" in app_name: keywords = ["Spotify Premium", "Spotify"]
                elif "brave" in app_name: keywords = ["Brave"]
                elif "edge" in app_name: keywords = ["Edge", "Microsoft​ Edge"]
                elif "vlc" in app_name: keywords = ["VLC media player"]
                elif "firefox" in app_name: keywords = ["Mozilla Firefox"]
                
                EnumWindows = ctypes.windll.user32.EnumWindows
                EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
                GetWindowText = ctypes.windll.user32.GetWindowTextW
                GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW
                IsWindowVisible = ctypes.windll.user32.IsWindowVisible
                
                def foreach_window(hwnd, lParam):
                    nonlocal found_window
                    if IsWindowVisible(hwnd):
                        length = GetWindowTextLength(hwnd)
                        buff = ctypes.create_unicode_buffer(length + 1)
                        GetWindowText(hwnd, buff, length + 1)
                        title = buff.value.lower()
                        
                        if "melo box" in title or "widget" in title or ".py" in title:
                            return True 
                            
                        for k in keywords:
                            if k.lower() in title:
                                ctypes.windll.user32.ShowWindow(hwnd, 9) 
                                ctypes.windll.user32.SetForegroundWindow(hwnd)
                                found_window = True
                                return False 
                    return True
                
                EnumWindows(EnumWindowsProc(foreach_window), 0)
            
            if not found_window:
                cmd_map = {
                    "spotify": "start spotify:",
                    "chrome": "start chrome",
                    "edge": "start msedge",
                    "brave": "start brave",
                    "vlc": "start vlc",
                    "firefox": "start firefox"
                }
                cmd = cmd_map.get(app_name, "")
                if cmd: os.system(cmd)
                
        threading.Thread(target=task, daemon=True).start()

    def _show_context_menu(self, e):
        cm = tk.Menu(self.root, tearoff=0)
        
        with self._state_lock: app_name = self._media_state.get("app", "")
        
        if app_name:
            cm.add_command(label=f"↗ Bring {app_name} to Front", command=lambda: self._focus_or_launch_app(app_name), font=(FONT_NAME, 9, "bold"))
            cm.add_separator()
            
        launch_menu = tk.Menu(cm, tearoff=0)
        self.menu_icons.clear()
        
        for app in ["Spotify", "Chrome", "Edge", "Brave", "VLC", "Firefox"]:
            domain = app.lower()
            icon_img = fetch_local_app_icon(domain)
            
            if icon_img:
                tk_icon = ImageTk.PhotoImage(icon_img.resize((16, 16), Image.Resampling.LANCZOS))
                self.menu_icons[app] = tk_icon
                launch_menu.add_command(label=f" {app}", image=tk_icon, compound="left", command=lambda a=app: self._focus_or_launch_app(a))
            else:
                launch_menu.add_command(label=f" {app}", command=lambda a=app: self._focus_or_launch_app(a))
            
        cm.add_cascade(label="⚡ Quick Launch Apps", menu=launch_menu)
        cm.add_separator()
            
        opacity_menu = tk.Menu(cm, tearoff=0)
        for val in [1.0, 0.85, 0.7]: opacity_menu.add_command(label=f"{int(val*100)}%", command=lambda v=val: self._set_opacity(v))
        cm.add_cascade(label="Set Opacity", menu=opacity_menu)
        cm.add_command(label=f"{'Unlock'if self.drag_locked else'Lock'} Position", command=self._toggle_drag_lock)
        cm.add_command(label=f"{'Unpin'if self.is_pinned else'Pin'} Window", command=self._toggle_pin)
        cm.add_separator()
        cm.add_command(label="Exit", command=self._on_close)
        cm.post(e.x_root, e.y_root)

    def _set_opacity(self, value):
        self.opacity = value
        self.root.attributes("-alpha", self.opacity)
        self._save_config()

    def _toggle_pin(self): 
        self.is_pinned = not self.is_pinned
        self.root.wm_attributes("-topmost", self.is_pinned)
        if self.is_pinned: self.root.lift()
        self._save_config()
        self._queue_command("refresh")
        
    def _toggle_drag_lock(self): 
        self.drag_locked = not self.drag_locked
        self._save_config()
        self._queue_command("refresh")
        
        # If locking (without pinning), push the app strictly to the desktop layer
        if self.drag_locked and not self.is_pinned and IS_WINDOWS:
            try:
                HWND_BOTTOM = 1
                USER32.SetWindowPos(self._hwnd, HWND_BOTTOM, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            except Exception: pass

    def _on_close(self):
        self._save_geometry()
        self._stop_event.set()
        self._fade_out()

    def _fade_out(self):
        current_alpha = self.root.attributes("-alpha")
        if current_alpha > 0.05:
            self.root.attributes("-alpha", current_alpha - 0.1)
            self.root.after(15, self._fade_out)
        else:
            self.root.destroy()

    def _show_session_menu(self, e):
        with self._state_lock: sessions = list(self._available_sessions)
        if not sessions: return
        
        dropdown = tk.Toplevel(self.root)
        dropdown.overrideredirect(True)
        dropdown.attributes("-topmost", True)
        dropdown.configure(bg="#1e1e1e", highlightthickness=1, highlightbackground="#333333")
        
        x = self.root.winfo_rootx() + 12
        y = self.root.winfo_rooty() + 45
        dropdown.geometry(f"+{x}+{y}")
        
        def close_dropdown(event=None): dropdown.destroy()
        dropdown.bind("<FocusOut>", close_dropdown)
        
        tk.Label(dropdown, text="ACTIVE APPS", bg="#1e1e1e", fg="#888888", font=(FONT_NAME, 7, "bold")).pack(anchor="w", padx=6, pady=(4,2))

        for app_name, app_id, domain in sessions:
            frame = tk.Frame(dropdown, bg="#1e1e1e", cursor="hand2")
            frame.pack(fill="x", pady=0, padx=2) 
            
            icon_img = fetch_local_app_icon(domain)
            if icon_img:
                tk_icon = ImageTk.PhotoImage(icon_img.resize((16, 16), Image.Resampling.LANCZOS))
                self.tk_image_references[f"dd_icon_{app_id}"] = tk_icon
                lbl_icon = tk.Label(frame, image=tk_icon, bg="#1e1e1e")
                lbl_icon.pack(side="left", padx=(4, 2), pady=2)
            
            lbl_text = tk.Label(frame, text=app_name, bg="#1e1e1e", fg="white", font=(FONT_NAME, 9))
            lbl_text.pack(side="left", pady=2, padx=(0,10))
            
            def on_enter(e, f=frame, li=lbl_icon if icon_img else None, lt=lbl_text):
                f.config(bg="#333333"); lt.config(bg="#333333")
                if li: li.config(bg="#333333")
            def on_leave(e, f=frame, li=lbl_icon if icon_img else None, lt=lbl_text):
                f.config(bg="#1e1e1e"); lt.config(bg="#1e1e1e")
                if li: li.config(bg="#1e1e1e")
                
            frame.bind("<Enter>", on_enter); lbl_text.bind("<Enter>", on_enter); 
            if icon_img: lbl_icon.bind("<Enter>", on_enter)
            
            frame.bind("<Leave>", on_leave); lbl_text.bind("<Leave>", on_leave); 
            if icon_img: lbl_icon.bind("<Leave>", on_leave)

            def switch_app(event, a_id=app_id):
                self._force_session_and_pause_others(a_id)
                close_dropdown()
                
            frame.bind("<Button-1>", switch_app)
            lbl_text.bind("<Button-1>", switch_app)
            if icon_img: lbl_icon.bind("<Button-1>", switch_app)

        dropdown.focus_set()

    def _force_session_and_pause_others(self, target_app_id):
        self._forced_app_id = target_app_id
        self._send_media_command("pause_others_and_switch", target_app_id)
        with self._state_lock: self.is_playing = False
        self._queue_command("refresh")

    def update_ui_with_state(self):
        try:
            self.widget_width, self.widget_height = self.root.winfo_width(), self.root.winfo_height()
            with self._state_lock: s, p = self._media_state.copy(), self.is_playing
            self.canvas.delete("all")
            
            self._draw_rounded_rect(0, 0, self.widget_width, self.widget_height, CORNER_RADIUS, "black", alpha=1.0)
            self._draw_rounded_outline(0, 0, self.widget_width, self.widget_height, CORNER_RADIUS, "white", 2, alpha=0.4)
            
            pil_art = s.get("image") if s.get("image") and not DISABLE_IMAGES else None
            
            if not p and not pil_art and self._song_history:
                self._run_slideshow()
                return

            if "slideshow" in self._after_ids:
                self.root.after_cancel(self._after_ids["slideshow"])
                del self._after_ids["slideshow"]

            if pil_art:
                bg_art = crop_center_fill(pil_art.copy(), self.widget_width, self.widget_height)
                bg_art = bg_art.filter(ImageFilter.GaussianBlur(6))
                alpha_layer = Image.new("L", bg_art.size, int(255 * 0.65))
                bg_art.putalpha(alpha_layer)
                tk_bg_art = ImageTk.PhotoImage(create_rounded_image(bg_art, (self.widget_width, self.widget_height), CORNER_RADIUS))
                self.canvas.create_image(0, 0, anchor="nw", image=tk_bg_art)
                self.tk_image_references["bg_art"] = tk_bg_art
                
            self._create_ui_elements(s, p, pil_art)
            self._draw_progress_bar_base()
        except Exception as e: logging.error(f"UI update error:{e}")

    def _run_slideshow(self):
        if not self._song_history: return
        self._slideshow_idx = (self._slideshow_idx + 1) % len(self._song_history)
        hist_track = self._song_history[self._slideshow_idx]
        
        pil_art = hist_track.get("image")
        if pil_art:
            bg_art = crop_center_fill(pil_art.copy(), self.widget_width, self.widget_height)
            bg_art = bg_art.filter(ImageFilter.GaussianBlur(6))
            alpha_layer = Image.new("L", bg_art.size, int(255 * 0.65))
            bg_art.putalpha(alpha_layer)
            tk_bg_art = ImageTk.PhotoImage(create_rounded_image(bg_art, (self.widget_width, self.widget_height), CORNER_RADIUS))
            self.canvas.create_image(0, 0, anchor="nw", image=tk_bg_art, tags="slideshow")
            self.tk_image_references["bg_art"] = tk_bg_art
            
        self._create_ui_elements(hist_track, False, pil_art)
        self._schedule_task("slideshow", 8000, self.update_ui_with_state)

    def _create_ui_elements(self, s, p, pil_art):
        if self.mouse_is_over:
            self._draw_rounded_rect(0, 0, self.widget_width, self.widget_height, CORNER_RADIUS, "black", alpha=OVERLAY_ALPHA, tags="hover_overlay")
        
        art_size = int(min(self.widget_width, self.widget_height) * 0.28)
        art_margin = 16
        art_y = self.widget_height - art_margin - art_size - 22
        
        app_name = s.get("app", "")
        app_domain = s.get("app_domain", "")
        
        if app_name:
            font_sz = max(10, int(self.widget_width / 22))
            icon_size = max(18, font_sz + 6)
            pad = 6
            badge_tags = ("app_badge",)
            
            current_x = 12
            
            if app_domain:
                icon_img = fetch_local_app_icon(app_domain)
                if icon_img:
                    tk_icon = ImageTk.PhotoImage(icon_img.resize((icon_size, icon_size), Image.Resampling.LANCZOS))
                    self.canvas.create_image(current_x, 12, anchor="nw", image=tk_icon, tags=badge_tags)
                    self.tk_image_references["app_icon"] = tk_icon
                    current_x += icon_size + pad
            
            display_text = f"{app_name}  ▼" 
            text_y = 12 + (icon_size / 2)
            
            self.canvas.create_text(current_x+1, text_y+1, text=display_text, fill="black", font=(FONT_NAME, font_sz, "bold"), anchor="w", tags=badge_tags)
            self.canvas.create_text(current_x, text_y, text=display_text, fill="white", font=(FONT_NAME, font_sz, "bold"), anchor="w", tags=badge_tags)
            
            self.canvas.tag_bind("app_badge", "<Button-1>", self._show_session_menu)
            self.canvas.tag_bind("app_badge", "<Enter>", lambda e: self.canvas.config(cursor="hand2"))
            self.canvas.tag_bind("app_badge", "<Leave>", lambda e: self.canvas.config(cursor=""))

        if pil_art:
            cropped_art = crop_center_fill(pil_art.copy(), art_size, art_size)
            tk_art = ImageTk.PhotoImage(create_rounded_image(cropped_art, (art_size, art_size), int(art_size * 0.15)))
            self.canvas.create_image(art_margin, art_y, anchor="nw", image=tk_art)
            self.tk_image_references["small_album_art"] = tk_art
            
        song = s.get("title", "Nothing Playing")
        artist = s.get("artist", "Play media to start")
        
        fs_s = max(11, int(self.widget_width / 18))
        fs_a = max(9, int(self.widget_width / 22))
        
        text_x = art_margin + art_size + 14 if pil_art else art_margin
        text_y = art_y
        max_text_w = self.widget_width - text_x - 12
        
        song_trunc = truncate_text(song, (FONT_NAME, fs_s, "bold"), max_text_w)
        artist_trunc = truncate_text(artist, (FONT_NAME, fs_a, "normal"), max_text_w)
        
        song_id = self.canvas.create_text(text_x, text_y, text=song_trunc, fill="white", font=(FONT_NAME,fs_s,"bold"), anchor="nw")
        bbox = self.canvas.bbox(song_id)
        artist_y = bbox[3] + 4 if bbox else text_y + fs_s + 4
            
        self.canvas.create_text(text_x, artist_y, text=artist_trunc, fill="lightgray", font=(FONT_NAME,fs_a), anchor="nw")
        
        if p: self._draw_eq_bars(text_x + font.Font(family=FONT_NAME, size=fs_a).measure(artist_trunc) + 10, artist_y)
        
        if self.mouse_is_over:
            self._draw_control_icons(p)
            self._draw_resize_grip()
            self._draw_top_right_icons()
            self._draw_volume_controls()

    def _draw_eq_bars(self, x, y):
        for i in range(3):
            bar_id = f"eq_bar_{i}"
            self.canvas.create_rectangle(x + (i*4), y + 4, x + (i*4) + 2, y + 14, fill="#1DB954", outline="", tags=("eq_bars", bar_id))

    def _animate_eq_bars(self):
        with self._state_lock: p = self.is_playing
        if p:
            for i in range(3):
                h = random.randint(2, 10)
                coords = self.canvas.coords(f"eq_bar_{i}")
                if coords:
                    x1, _, x2, y2 = coords
                    self.canvas.coords(f"eq_bar_{i}", x1, y2 - h, x2, y2)
        else:
            for i in range(3):
                coords = self.canvas.coords(f"eq_bar_{i}")
                if coords:
                    x1, _, x2, y2 = coords
                    self.canvas.coords(f"eq_bar_{i}", x1, y2 - 2, x2, y2)

    def _draw_top_right_icons(self):
        s, m, pad = max(12, min(self.widget_width, self.widget_height)//18), 12, 3
        
        cx, y = self.widget_width - m - s, m
        self.canvas.create_oval(cx, y, cx+s, y+s, fill="#ff4444", outline="", tags=("tr_icon","close"))
        self.canvas.create_line(cx+pad, y+pad, cx+s-pad, y+s-pad, fill="white", width=2, tags=("tr_icon","close"))
        self.canvas.create_line(cx+s-pad, y+pad, cx+pad, y+s-pad, fill="white", width=2, tags=("tr_icon","close"))
        self.canvas.tag_bind("close","<Button-1>", lambda e: self._on_close())

        px = cx - s - 6
        pin_color = "#1DB954" if self.is_pinned else "#666666"
        self.canvas.create_oval(px, y, px+s, y+s, fill=pin_color, outline="", tags=("tr_icon","pin"))
        self.canvas.create_oval(px+(s*0.3), y+(s*0.2), px+(s*0.7), y+(s*0.6), fill="white", outline="", tags=("tr_icon","pin"))
        self.canvas.create_line(px+(s*0.5), y+(s*0.6), px+(s*0.5), y+s-2, fill="white", width=2, tags=("tr_icon","pin"))
        self.canvas.tag_bind("pin","<Button-1>", lambda e: self._toggle_pin())

        lx = px - s - 6
        lock_color = "#ff8800" if self.drag_locked else "#666666"
        self.canvas.create_oval(lx, y, lx+s, y+s, fill=lock_color, outline="", tags=("tr_icon","lock"))
        self.canvas.create_rectangle(lx+(s*0.25), y+(s*0.45), lx+(s*0.75), y+(s*0.8), fill="white", outline="", tags=("tr_icon","lock"))
        self.canvas.create_arc(lx+(s*0.35), y+(s*0.2), lx+(s*0.65), y+(s*0.6), start=0, extent=180, outline="white", width=1.5, style=tk.ARC, tags=("tr_icon","lock"))
        self.canvas.tag_bind("lock","<Button-1>", lambda e: self._toggle_drag_lock())

    def _draw_volume_controls(self):
        s = max(14, self.widget_height // 16)
        x = self.widget_width - s - 12
        cy = self.widget_height / 2
        
        uy = cy - s - 5
        self.canvas.create_oval(x, uy, x+s, uy+s, fill="#404040", outline="", tags="vol_up")
        self.canvas.create_line(x+s/2, uy+s*0.25, x+s/2, uy+s*0.75, fill="white", width=2, tags="vol_up")
        self.canvas.create_line(x+s*0.25, uy+s/2, x+s*0.75, uy+s/2, fill="white", width=2, tags="vol_up")
        self.canvas.tag_bind("vol_up", "<Button-1>", lambda e: self._send_vol_command(VK_VOLUME_UP))
        
        dy = cy + 5
        self.canvas.create_oval(x, dy, x+s, dy+s, fill="#404040", outline="", tags="vol_down")
        self.canvas.create_line(x+s*0.25, dy+s/2, x+s*0.75, dy+s/2, fill="white", width=2, tags="vol_down")
        self.canvas.tag_bind("vol_down", "<Button-1>", lambda e: self._send_vol_command(VK_VOLUME_DOWN))

    def _draw_resize_grip(self):
        s = 12; x, y = self.widget_width - s - 4, self.widget_height - s - 14
        for i in range(3): self.canvas.create_line(x+i*3, y+s-2, x+s-2, y+i*3, fill="white", width=1, tags="resize_grip")

    def _draw_rounded_rect(self, x1, y1, x2, y2, r, fill, alpha=1.0, tags=""):
        w, h = int(x2-x1), int(y2-y1)
        if w <= 0 or h <=0 or alpha <= 0.01: return
        key = f"rect_{fill}_{alpha:.2f}_{w}x{h}_{r}"
        if not(img_ref := self._overlay_cache.get(key)):
            img = Image.new("RGBA", (w, h), (0,0,0,0))
            ImageDraw.Draw(img).rounded_rectangle((0,0,w,h), r, fill=self.root.winfo_rgb(fill)+(int(alpha*255),))
            img_ref = ImageTk.PhotoImage(img)
            self._overlay_cache[key] = img_ref
        self.canvas.create_image(x1, y1, anchor="nw", image=img_ref, tags=tags)
        self.tk_image_references[key] = img_ref

    def _draw_rounded_outline(self, x1, y1, x2, y2, r, fill, width, alpha=1.0):
        w, h = int(x2-x1), int(y2-y1)
        if w <= 0 or h <= 0 or alpha <= 0.01: return
        key = f"out_{fill}_{alpha:.2f}_{w}x{h}_{r}_{width}"
        if not(img_ref := self._overlay_cache.get(key)):
            img = Image.new("RGBA", (w, h), (0,0,0,0))
            ImageDraw.Draw(img).rounded_rectangle((0,0,w,h), r, outline=self.root.winfo_rgb(fill)+(int(alpha*255),), width=width)
            img_ref = ImageTk.PhotoImage(img)
            self._overlay_cache[key] = img_ref
        self.canvas.create_image(x1, y1, anchor="nw", image=img_ref, tags="outline")
        self.tk_image_references[key] = img_ref

    def _draw_control_icons(self, p):
        s = min(self.widget_width, self.widget_height) / 10
        y = self.widget_height * 0.40
        c = "white"
        hitbox_w, hitbox_h = self.widget_width / 3.5, s * 3
        
        prev_x = self.widget_width * 0.25
        self.canvas.create_rectangle(prev_x - hitbox_w/2, y - hitbox_h/2, prev_x + hitbox_w/2, y + hitbox_h/2, fill="", outline="", tags="prev_hitbox")
        self.canvas.create_polygon([(prev_x + s*0.2, y - s/2), (prev_x + s*0.2, y + s/2), (prev_x - s*0.3, y)], fill=c, tags="prev_hitbox")
        self.canvas.create_rectangle(prev_x - s*0.5, y - s/2, prev_x - s*0.3, y+s/2, fill=c, outline="", tags="prev_hitbox")
        self.canvas.tag_bind("prev_hitbox", "<Button-1>", lambda e: self._send_media_command("prev"))

        play_x = self.widget_width / 2
        self.canvas.create_rectangle(play_x - hitbox_w/2, y - hitbox_h/2, play_x + hitbox_w/2, y + hitbox_h/2, fill="", outline="", tags="play_hitbox")
        if p:
            pw, g = s*0.7, s*0.3
            self.canvas.create_rectangle(play_x-pw/2, y-s/2, play_x-g/2, y+s/2, fill=c, outline="", tags="play_hitbox")
            self.canvas.create_rectangle(play_x+g/2, y-s/2, play_x+pw/2, y+s/2, fill=c, outline="", tags="play_hitbox")
        else:
            self.canvas.create_polygon([(play_x-s/2.5, y-s/2), (play_x-s/2.5, y+s/2), (play_x+s/2, y)], fill=c, tags="play_hitbox")
        self.canvas.tag_bind("play_hitbox", "<Button-1>", lambda e: self._send_media_command("play_pause"))

        next_x = self.widget_width * 0.75
        self.canvas.create_rectangle(next_x - hitbox_w/2, y - hitbox_h/2, next_x + hitbox_w/2, y + hitbox_h/2, fill="", outline="", tags="next_hitbox")
        self.canvas.create_polygon([(next_x - s*0.2, y - s/2), (next_x - s*0.2, y + s/2), (next_x + s*0.3, y)], fill=c, tags="next_hitbox")
        self.canvas.create_rectangle(next_x + s*0.5, y-s/2, next_x + s*0.3, y+s/2, fill=c, outline="", tags="next_hitbox")
        self.canvas.tag_bind("next_hitbox", "<Button-1>", lambda e: self._send_media_command("next"))

    def _draw_progress_bar_base(self):
        self.canvas.delete("progress_bar_base", "progress_fg", "progress_ball", "progress_time", "progress_dur", "progress_hitbox")
        
        bar_height = 5
        margin = 24
        y_center = self.widget_height - 20
        bar_w = self.widget_width - (margin * 2)
        
        self.canvas.create_line(margin, y_center, margin + bar_w, y_center, fill="#404040", width=bar_height, capstyle=tk.ROUND, tags="progress_bar_base")
        self.canvas.create_line(margin, y_center, margin, y_center, fill="#1DB954", width=bar_height, capstyle=tk.ROUND, tags="progress_fg")
        
        ball_r = 6
        self.canvas.create_oval(margin - ball_r, y_center - ball_r, margin + ball_r, y_center + ball_r, fill="white", outline="", tags="progress_ball", state="hidden")
        
        font_size = max(8, int(self.widget_width / 26))
        y_pos = y_center - 13
        self.canvas.create_text(margin, y_pos, text="00:00", fill="lightgray", font=(FONT_NAME, font_size), anchor="w", tags="progress_time", state="hidden")
        self.canvas.create_text(self.widget_width - margin, y_pos, text="00:00", fill="lightgray", font=(FONT_NAME, font_size), anchor="e", tags="progress_dur", state="hidden")

        self.canvas.create_rectangle(0, self.widget_height - 35, self.widget_width, self.widget_height, fill="", outline="", tags="progress_hitbox")
        self.canvas.tag_bind("progress_hitbox", "<Button-1>", self._seek_media)
        self.canvas.tag_bind("progress_hitbox", "<B1-Motion>", self._seek_media)

    def _animate_progress_bar(self):
        with self._state_lock: s, p = self._media_state.copy(), self.is_playing
        if not s.get("duration_ms"): return
        
        elapsed = (time.time() - s.get("last_update_time", time.time())) * 1000 if p else 0
        prog = min(s.get("progress_ms", 0) + elapsed, s["duration_ms"])
        ratio = max(0.0, min(1.0, prog / s["duration_ms"]))
        
        margin = 24
        bar_w = self.widget_width - (margin * 2)
        bw = max(0.1, bar_w * ratio)
        y_center = self.widget_height - 20
        
        self.canvas.coords("progress_fg", margin, y_center, margin + bw, y_center)
        
        if self.mouse_is_over:
            ball_r = 6
            self.canvas.coords("progress_ball", margin + bw - ball_r, y_center - ball_r, margin + bw + ball_r, y_center + ball_r)
            self.canvas.itemconfig("progress_time", text=format_ms(prog))
            self.canvas.itemconfig("progress_dur", text=format_ms(s["duration_ms"]))
            
            self.canvas.itemconfig("progress_ball", state="normal")
            self.canvas.itemconfig("progress_time", state="normal")
            self.canvas.itemconfig("progress_dur", state="normal")
        else:
            self.canvas.itemconfig("progress_ball", state="hidden")
            self.canvas.itemconfig("progress_time", state="hidden")
            self.canvas.itemconfig("progress_dur", state="hidden")

    def _seek_media(self, event):
        with self._state_lock:
            if not self._media_state.get("duration_ms"): return
            click_x = event.x
            margin = 24
            bar_w = self.widget_width - (margin * 2)
            
            ratio = max(0.0, min(1.0, (click_x - margin) / bar_w))
            target_ms = ratio * self._media_state["duration_ms"]
            self._media_state["progress_ms"] = target_ms
            self._media_state["last_update_time"] = time.time()
            
        self._send_media_command("seek", target_ms)

    def _send_vol_command(self, vk):
        if not NATIVE_CONTROLS_AVAILABLE: return
        try:
            USER32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.02)
            USER32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        except Exception: pass

    def _send_media_command(self, action, payload=None):
        self._last_user_action_time = time.time()
        
        if action in ["next", "prev"]:
            self._action_lock_time = time.time() + 2.5  
            with self._state_lock:
                self._locked_app_id = self._media_state.get("app_id_raw")
                self._media_state["progress_ms"] = 0
                self._media_state["last_seen_pos"] = 0
                self._media_state["last_update_time"] = time.time()
        elif action == "play_pause":
            self.is_playing = not self.is_playing
            self._queue_command("refresh")

        def run_async_cmd(): asyncio.run(self._execute_smtc_cmd(action, payload))
        threading.Thread(target=run_async_cmd, daemon=True).start()

    async def _execute_smtc_cmd(self, action, payload=None):
        if not WIN_MEDIA_AVAILABLE: return
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        
        target_session = None
        
        if action == "pause_others_and_switch":
            for s in manager.get_sessions():
                if s.source_app_user_model_id != payload:
                    info = s.get_playback_info()
                    if info and info.playback_status == 4:
                        await s.try_toggle_play_pause_async()
            return
            
        if getattr(self, "_locked_app_id", None) and time.time() < self._action_lock_time:
            for s in manager.get_sessions():
                if s.source_app_user_model_id == self._locked_app_id: target_session = s; break
        elif getattr(self, "_forced_app_id", None):
            for s in manager.get_sessions():
                if s.source_app_user_model_id == self._forced_app_id: target_session = s; break
        
        if not target_session:
            for s in manager.get_sessions():
                if s.get_playback_info() and s.get_playback_info().playback_status == 4: target_session = s; break
        if not target_session: target_session = manager.get_current_session()
        
        if target_session:
            self._locked_app_id = target_session.source_app_user_model_id
            try:
                if action == "play_pause": await target_session.try_toggle_play_pause_async()
                elif action == "next":
                    info = target_session.get_playback_info()
                    if info and getattr(info.controls, "is_next_enabled", True): await target_session.try_skip_next_async()
                    elif NATIVE_CONTROLS_AVAILABLE: self._send_vol_command(VK_MEDIA_NEXT_TRACK)
                elif action == "prev":
                    info = target_session.get_playback_info()
                    if info and getattr(info.controls, "is_previous_enabled", True): await target_session.try_skip_previous_async()
                    elif NATIVE_CONTROLS_AVAILABLE: self._send_vol_command(VK_MEDIA_PREV_TRACK) 
                elif action == "seek" and payload is not None:
                    await target_session.try_change_playback_position_async(int(payload * 10000))
            except Exception as e: logging.error(f"Action {action} failed: {e}")

    def _start_background_tasks(self):
        if WIN_MEDIA_AVAILABLE: 
            threading.Thread(target=self._run_async_media_loop, daemon=True, name="WinMediaLoop").start()
            
        if IS_WINDOWS:
            # We start the dedicated Win+D thread here
            threading.Thread(target=self._fast_win_d_monitor, daemon=True, name="WinDMonitor").start()

    def _run_async_media_loop(self):
        asyncio.run(self._media_poll_loop())

    async def _media_poll_loop(self):
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        
        last_track_change_time = 0.0
        last_track_title = ""
        
        while not self._stop_event.is_set():
            try:
                sessions = manager.get_sessions()
                
                available_apps = []
                active_ids = []
                for s in sessions:
                    app_id = s.source_app_user_model_id
                    active_ids.append(app_id)
                    domain, clean_name = get_app_domain_and_name(app_id)
                    available_apps.append((clean_name, app_id, domain))
                
                with self._state_lock: self._available_sessions = available_apps
                
                if getattr(self, "_forced_app_id", None) and self._forced_app_id not in active_ids:
                    self._forced_app_id = None

                active_session = None
                
                if time.time() < self._action_lock_time and self._locked_app_id:
                    for s in sessions:
                        if s.source_app_user_model_id == self._locked_app_id: active_session = s; break
                
                if not active_session:
                    for s in sessions:
                        info = s.get_playback_info()
                        if info and info.playback_status == 4: active_session = s; break
                
                if not active_session and getattr(self, "_forced_app_id", None):
                    for s in sessions:
                        if s.source_app_user_model_id == self._forced_app_id: active_session = s; break
                            
                if not active_session: active_session = manager.get_current_session()

                if active_session:
                    self._locked_app_id = active_session.source_app_user_model_id
                    
                    info = await active_session.try_get_media_properties_async()
                    playback_info = active_session.get_playback_info()
                    timeline = active_session.get_timeline_properties()
                    
                    n_p = (playback_info.playback_status == 4) if playback_info else False
                    n_title = info.title or ""
                    n_artist = info.artist or ""
                    domain, clean_app_name = get_app_domain_and_name(active_session.source_app_user_model_id)
                    
                    n_pos_ms = timeline.position.total_seconds() * 1000 if timeline else 0
                    n_dur_ms = timeline.end_time.total_seconds() * 1000 if timeline else 1
                    
                    if time.time() - self._last_user_action_time < 1.0: n_p = self.is_playing 
                    
                    pil_image = get_local_cover_art(n_title)
                    
                    if n_title != last_track_title:
                        last_track_change_time = time.time()
                        last_track_title = n_title
                        n_pos_ms = 0
                        with self._state_lock: 
                            self._media_state["image"] = None
                            self._media_state["last_seen_pos"] = 0
                            self._media_state["progress_ms"] = 0
                        
                    ignore_new_thumb = (time.time() - last_track_change_time) < 1.5
                    
                    with self._state_lock:
                        changed = (
                            self.is_playing != n_p or 
                            self._media_state.get("title") != n_title or 
                            self._media_state.get("app") != clean_app_name
                        )
                    
                    if pil_image is None:
                        pil_image = self._media_state.get("image")
                        if info.thumbnail and not ignore_new_thumb:
                            try:
                                stream = await info.thumbnail.open_read_async()
                                reader = DataReader(stream)
                                await reader.load_async(stream.size)
                                buffer = bytearray(stream.size)
                                reader.read_bytes(buffer)
                                new_pil = Image.open(io.BytesIO(buffer)).convert("RGBA")
                                if pil_image is None or new_pil.tobytes() != pil_image.tobytes():
                                    pil_image = new_pil
                                    changed = True
                            except Exception: pass
                    else:
                        if self._media_state.get("image") is None: changed = True
                    
                    if changed:
                        track_data = {
                            "title": n_title or "Unknown", "artist": n_artist or "Unknown",
                            "app": clean_app_name, "app_domain": domain, "app_id_raw": active_session.source_app_user_model_id,
                            "image": pil_image, "progress_ms": n_pos_ms, "duration_ms": n_dur_ms, 
                            "last_update_time": time.time(), "last_seen_pos": n_pos_ms
                        }
                        
                        if pil_image and n_title and not any(t["title"] == n_title for t in self._song_history):
                            self._song_history.insert(0, track_data)
                            if len(self._song_history) > 10: self._song_history.pop()
                        
                        with self._state_lock:
                            self.is_playing = n_p
                            self._media_state = track_data
                        self._queue_command("refresh")
                    else:
                        with self._state_lock:
                            self.is_playing = n_p
                            if self._media_state:
                                current_last_seen = self._media_state.get("last_seen_pos", -1)
                                current_interpolated = self._media_state.get("progress_ms", 0) + ((time.time() - self._media_state.get("last_update_time", time.time())) * 1000 if n_p else 0)
                                
                                if n_pos_ms != current_last_seen or abs(n_pos_ms - current_interpolated) > 2000:
                                    self._media_state["progress_ms"] = n_pos_ms
                                    self._media_state["last_update_time"] = time.time()
                                    self._media_state["last_seen_pos"] = n_pos_ms
                                    
                                self._media_state["duration_ms"] = n_dur_ms
                else:
                    if self.is_playing or self._media_state:
                        with self._state_lock:
                            self.is_playing = False
                            self._media_state = {}
                        self._queue_command("refresh")
                        
            except Exception as e: logging.error(f"Media loop error: {e}")
            await asyncio.sleep(0.3) 

def main():
    root = tk.Tk()
    install_tk_excepthook(root)
    UniversalMediaWidget(root)
    root.mainloop()

if __name__ == "__main__":
    main()
