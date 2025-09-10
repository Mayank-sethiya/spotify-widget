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
import queue

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
LOW_END_MODE = os.getenv("LOW_END_MODE", "0") == "1"
PIN_TO_DESKTOP = os.getenv("PIN_TO_DESKTOP", "1") == "1" if IS_WINDOWS else False

# Performance settings based on LOW_END_MODE
PROGRESS_BAR_FPS = 4 if LOW_END_MODE else 20
UI_REFRESH_DELAY = 250 if LOW_END_MODE else 50
NETWORK_POLL_DELAY = 1000 if LOW_END_MODE else 750

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    handlers=[logging.FileHandler("widget.log", mode="w", encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)

# Log configuration at startup
logging.info("Spotify Widget starting up")
logging.info(f"LOW_END_MODE: {LOW_END_MODE}")
logging.info(f"PIN_TO_DESKTOP: {PIN_TO_DESKTOP}")
logging.info(f"ENABLE_TRANSPARENCY: {ENABLE_TRANSPARENCY}")
logging.info(f"DEBUG: {DEBUG}")
logging.info(f"Progress bar FPS: {PROGRESS_BAR_FPS}")
logging.info(f"UI refresh delay: {UI_REFRESH_DELAY}ms")

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

        # Thread-safe UI update queue
        self.ui_queue = queue.Queue()
        self._process_ui_queue()

        # State tracking
        self.drag_locked = False
        self._last_x = 0
        self._last_y = 0
        self._pinned_to_desktop = False

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
        self._album_art_cache: Dict[str, Image.Image] = {}  # Cache album art by URL
        self._stop_event = threading.Event()
        self._overlay_cache: Dict[str, ImageTk.PhotoImage] = {}

        self._bind_events()
        self.update_ui_with_state()

        # Try to pin to desktop on Windows if enabled
        if IS_WINDOWS and PIN_TO_DESKTOP:
            self._pin_to_desktop()

        logging.info("Starting Spotify background thread")
        self._spotify_thread = threading.Thread(target=self._spotify_update_loop, daemon=True, name="SpotifyLoop")
        self._spotify_thread.start()
        self._animate_progress_bar()

    def _process_ui_queue(self):
        """Process UI updates from background threads safely on main thread"""
        try:
            while True:
                try:
                    update_func = self.ui_queue.get_nowait()
                    update_func()
                except queue.Empty:
                    break
        except Exception as e:
            logging.exception(f"UI queue processing error: {e}")
        finally:
            self.root.after(100, self._process_ui_queue)

    def _schedule_ui_update(self, update_func):
        """Thread-safe way to schedule UI updates from background threads"""
        self.ui_queue.put(update_func)

    def _bind_events(self):
        self.canvas.bind("<ButtonPress-1>", self._start_move)
        self.canvas.bind("<B1-Motion>", self._do_move)
        self.root.bind("<Enter>", self._on_enter)
        self.root.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-2>", self._force_refresh)
        self.canvas.bind("<Button-3>", self._show_context_menu)  # Right-click context menu
        # Resize with Ctrl+MouseWheel
        self.root.bind("<Control-Button-4>", self._resize_up)
        self.root.bind("<Control-Button-5>", self._resize_down)

    def _show_context_menu(self, event):
        """Show context menu with additional options"""
        try:
            context_menu = tk.Menu(self.root, tearoff=0)
            context_menu.add_command(label="Select Device", command=self._show_device_picker)
            context_menu.add_command(label="Refresh", command=self._force_refresh)
            context_menu.add_separator()
            context_menu.add_command(label=f"{'Unlock' if self.drag_locked else 'Lock'} Position", command=self._toggle_drag_lock)
            if IS_WINDOWS:
                pin_text = "Unpin from Desktop" if self._pinned_to_desktop else "Pin to Desktop"
                context_menu.add_command(label=pin_text, command=self._toggle_desktop_pin)
            context_menu.add_separator()
            context_menu.add_command(label="Exit", command=self._on_close)
            
            context_menu.post(event.x_root, event.y_root)
        except Exception as e:
            logging.exception(f"Context menu error: {e}")

    def _start_move(self, event):
        if not self.drag_locked:
            self._x, self._y = event.x, event.y

    def _do_move(self, event):
        if self.drag_locked:
            return
        try:
            deltax, deltay = event.x - self._x, event.y - self._y
            # Use Windows API for pinned windows, geometry for others
            if IS_WINDOWS and PIN_TO_DESKTOP:
                try:
                    import ctypes
                    from ctypes import wintypes
                    
                    # Throttle to ~60Hz for performance
                    current_time = time.time()
                    if hasattr(self, '_last_move_time') and current_time - self._last_move_time < 0.016:
                        return
                    self._last_move_time = current_time
                    
                    hwnd = self.root.winfo_id()
                    new_x = self.root.winfo_x() + deltax
                    new_y = self.root.winfo_y() + deltay
                    ctypes.windll.user32.SetWindowPos(hwnd, 0, new_x, new_y, 0, 0, 0x0001)
                except Exception as e:
                    logging.debug(f"Windows SetWindowPos failed, falling back to geometry: {e}")
                    self.root.geometry(f"+{self.root.winfo_x() + deltax}+{self.root.winfo_y() + deltay}")
            else:
                self.root.geometry(f"+{self.root.winfo_x() + deltax}+{self.root.winfo_y() + deltay}")
        except Exception as e:
            logging.warning(f"Move error: {e}")

    def _resize_up(self, event):
        """Resize widget larger with Ctrl+ScrollUp"""
        try:
            new_size = min(self.widget_size + 25, 500)
            if new_size != self.widget_size:
                self.widget_size = new_size
                self.root.geometry(f"{self.widget_size}x{self.widget_size}")
                self._album_art_cache_key = None  # Force refresh
                self.update_ui_with_state()
        except Exception as e:
            logging.exception(f"Resize up error: {e}")

    def _resize_down(self, event):
        """Resize widget smaller with Ctrl+ScrollDown"""
        try:
            new_size = max(self.widget_size - 25, 150)
            if new_size != self.widget_size:
                self.widget_size = new_size
                self.root.geometry(f"{self.widget_size}x{self.widget_size}")
                self._album_art_cache_key = None  # Force refresh
                self.update_ui_with_state()
        except Exception as e:
            logging.exception(f"Resize down error: {e}")

    def _on_enter(self, _event):
        # Controls are always visible, just update hover highlights
        pass

    def _on_leave(self, _event):
        # Controls stay visible, just remove hover highlights  
        self._draw_control_icons(self.is_playing, None)

    def _force_refresh(self, _event=None):
        def refresh_task():
            self._fetch_playback_data(schedule_ui=True)
        threading.Thread(target=refresh_task, daemon=True).start()

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
                cache_key = f"album_{self.widget_size}"
                if self._album_art_cache_key != cache_key:
                    rounded_art = create_rounded_image(pil_art, (self.widget_size, self.widget_size), 20)
                    self.tk_image_references["album_art"] = ImageTk.PhotoImage(rounded_art)
                    self._album_art_cache_key = cache_key
                self.canvas.create_image(0, 0, anchor="nw", image=self.tk_image_references["album_art"])
            else:
                self._draw_rounded_rect(0, 0, self.widget_size, self.widget_size, 20, "black", alpha=1.0)

            self._create_overlay_elements(song_info, is_playing)
        except Exception as e:
            logging.exception(f"UI update error: {e}")

    def _create_overlay_elements(self, song_info: Dict[str, Any], is_playing: bool):
        if song_info.get("song_name"):
            song_name_text = song_info.get("song_name", "")
            artist_name_text = song_info.get("artist_name", "")
        else:
            song_name_text = "Spotify Idle"
            artist_name_text = "Hover to control"

        # Draw overlay but keep it visible (not hidden like before)
        self._draw_rounded_rect(0, 0, self.widget_size, self.widget_size, 20, "black", alpha=0.5, tags="overlay")

        # Add close and lock icons in top-right corner
        self._draw_corner_icons()

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
        
        # Add resize grip in bottom-right corner
        self._draw_resize_grip()

    def _draw_corner_icons(self):
        """Draw close and lock icons in top-right corner"""
        self.canvas.delete("corner_icons")
        
        icon_size = max(16, self.widget_size // 15)
        margin = 8
        
        # Close icon (×)
        close_x = self.widget_size - margin - icon_size
        close_y = margin
        
        self.canvas.create_oval(
            close_x, close_y, close_x + icon_size, close_y + icon_size,
            fill="#ff4444", outline="#cc3333", width=1, tags=("corner_icons", "close_icon")
        )
        
        # Draw × symbol
        line_margin = icon_size // 4
        self.canvas.create_line(
            close_x + line_margin, close_y + line_margin,
            close_x + icon_size - line_margin, close_y + icon_size - line_margin,
            fill="white", width=2, tags=("corner_icons", "close_icon")
        )
        self.canvas.create_line(
            close_x + icon_size - line_margin, close_y + line_margin,
            close_x + line_margin, close_y + icon_size - line_margin,
            fill="white", width=2, tags=("corner_icons", "close_icon")
        )
        
        # Lock icon
        lock_x = close_x - icon_size - 6
        lock_y = margin
        
        lock_color = "#ff8800" if self.drag_locked else "#666666"
        self.canvas.create_oval(
            lock_x, lock_y, lock_x + icon_size, lock_y + icon_size,
            fill=lock_color, outline=lock_color, width=1, tags=("corner_icons", "lock_icon")
        )
        
        # Draw lock symbol (rectangle with arc on top)
        lock_inner = icon_size // 3
        lock_center_x = lock_x + icon_size // 2
        lock_center_y = lock_y + icon_size // 2
        
        # Lock body
        self.canvas.create_rectangle(
            lock_center_x - lock_inner//2, lock_center_y - lock_inner//4,
            lock_center_x + lock_inner//2, lock_center_y + lock_inner//2,
            fill="white", outline="white", tags=("corner_icons", "lock_icon")
        )
        
        # Lock shackle (arc)
        if self.drag_locked:
            # Closed lock - full arc
            self.canvas.create_arc(
                lock_center_x - lock_inner//2, lock_center_y - lock_inner//2,
                lock_center_x + lock_inner//2, lock_center_y + lock_inner//4,
                start=0, extent=180, outline="white", width=2, style="arc",
                tags=("corner_icons", "lock_icon")
            )
        else:
            # Open lock - partial arc
            self.canvas.create_arc(
                lock_center_x - lock_inner//2, lock_center_y - lock_inner//2,
                lock_center_x + lock_inner//2, lock_center_y + lock_inner//4,
                start=20, extent=140, outline="white", width=2, style="arc",
                tags=("corner_icons", "lock_icon")
            )
        
        # Bind click events
        self.canvas.tag_bind("close_icon", "<Button-1>", lambda e: self._on_close())
        self.canvas.tag_bind("lock_icon", "<Button-1>", lambda e: self._toggle_drag_lock())

    def _toggle_drag_lock(self):
        """Toggle drag lock state"""
        self.drag_locked = not self.drag_locked
        logging.info(f"Drag lock {'enabled' if self.drag_locked else 'disabled'}")
        self._draw_corner_icons()  # Refresh icon appearance

    def _draw_resize_grip(self):
        """Draw resize grip in bottom-right corner"""
        self.canvas.delete("resize_grip")
        
        grip_size = 12
        x = self.widget_size - grip_size - 2
        y = self.widget_size - grip_size - 2
        
        # Draw three diagonal lines as resize grip
        for i in range(3):
            offset = i * 3
            self.canvas.create_line(
                x + offset, y + grip_size,
                x + grip_size, y + offset,
                fill="#999999", width=1, tags="resize_grip"
            )

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

        # Bind click events for all platforms (not just Windows)
        self.canvas.tag_bind("prev_btn", "<Button-1>", lambda e: self._prev_track())
        self.canvas.tag_bind("play_btn", "<Button-1>", lambda e: self._toggle_play_pause())
        self.canvas.tag_bind("next_btn", "<Button-1>", lambda e: self._next_track())

    def _get_available_devices(self):
        """Get list of available Spotify Connect devices"""
        try:
            if self.sp:
                devices = self.sp.devices()
                return devices.get("devices", [])
        except Exception as e:
            logging.exception(f"Failed to get devices: {e}")
        return []

    def _show_device_picker(self):
        """Show device picker dialog"""
        devices = self._get_available_devices()
        if not devices:
            messagebox.showinfo("No Devices", "No Spotify Connect devices found. Make sure Spotify is running on at least one device.")
            return

        # Create device picker window
        device_window = tk.Toplevel(self.root)
        device_window.title("Select Spotify Device")
        device_window.geometry("300x200")
        device_window.resizable(False, False)
        device_window.attributes("-topmost", True)

        tk.Label(device_window, text="Select device for playback:", font=(FONT_NAME, 10)).pack(pady=10)

        # Device listbox
        listbox = tk.Listbox(device_window, font=(FONT_NAME, 9))
        listbox.pack(fill="both", expand=True, padx=10, pady=5)

        device_info = []
        for device in devices:
            name = device.get("name", "Unknown Device")
            device_type = device.get("type", "").title()
            is_active = device.get("is_active", False)
            status = " (Active)" if is_active else ""
            display_text = f"{name} - {device_type}{status}"
            listbox.insert(tk.END, display_text)
            device_info.append(device)

        def transfer_to_selected():
            try:
                selection = listbox.curselection()
                if selection:
                    device = device_info[selection[0]]
                    device_id = device.get("id")
                    if device_id:
                        self._transfer_playback(device_id)
                        device_window.destroy()
                else:
                    messagebox.showwarning("No Selection", "Please select a device.")
            except Exception as e:
                logging.exception(f"Device selection error: {e}")

        button_frame = tk.Frame(device_window)
        button_frame.pack(pady=10)
        
        tk.Button(button_frame, text="Transfer Playback", command=transfer_to_selected).pack(side="left", padx=5)
        tk.Button(button_frame, text="Cancel", command=device_window.destroy).pack(side="left", padx=5)

        # Select current active device if any
        for i, device in enumerate(device_info):
            if device.get("is_active"):
                listbox.selection_set(i)
                break

    def _transfer_playback(self, device_id: str):
        """Transfer playback to specified device"""
        def transfer_task():
            try:
                if self.sp:
                    self.sp.transfer_playback(device_id, force_play=True)
                    logging.info(f"Transferred playback to device: {device_id}")
                    # Refresh playback state after transfer
                    time.sleep(1)  # Give it a moment to transfer
                    self._fetch_playback_data(schedule_ui=True)
            except Exception as e:
                logging.exception(f"Failed to transfer playback: {e}")
                self._schedule_ui_update(lambda: messagebox.showerror("Transfer Failed", f"Could not transfer playback: {e}"))
        
        threading.Thread(target=transfer_task, daemon=True).start()

    def _pin_to_desktop(self):
        """Pin widget to Windows desktop (behind icons)"""
        if not IS_WINDOWS:
            return
            
        try:
            import ctypes
            from ctypes import wintypes
            
            # Get window handles
            hwnd = self.root.winfo_id()
            
            # Find the desktop WorkerW window
            def enum_windows_proc(hwnd, lparam):
                class_name = ctypes.create_unicode_buffer(256)
                ctypes.windll.user32.GetClassNameW(hwnd, class_name, 256)
                if class_name.value == "WorkerW":
                    # Check if this WorkerW contains the desktop listview
                    child_hwnd = ctypes.windll.user32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
                    if child_hwnd:
                        # This is the right WorkerW, store it
                        lparam.contents = wintypes.HWND(hwnd)
                        return False  # Stop enumeration
                return True  # Continue enumeration
            
            # Trigger creation of WorkerW by sending message to Program Manager
            progman = ctypes.windll.user32.FindWindowW("Progman", None)
            ctypes.windll.user32.SendMessageW(progman, 0x052C, 0xD, 0)
            ctypes.windll.user32.SendMessageW(progman, 0x052C, 0xD, 1)
            
            # Find the WorkerW window
            workerw = wintypes.HWND()
            enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            ctypes.windll.user32.EnumWindows(enum_proc(enum_windows_proc), ctypes.byref(workerw))
            
            if workerw.value:
                # Set our window as child of WorkerW
                ctypes.windll.user32.SetParent(hwnd, workerw.value)
                self._pinned_to_desktop = True
                logging.info("Successfully pinned to desktop")
            else:
                logging.warning("Could not find WorkerW window for desktop pinning")
                
        except Exception as e:
            logging.warning(f"Desktop pinning failed: {e}")

    def _unpin_from_desktop(self):
        """Unpin widget from Windows desktop"""
        if not IS_WINDOWS or not self._pinned_to_desktop:
            return
            
        try:
            import ctypes
            hwnd = self.root.winfo_id()
            # Remove from WorkerW and make it a normal window
            ctypes.windll.user32.SetParent(hwnd, 0)
            self._pinned_to_desktop = False
            logging.info("Unpinned from desktop")
        except Exception as e:
            logging.warning(f"Desktop unpinning failed: {e}")

    def _toggle_desktop_pin(self):
        """Toggle desktop pinning state"""
        if self._pinned_to_desktop:
            self._unpin_from_desktop()
    def _handle_spotify_api_error(self, error, action):
        """Handle Spotify API errors with user-friendly messages"""
        error_msg = str(error)
        if "No active device found" in error_msg:
            self._schedule_ui_update(lambda: messagebox.showwarning(
                "No Active Device", 
                f"Cannot {action}: No active Spotify device found.\n\nPlease:\n1. Open Spotify on any device\n2. Start playing something\n3. Try again, or use 'Select Device' to transfer playback."
            ))
        elif "Insufficient client scope" in error_msg:
            self._schedule_ui_update(lambda: messagebox.showerror(
                "Permission Error",
                f"Cannot {action}: Insufficient permissions.\n\nPlease restart the widget to re-authorize with required permissions."
            ))
        elif "Device not found" in error_msg:
            self._schedule_ui_update(lambda: messagebox.showwarning(
                "Device Error",
                f"Cannot {action}: Selected device is no longer available.\n\nTry selecting a different device."
            ))
        else:
            self._schedule_ui_update(lambda: messagebox.showerror(
                "Spotify Error",
                f"Cannot {action}: {error_msg[:100]}..."
            ))

    def _draw_rounded_rect(self, x1, y1, x2, y2, radius, fill, alpha=1.0, tags=""):
        width, height = (x2 - x1), (y2 - y1)
        cache_key = f"rect_{fill}_{alpha}_{width}x{height}_{radius}"
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
            scope = "user-read-playback-state user-modify-playback-state user-read-currently-playing"
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
            self._schedule_ui_update(self.update_ui_with_state)
            return

        while not self._stop_event.is_set():
            self._fetch_playback_data(schedule_ui=True)
            # Use performance-aware polling delay
            poll_delay = NETWORK_POLL_DELAY / 1000.0  # Convert to seconds
            for _ in range(int(poll_delay * 10)):
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
                    # Check cache first for performance
                    album_art_pil = self._album_art_cache.get(new_album_art_url)
                    if album_art_pil is None:
                        try:
                            resp = requests.get(new_album_art_url, timeout=NETWORK_TIMEOUT)
                            resp.raise_for_status()
                            album_art_pil = Image.open(io.BytesIO(resp.content)).convert("RGBA")
                            # Cache the album art
                            self._album_art_cache[new_album_art_url] = album_art_pil
                            # Limit cache size in LOW_END_MODE
                            if LOW_END_MODE and len(self._album_art_cache) > 5:
                                # Remove oldest entries
                                oldest_key = next(iter(self._album_art_cache))
                                del self._album_art_cache[oldest_key]
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
                # Use thread-safe UI update
                self._schedule_ui_update(self.update_ui_with_state)

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
            # Use performance-aware refresh rate
            refresh_delay = int(1000 / PROGRESS_BAR_FPS)
            self._progress_after_id = self.root.after(refresh_delay, self._animate_progress_bar)

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
        """Toggle play/pause using both Windows API and Spotify Web API"""
        def control_task():
            try:
                # Try Spotify Web API first
                if self.sp:
                    try:
                        playback = self.sp.current_playback()
                        if playback and playback.get("is_playing"):
                            self.sp.pause_playback()
                            logging.info("Paused via Spotify API")
                        else:
                            self.sp.start_playback()
                            logging.info("Started playback via Spotify API")
                    except Exception as e:
                        self._handle_spotify_api_error(e, "play/pause")
                        # Fallback to Windows API
                        if IS_WINDOWS:
                            asyncio.run(self._win_control("play_pause"))
                else:
                    # Fallback to Windows API
                    if IS_WINDOWS:
                        asyncio.run(self._win_control("play_pause"))
            except Exception as e:
                logging.exception(f"Play/pause control error: {e}")
        
        threading.Thread(target=control_task, daemon=True).start()
        self._button_click_animation("play_btn")

    def _next_track(self):
        """Skip to next track using both Windows API and Spotify Web API"""
        def control_task():
            try:
                # Try Spotify Web API first
                if self.sp:
                    try:
                        self.sp.next_track()
                        logging.info("Next track via Spotify API")
                    except Exception as e:
                        self._handle_spotify_api_error(e, "skip to next track")
                        # Fallback to Windows API
                        if IS_WINDOWS:
                            asyncio.run(self._win_control("next"))
                else:
                    # Fallback to Windows API
                    if IS_WINDOWS:
                        asyncio.run(self._win_control("next"))
            except Exception as e:
                logging.exception(f"Next track control error: {e}")
        
        threading.Thread(target=control_task, daemon=True).start()
        self._button_click_animation("next_btn")

    def _prev_track(self):
        """Skip to previous track using both Windows API and Spotify Web API"""
        def control_task():
            try:
                # Try Spotify Web API first
                if self.sp:
                    try:
                        self.sp.previous_track()
                        logging.info("Previous track via Spotify API")
                    except Exception as e:
                        self._handle_spotify_api_error(e, "skip to previous track")
                        # Fallback to Windows API
                        if IS_WINDOWS:
                            asyncio.run(self._win_control("prev"))
                else:
                    # Fallback to Windows API
                    if IS_WINDOWS:
                        asyncio.run(self._win_control("prev"))
            except Exception as e:
                logging.exception(f"Previous track control error: {e}")
        
        threading.Thread(target=control_task, daemon=True).start()
        self._button_click_animation("prev_btn")


def main():
    # Widget can now work on all platforms via Spotify Web API
    # Windows-specific features will be safely disabled on other platforms
    if not IS_WINDOWS:
        logging.info("Running on non-Windows platform - Windows media controls disabled, using Spotify Web API only")

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