import tkinter as tk
from tkinter import messagebox
import threading
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any
import io
import json
import os
import sys
import asyncio
import logging

# Image + network
from PIL import Image, ImageTk, ImageDraw
import requests

# Spotify
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# --- Windows Media Control ---
try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as SessionManager
    IS_WINDOWS = True
except ImportError:
    IS_WINDOWS = False
    print("Windows-specific libraries not found. Controls will be disabled.")

# --- WIDGET CONFIGURATION ---
INITIAL_WIDGET_SIZE = 250
FONT_NAME = "Segoe UI"
UPDATE_DELAY_S = 1.0
CONFIG_FILE = "config.json"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
NETWORK_TIMEOUT = 6

# Safe-mode toggles via env vars
ENABLE_TRANSPARENCY = os.getenv("ENABLE_TRANSPARENCY", "0") == "1"
DEBUG = os.getenv("DEBUG", "0") == "1"

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    handlers=[logging.FileHandler("widget.log", mode="w", encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)

def resource_path(relative_path: str) -> str:
    try:
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def create_rounded_image(pil_image: Image.Image, size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0) + size, radius, fill=255)
    output = pil_image.copy().resize(size, Image.Resampling.LANCZOS)
    output.putalpha(mask)
    return output

def rgb_to_8bit_tuple(rgb16: tuple[int, int, int]) -> tuple[int, int, int]:
    return tuple(int(v / 257) for v in rgb16)

@dataclass
class AppConfig:
    CLIENT_ID: str
    CLIENT_SECRET: str
    USERNAME: str

class SetupWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("First-Time Setup")
        self.root.geometry("420x270")
        self.root.resizable(False, False)

        tk.Label(root, text="Please enter your Spotify API credentials.", font=(FONT_NAME, 12)).pack(pady=10)
        tk.Label(root, text="You only need to do this once.", font=(FONT_NAME, 9), fg="gray").pack()

        tk.Label(root, text="Client ID:").pack(pady=(10, 0))
        self.client_id_entry = tk.Entry(root, width=52)
        self.client_id_entry.pack()

        tk.Label(root, text="Client Secret:").pack(pady=(10, 0))
        self.client_secret_entry = tk.Entry(root, width=52, show="*")
        self.client_secret_entry.pack()

        tk.Label(root, text="Spotify Username:").pack(pady=(10, 0))
        self.username_entry = tk.Entry(root, width=52)
        self.username_entry.pack()

        tk.Button(root, text="Save and Start Widget", command=self.save_config).pack(pady=20)

    def save_config(self):
        config_data = {
            "CLIENT_ID": self.client_id_entry.get().strip(),
            "CLIENT_SECRET": self.client_secret_entry.get().strip(),
            "USERNAME": self.username_entry.get().strip(),
        }
        if not all(config_data.values()):
            messagebox.showerror("Error", "All fields are required.")
            return

        with open(resource_path(CONFIG_FILE), "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)
        messagebox.showinfo("Success", "Configuration saved! The widget will now start.")
        self.root.destroy()

class SpotifyWidget:
    def __init__(self, root: tk.Tk, config: AppConfig):
        self.root = root
        self.config = config
        self.widget_size = INITIAL_WIDGET_SIZE

        self.root.overrideredirect(True)
        self.root.geometry(f"{self.widget_size}x{self.widget_size}+200+200")
        self.root.config(bg="grey")
        if IS_WINDOWS and ENABLE_TRANSPARENCY:
            logging.info("Enabling window transparency")
            self.root.wm_attributes("-transparentcolor", "grey")
        else:
            logging.info("Transparency disabled (ENABLE_TRANSPARENCY=0)")

        self.root.attributes("-topmost", False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.canvas = tk.Canvas(root, bg="grey", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # State
        self.sp: Optional[spotipy.Spotify] = None
        self.is_playing = False

        self._state_lock = threading.Lock()
        self._song_state: Dict[str, Any] = {}
        self.tk_image_references: Dict[str, ImageTk.PhotoImage] = {}
        self._album_art_cache_key: Optional[str] = None
        self._stop_event = threading.Event()
        self._overlay_cache: Dict[str, ImageTk.PhotoImage] = {}

        self._bind_events()
        self.update_ui_with_state()

        logging.info("Starting Spotify background thread")
        self._spotify_thread = threading.Thread(target=self._spotify_update_loop, daemon=True, name="SpotifyLoop")
        self._spotify_thread.start()
        self._animate_progress_bar()

    def _bind_events(self):
        self.canvas.bind("<ButtonPress-1>", self._start_move)
        self.canvas.bind("<B1-Motion>", self._do_move)
        self.root.bind("<Enter>", self._on_enter)
        self.root.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-2>", self._force_refresh)
        self.canvas.bind("<Button-3>", lambda e: self._on_close())

    def _start_move(self, event):
        self._x, self._y = event.x, event.y

    def _do_move(self, event):
        try:
            deltax, deltay = event.x - self._x, event.y - self._y  # fixed: _y
            self.root.geometry(f"+{{self.root.winfo_x() + deltax}}+{{self.root.winfo_y() + deltay}}")
        except Exception as e:
            logging.warning(f"Move error: {{e}}")

    def _on_enter(self, _event):
        self.canvas.itemconfigure("overlay", state="normal")
        self.canvas.itemconfigure("controls", state="normal")

    def _on_leave(self, _event):
        self.canvas.itemconfigure("overlay", state="hidden")
        self.canvas.itemconfigure("controls", state="hidden")

    def _force_refresh(self, _event=None):
        self._fetch_playback_data(schedule_ui=True)

    def _on_close(self):
        self._stop_event.set()
        try:
            if hasattr(self, "_progress_after_id"):
                self.root.after_cancel(self._progress_after_id)
        except Exception:
            pass
        self.root.destroy()

    def update_ui_with_state(self):
        try:
            with self._state_lock:
                song_info = dict(self._song_state)
                is_playing = self.is_playing

            self.canvas.delete("all")

            pil_art: Optional[Image.Image] = song_info.get("album_art_pil")
            if pil_art is not None:
                cache_key = f"album_{{self.widget_size}}"
                if self._album_art_cache_key != cache_key:
                    rounded_art = create_rounded_image(pil_art, (self.widget_size, self.widget_size), 20)
                    self.tk_image_references["album_art"] = ImageTk.PhotoImage(rounded_art)
                    self._album_art_cache_key = cache_key
                self.canvas.create_image(0, 0, anchor="nw", image=self.tk_image_references["album_art"])
            else:
                self._draw_rounded_rect(0, 0, self.widget_size, self.widget_size, 20, "black", alpha=1.0)

            self._create_overlay_elements(song_info, is_playing)
        except Exception as e:
            logging.exception(f"UI update error: {{e}}")

    def _create_overlay_elements(self, song_info: Dict[str, Any], is_playing: bool):
        if song_info.get("song_name"):
            song_name_text = song_info.get("song_name", "")
            artist_name_text = song_info.get("artist_name", "")
        else:
            song_name_text = "Spotify Idle"
            artist_name_text = "Hover to control"

        self._draw_rounded_rect(0, 0, self.widget_size, self.widget_size, 20, "black", alpha=0.5, tags="overlay")

        font_size_song = max(9, int(self.widget_size / 16))
        font_size_artist = max(8, int(self.widget_size / 20))
        song_y = self.widget_size / 2 - font_size_song
        artist_y = self.widget_size / 2 + font_size_artist

        self.canvas.create_text(
            self.widget_size / 2, song_y,
            text=song_name_text, fill="white",
            font=(FONT_NAME, font_size_song, "bold"),
            width=self.widget_size - 20, tags="overlay"
        )
        self.canvas.create_text(
            self.widget_size / 2, artist_y,
            text=artist_name_text, fill="lightgray",
            font=(FONT_NAME, font_size_artist),
            width=self.widget_size - 20, tags="overlay"
        )

        self._draw_control_icons(is_playing)
        self.canvas.itemconfigure("overlay", state="hidden")
        self.canvas.itemconfigure("controls", state="hidden")

    def _draw_control_icons(self, is_playing: bool, hover_state: Optional[str] = None):
        self.canvas.delete("controls")
        icon_size = self.widget_size / 12
        controls_y = self.widget_size * 0.75

        play_color = "#1DB954" if hover_state == "play" else "white"
        prev_color = "#1DB954" if hover_state == "prev" else "white"
        next_color = "#1DB954" if hover_state == "next" else "white"

        prev_coords = [
            (self.widget_size * 0.28, controls_y - icon_size / 2),
            (self.widget_size * 0.28, controls_y + icon_size / 2),
            (self.widget_size * 0.2, controls_y),
        ]
        self.canvas.create_polygon(prev_coords, fill=prev_color, outline=prev_color, tags=("controls", "prev_btn"))
        self.canvas.create_rectangle(
            self.widget_size * 0.18, controls_y - icon_size / 2,
            self.widget_size * 0.2, controls_y + icon_size / 2,
            fill=prev_color, outline="", tags=("controls", "prev_btn")
        )

        if is_playing:
            pause_w, gap = icon_size * 0.7, icon_size * 0.3
            self.canvas.create_rectangle(
                self.widget_size / 2 - pause_w / 2, controls_y - icon_size / 2,
                self.widget_size / 2 - gap / 2, controls_y + icon_size / 2,
                fill=play_color, outline="", tags=("controls", "play_btn")
            )
            self.canvas.create_rectangle(
                self.widget_size / 2 + gap / 2, controls_y - icon_size / 2,
                self.widget_size / 2 + pause_w / 2, controls_y + icon_size / 2,
                fill=play_color, outline="", tags=("controls", "play_btn")
            )
        else:
            play_coords = [
                (self.widget_size / 2 - icon_size / 2.5, controls_y - icon_size / 2),
                (self.widget_size / 2 - icon_size / 2.5, controls_y + icon_size / 2),
                (self.widget_size / 2 + icon_size / 2, controls_y),
            ]
            self.canvas.create_polygon(play_coords, fill=play_color, outline=play_color, tags=("controls", "play_btn"))

        next_coords = [
            (self.widget_size * 0.72, controls_y - icon_size / 2),
            (self.widget_size * 0.72, controls_y + icon_size / 2),
            (self.widget_size * 0.8, controls_y),
        ]
        self.canvas.create_polygon(next_coords, fill=next_color, outline=next_color, tags=("controls", "next_btn"))
        self.canvas.create_rectangle(
            self.widget_size * 0.82, controls_y - icon_size / 2,
            self.widget_size * 0.8, controls_y + icon_size / 2,
            fill=next_color, outline="", tags=("controls", "next_btn")
        )

        self.canvas.tag_bind("prev_btn", "<Enter>", lambda e: self._draw_control_icons(self.is_playing, "prev"))
        self.canvas.tag_bind("play_btn", "<Enter>", lambda e: self._draw_control_icons(self.is_playing, "play"))
        self.canvas.tag_bind("next_btn", "<Enter>", lambda e: self._draw_control_icons(self.is_playing, "next"))
        self.canvas.tag_bind("controls", "<Leave>", lambda e: self._draw_control_icons(self.is_playing, None))

        if IS_WINDOWS:
            self.canvas.tag_bind("prev_btn", "<Button-1>", lambda e: self._prev_track())
            self.canvas.tag_bind("play_btn", "<Button-1>", lambda e: self._toggle_play_pause())
            self.canvas.tag_bind("next_btn", "<Button-1>", lambda e: self._next_track())

    def _draw_rounded_rect(self, x1, y1, x2, y2, radius, fill, alpha=1.0, tags=""):
        width, height = (x2 - x1), (y2 - y1)
        cache_key = f"rect_{{fill}}_{{alpha}}_{{width}}x{{height}}_{{radius}}"
        img_ref = getattr(self, "_overlay_cache", {}).get(cache_key)
        if img_ref is None:
            img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            rgba = rgb_to_8bit_tuple(self.root.winfo_rgb(fill)) + (int(alpha * 255),)
            draw.rounded_rectangle((0, 0, width, height), radius, fill=rgba)
            img_ref = ImageTk.PhotoImage(img)
            self._overlay_cache[cache_key] = img_ref
        self.canvas.create_image(x1, y1, anchor="nw", image=img_ref, tags=tags)
        self.tk_image_references[cache_key] = img_ref

    def _spotify_update_loop(self):
        try:
            logging.info("Authorizing with Spotify...")
            scope = "user-read-playback-state"
            auth_manager = SpotifyOAuth(
                client_id=self.config.CLIENT_ID,
                client_secret=self.config.CLIENT_SECRET,
                redirect_uri=REDIRECT_URI,
                scope=scope,
                username=self.config.USERNAME,
                cache_path=resource_path(".spotipy_cache"),
                open_browser=True,
            )
            self.sp = spotipy.Spotify(auth_manager=auth_manager)
            logging.info("Spotify auth OK")
        except Exception:
            logging.exception("Auth Error")
            with self._state_lock:
                self._song_state = {"song_name": "Auth Error"}
                self.is_playing = False
            self.root.after(0, self.update_ui_with_state)
            return

        while not self._stop_event.is_set():
            self._fetch_playback_data(schedule_ui=True)
            for _ in range(int(UPDATE_DELAY_S * 10)):
                if self._stop_event.is_set():
                    break
                time.sleep(0.1)

    def _fetch_playback_data(self, schedule_ui: bool):
        if not self.sp:
            return
        try:
            playback = self.sp.current_playback()
            logging.debug("Fetched playback")
            if playback and playback.get("item"):
                new_is_playing = bool(playback.get("is_playing"))
                item = playback["item"]

                images = item.get("album", {}).get("images", [])
                new_album_art_url = images[0]["url"] if images else None

                album_art_pil = None
                if new_album_art_url and new_album_art_url != (self._song_state.get("album_art_url")):
                    try:
                        resp = requests.get(new_album_art_url, timeout=NETWORK_TIMEOUT)
                        resp.raise_for_status()
                        album_art_pil = Image.open(io.BytesIO(resp.content)).convert("RGBA")
                    except Exception:
                        logging.exception("Album art fetch error")

                with self._state_lock:
                    if album_art_pil is not None:
                        self._song_state["album_art_pil"] = album_art_pil
                        self._album_art_cache_key = None

                    self._song_state.update(
                        {
                            "is_playing": new_is_playing,
                            "song_name": item.get("name", ""),
                            "artist_name": ", ".join(a.get("name", "") for a in item.get("artists", [])),
                            "album_art_url": new_album_art_url,
                            "progress_ms": playback.get("progress_ms", 0),
                            "duration_ms": item.get("duration_ms", 1) or 1,
                            "last_update_time": time.time(),
                        }
                    )
                    self.is_playing = new_is_playing
            else:
                with self._state_lock:
                    self.is_playing = False
                    self._song_state = {}

            if schedule_ui:
                self.root.after(0, self.update_ui_with_state)

        except Exception:
            logging.exception("Spotify API error")

    def _animate_progress_bar(self):
        try:
            self.canvas.delete("progress_bar")
            with self._state_lock:
                state = dict(self._song_state)
                playing = self.is_playing

            if playing and state:
                elapsed = (time.time() - state.get("last_update_time", 0)) * 1000.0
                current_progress = state.get("progress_ms", 0) + elapsed
                duration = max(1, state.get("duration_ms", 1))
                progress_ratio = min(current_progress / duration, 1.0)
                bar_width = self.widget_size * progress_ratio

                y1 = self.widget_size - 6
                y2 = self.widget_size - 2
                self.canvas.create_rectangle(0, y1, self.widget_size, y2, fill="#404040", outline="", tags="progress_bar")
                if bar_width > 0:
                    self.canvas.create_rectangle(0, y1, bar_width, y2, fill="#1DB954", outline="", tags="progress_bar")
        except Exception:
            logging.exception("Progress bar error")
        finally:
            self._progress_after_id = self.root.after(50, self._animate_progress_bar)

    async def _win_control(self, action: str):
        try:
            sessions = await SessionManager.request_async()
            session = sessions.get_current_session()
            if session:
                if action == "play_pause":
                    await session.try_toggle_play_pause_async()
                elif action == "next":
                    await session.try_skip_next_async()
                elif action == "prev":
                    await session.try_skip_previous_async()
        except Exception:
            logging.exception("WinSDK Error")

    def _run_async(self, coro):
        threading.Thread(target=lambda: asyncio.run(coro), daemon=True).start()

    def _button_click_animation(self, button_tag):
        try:
            self.canvas.itemconfig(button_tag, fill="#168a40")
            self.root.after(120, lambda: self._draw_control_icons(self.is_playing, hover_state=button_tag.split("_")[0]))
        except Exception:
            pass

    def _toggle_play_pause(self):
        if IS_WINDOWS:
            self._run_async(self._win_control("play_pause"))
        self._button_click_animation("play_btn")

    def _next_track(self):
        if IS_WINDOWS:
            self._run_async(self._win_control("next"))
        self._button_click_animation("next_btn")

    def _prev_track(self):
        if IS_WINDOWS:
            self._run_async(self._win_control("prev"))
        self._button_click_animation("prev_btn")


def main():
    if not IS_WINDOWS:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Unsupported OS", "This widget requires Windows for playback control.")
        return

    config_path = resource_path(CONFIG_FILE)
    if not os.path.exists(config_path):
        setup_root = tk.Tk()
        SetupWindow(setup_root)
        setup_root.mainloop()

    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        try:
            config = AppConfig(**raw)
        except TypeError:
            messagebox.showerror("Config Error", "Invalid config.json. Please delete it and restart.")
            return
        widget_root = tk.Tk()
        SpotifyWidget(widget_root, config)
        widget_root.mainloop()

if __name__ == "__main__":
    main()