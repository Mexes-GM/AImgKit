from __future__ import annotations

import os
import sys
import json
import gc
import random
import struct
import threading
import logging
import logging.handlers
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Windows AppUserModelID (must run BEFORE any Tk window) ──────────
if sys.platform == 'win32':
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'FastWatermark.WatermarkApp'
        )
    except Exception:
        pass

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk
from tkinterdnd2 import DND_FILES, TkinterDnD

from post_filters import apply_pipeline, DEFAULT_PIPELINE
from comfy_metadata import get_candidates_for_image, sanitize_for_filename
from core.formats import IMAGE_EXTS, VIDEO_EXTS
from core.watermark import compute_watermark_width, position_for_corner
from core.corner import CornerSelector
from core.naming import build_output_filename as _core_build_output_filename
from core.io_save import save_without_metadata, unique_save_path as _core_unique_save_path

# FIX-09: allow up to 300 MP; DecompressionBombError raised beyond this
Image.MAX_IMAGE_PIXELS = 300_000_000


def _config_dir():
    """Return (and create) per-user config/log directory."""
    d = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'FastWatermark')
    os.makedirs(d, exist_ok=True)
    return d


# ── Logging setup ──────────────────────────────────────────────────────────
_log_path = os.path.join(_config_dir(), 'fastwatermark.log')
_handler = logging.handlers.RotatingFileHandler(
    _log_path, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_handler])


# Configuration from original script
MARGIN = 10
WATERMARK_RELATIVE_WIDTH = 0.284
WATERMARK_MIN_RELATIVE = 0.06
WATERMARK_MAX_RELATIVE = 0.50
WATERMARK_OPACITY = 0.8
PREVIEW_PADDING = 20
THUMBNAIL_SIZE = 240
PROMPT_EXCERPT_LEN = 1500

class FastWatermarkApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Fast Watermark & Metadata Remover")
        self.root.geometry("1180x820")
        
        # Window icon (title bar + taskbar, all resolutions)
        self._set_window_icon()
        
        # Config file path — in per-user %LOCALAPPDATA%/FastWatermark (FIX-10)
        cfg_dir = self._config_dir()
        self.config_file = os.path.join(cfg_dir, 'watermark_config.json')
        self.library_file = os.path.join(cfg_dir, 'character_library.json')

        # FIX-10 migration: copy old config from app dir if new location is empty
        _old_cfg = os.path.join(self._app_dir(), 'watermark_config.json')
        if os.path.exists(_old_cfg) and not os.path.exists(self.config_file):
            import shutil
            try:
                shutil.copy2(_old_cfg, self.config_file)
                logging.info("Migrated config from %s to %s", _old_cfg, self.config_file)
            except Exception:
                pass
        
        self.files_to_process = []
        self.watermark_path = tk.StringVar()
        self.output_dir = tk.StringVar()  # empty = <input_dir>/watermarked_clean
        self.status_var = tk.StringVar(value="Ready")

        # FIX-01
        self._reserved_paths: set = set()
        self._reserved_lock = threading.Lock()
        # FIX-03
        self.processing = False
        self.cancel_event = threading.Event()
        
        # New watermark customization variables
        self.watermark_size = tk.DoubleVar(value=WATERMARK_RELATIVE_WIDTH)
        self.watermark_opacity = tk.DoubleVar(value=WATERMARK_OPACITY)
        self.watermark_corner = tk.StringVar(value="bottom-left")
        self.randomize_corner = tk.BooleanVar(value=False)
        self.corner_selector = CornerSelector()

        # ===== Post-processing filter variables =====
        self.pp_enabled = tk.BooleanVar(value=DEFAULT_PIPELINE["enabled"])
        self.pp_upscale = tk.DoubleVar(value=DEFAULT_PIPELINE["upscale"])
        self.pp_kuwahara_radius = tk.IntVar(value=DEFAULT_PIPELINE["kuwahara_radius"])
        self.pp_median_size = tk.IntVar(value=DEFAULT_PIPELINE["median_size"])
        self.pp_downscale = tk.DoubleVar(value=DEFAULT_PIPELINE["downscale"])
        self.pp_noise_strength = tk.DoubleVar(value=DEFAULT_PIPELINE["noise_strength"])
        self.pp_noise_mono = tk.BooleanVar(value=DEFAULT_PIPELINE["noise_monochromatic"])
        self.pp_noise_invert = tk.BooleanVar(value=DEFAULT_PIPELINE["noise_invert"])
        self.pp_noise_channels = tk.StringVar(value=DEFAULT_PIPELINE["noise_channels"])

        # ===== Auto-naming variables =====
        self.autoname_enabled = tk.BooleanVar(value=False)
        # Map file_path -> list of chosen characters (up to 2, filled by dialog before worker)
        self.autoname_map = {}
        
        # Cache for preview image
        self.preview_photo = None
        self._wm_cache: tuple = ()
        self._save_after_id = None

        # Character library — learned tags from user selections
        # { "tag_lowercase": {"tag": "original casing", "count": N, "last_used": "ISO date"} }
        self.character_library = {}

        # Per-character index for auto-naming (filled at processing time)
        self.autoname_counters = defaultdict(int)
        
        # Load saved options
        self.load_options()
        self._load_character_library()
        
        # Try to find default watermark
        default_wm = os.path.join(os.path.dirname(__file__), "watermark.png")
        if os.path.exists(default_wm) and not self.watermark_path.get():
            self.watermark_path.set(default_wm)

        self.create_widgets()
        
        # Save options when closing
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _set_window_icon(self):
        """Load multi-resolution .ico and set window icon correctly."""
        icon_path = os.path.join(self._app_dir(), "icon.ico")
        if not os.path.exists(icon_path):
            return
        try:
            # Title bar icon (16x16)
            self.root.iconbitmap(bitmap=icon_path)
            # Taskbar/Alt+Tab icon (all resolutions via Tcl)
            photos = self._load_ico_photos(icon_path)
            if photos:
                self.root.tk.call('wm', 'iconphoto', self.root._w,
                                  '-default', *photos)
        except Exception:
            pass

    @staticmethod
    def _config_dir() -> str:
        """Return (and create) per-user config/log directory."""
        return _config_dir()

    @staticmethod
    def _app_dir():
        """Directory containing the script or frozen executable."""
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    @staticmethod
    def _load_ico_photos(ico_path):
        """Parse ICO binary and return list of PhotoImage for all sizes."""
        photos = []
        with open(ico_path, 'rb') as f:
            data = f.read()
        # ICO header: reserved(2) + type(2) + count(2)
        count = struct.unpack_from('<H', data, 4)[0]
        for i in range(count):
            off = 6 + i * 16
            w, h = data[off], data[off + 1]
            w = 256 if w == 0 else w
            h = 256 if h == 0 else h
            size = struct.unpack_from('<I', data, off + 8)[0]
            img_off = struct.unpack_from('<I', data, off + 12)[0]
            # Read BMP data (skip 40-byte BITMAPINFOHEADER)
            bmp_data = data[img_off:img_off + size]
            # The BMP inside ICO is height*2 (includes AND mask)
            bmp_h = struct.unpack_from('<i', bmp_data, 8)[0] // 2
            # Read 32-bit BGRA pixels
            pixel_data = bmp_data[40:40 + w * bmp_h * 4]
            # Convert BGRA → RGBA for PPM → PhotoImage
            ppm = b'P6\n%d %d\n255\n' % (w, h)
            rgba = bytearray(w * h * 3)
            for row in range(h):
                src_row = (bmp_h - 1 - row) * w * 4
                dst_row = (h - 1 - row) * w * 3
                for x in range(w):
                    b = pixel_data[src_row + x * 4]
                    g = pixel_data[src_row + x * 4 + 1]
                    r = pixel_data[src_row + x * 4 + 2]
                    rgba[dst_row + x * 3] = r
                    rgba[dst_row + x * 3 + 1] = g
                    rgba[dst_row + x * 3 + 2] = b
            photo = tk.PhotoImage(data=ppm + bytes(rgba))
            photos.append(photo)
        return photos

    def create_widgets(self):
        # Create main frames
        left_frame = tk.Frame(self.root)
        left_frame.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        
        right_frame = tk.Frame(self.root, width=460)
        right_frame.pack(side="right", fill="both", expand=False, padx=10, pady=10)
        right_frame.pack_propagate(False)

        # ===== LEFT SIDE =====
        
        # Watermark Selection
        frame_wm = tk.LabelFrame(left_frame, text="Watermark Image", padx=10, pady=10)
        frame_wm.pack(fill="x", pady=5)
        
        entry_wm = tk.Entry(frame_wm, textvariable=self.watermark_path, width=50)
        entry_wm.pack(side="left", fill="x", expand=True)
        
        # Enable Drag & Drop for Watermark
        entry_wm.drop_target_register(DND_FILES)
        entry_wm.dnd_bind('<<Drop>>', self.drop_watermark)
        
        btn_wm = tk.Button(frame_wm, text="Browse...", command=self.browse_watermark)
        btn_wm.pack(side="right", padx=5)

        # Output directory
        frame_out = tk.LabelFrame(left_frame, text="Output folder (empty = <source>/watermarked_clean)",
                                  padx=10, pady=10)
        frame_out.pack(fill="x", pady=5)
        entry_out = tk.Entry(frame_out, textvariable=self.output_dir, width=50)
        entry_out.pack(side="left", fill="x", expand=True)
        entry_out.drop_target_register(DND_FILES)
        entry_out.dnd_bind('<<Drop>>', self.drop_output_dir)
        tk.Button(frame_out, text="Browse...",
                  command=self.browse_output_dir).pack(side="right", padx=5)
        tk.Button(frame_out, text="Clear",
                  command=lambda: self.output_dir.set("")).pack(side="right")

        # Drop Zone (Listbox)
        frame_drop = tk.LabelFrame(left_frame, text="Drag & Drop Images Here", padx=10, pady=10)
        frame_drop.pack(fill="both", expand=True, pady=5)

        self.file_listbox = tk.Listbox(frame_drop, selectmode=tk.EXTENDED)
        self.file_listbox.pack(fill="both", expand=True, side="left")
        
        scrollbar = tk.Scrollbar(frame_drop, orient="vertical", command=self.file_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.file_listbox.config(yscrollcommand=scrollbar.set)

        # Enable Drag & Drop for Listbox
        self.file_listbox.drop_target_register(DND_FILES)
        self.file_listbox.dnd_bind('<<Drop>>', self.drop_files)

        # Clear List Button
        btn_clear = tk.Button(left_frame, text="Clear List", command=self.clear_list)
        btn_clear.pack(pady=5)

        # Progress Bar
        self.progress = ttk.Progressbar(left_frame, orient="horizontal", length=400, mode="determinate")
        self.progress.pack(pady=10, fill="x")

        # Status Label
        lbl_status = tk.Label(left_frame, textvariable=self.status_var)
        lbl_status.pack()

        # Process Button
        btn_process = tk.Button(left_frame, text="Start Batch Processing", command=self.start_processing_thread, bg="#4CAF50", fg="white", font=("Arial", 12, "bold"))
        btn_process.pack(pady=10, ipadx=20, ipady=5)

        # ===== RIGHT SIDE (tabbed) =====
        notebook = ttk.Notebook(right_frame)
        notebook.pack(fill="both", expand=True)

        wm_tab = tk.Frame(notebook)
        pp_tab = tk.Frame(notebook)
        an_tab = tk.Frame(notebook)
        notebook.add(wm_tab, text="Watermark")
        notebook.add(pp_tab, text="Post-Processing")
        notebook.add(an_tab, text="Auto-Name")

        # ---- Watermark tab ----
        # Watermark Size
        frame_size = tk.LabelFrame(wm_tab, text="Watermark Size", padx=10, pady=10)
        frame_size.pack(fill="x", pady=5)
        tk.Label(frame_size, text="Size (%):").pack(anchor="w")
        self.size_scale = tk.Scale(frame_size, from_=6, to=50, orient="horizontal",
                                    variable=self.watermark_size, command=self.update_preview)
        self.size_scale.pack(fill="x", pady=5)
        self.size_label = tk.Label(frame_size, text=f"28.4%")
        self.size_label.pack(anchor="w")

        # Watermark Opacity
        frame_opacity = tk.LabelFrame(wm_tab, text="Transparency", padx=10, pady=10)
        frame_opacity.pack(fill="x", pady=5)
        tk.Label(frame_opacity, text="Opacity:").pack(anchor="w")
        self.opacity_scale = tk.Scale(frame_opacity, from_=0, to=100, orient="horizontal",
                                       variable=self.watermark_opacity, command=self.update_preview)
        self.opacity_scale.pack(fill="x", pady=5)
        self.opacity_label = tk.Label(frame_opacity, text=f"80%")
        self.opacity_label.pack(anchor="w")

        # Watermark Position
        frame_position = tk.LabelFrame(wm_tab, text="Position", padx=10, pady=10)
        frame_position.pack(fill="x", pady=5)
        tk.Label(frame_position, text="Corner:").pack(anchor="w")
        corner_frame = tk.Frame(frame_position)
        corner_frame.pack(fill="x", pady=5)
        corners = [("Bot. Left", "bottom-left"), ("Bot. Right", "bottom-right"),
                   ("Top Left", "top-left"), ("Top Right", "top-right")]
        for label, value in corners:
            tk.Radiobutton(corner_frame, text=label, variable=self.watermark_corner,
                          value=value, command=self.update_preview).pack(anchor="w")
        tk.Checkbutton(frame_position, text="Randomize corner",
                      variable=self.randomize_corner, command=self.update_preview).pack(anchor="w", pady=5)

        # Preview
        frame_preview = tk.LabelFrame(wm_tab, text="Preview", padx=10, pady=10)
        frame_preview.pack(fill="both", expand=True, pady=5)
        self.preview_canvas = tk.Canvas(frame_preview, bg="#f0f0f0", highlightthickness=1, highlightbackground="#cccccc")
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_canvas.bind("<Configure>", self.on_preview_resize)

        # ---- Post-Processing tab ----
        self._build_postprocessing_tab(pp_tab)
        # ---- Auto-Naming tab ----
        self._build_autoname_tab(an_tab)
        
        # Add traces to save options when modified
        for v in (self.watermark_path, self.output_dir,
                  self.watermark_size, self.watermark_opacity,
                  self.watermark_corner, self.randomize_corner,
                  self.pp_enabled, self.pp_upscale, self.pp_kuwahara_radius,
                  self.pp_median_size, self.pp_downscale, self.pp_noise_strength,
                  self.pp_noise_mono, self.pp_noise_invert, self.pp_noise_channels,
                  self.autoname_enabled):
            v.trace_add('write', lambda *args: self.on_option_changed())

    def load_options(self):
        """Load saved options from config file."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    
                # Restore saved options
                if "watermark_path" in config:
                    self.watermark_path.set(config["watermark_path"])
                if "watermark_size" in config:
                    self.watermark_size.set(config["watermark_size"])
                if "watermark_opacity" in config:
                    self.watermark_opacity.set(config["watermark_opacity"])
                if "watermark_corner" in config:
                    self.watermark_corner.set(config["watermark_corner"])
                if "randomize_corner" in config:
                    self.randomize_corner.set(config["randomize_corner"])
                if "output_dir" in config:
                    self.output_dir.set(config["output_dir"])

                # Post-processing
                pp = config.get("post_processing", {}) or {}
                if "enabled" in pp: self.pp_enabled.set(pp["enabled"])
                if "upscale" in pp: self.pp_upscale.set(pp["upscale"])
                if "kuwahara_radius" in pp: self.pp_kuwahara_radius.set(pp["kuwahara_radius"])
                if "median_size" in pp: self.pp_median_size.set(pp["median_size"])
                if "downscale" in pp: self.pp_downscale.set(pp["downscale"])
                if "noise_strength" in pp: self.pp_noise_strength.set(pp["noise_strength"])
                if "noise_monochromatic" in pp: self.pp_noise_mono.set(pp["noise_monochromatic"])
                if "noise_invert" in pp: self.pp_noise_invert.set(pp["noise_invert"])
                if "noise_channels" in pp: self.pp_noise_channels.set(pp["noise_channels"])
                # Auto-name
                if "autoname_enabled" in config:
                    self.autoname_enabled.set(config["autoname_enabled"])

                logging.info("Options loaded from %s", self.config_file)
            except Exception as e:
                logging.exception("Error loading options: %s", e)

    def _load_character_library(self):
        """Load learned character tags from JSON file."""
        if os.path.exists(self.library_file):
            try:
                with open(self.library_file, 'r') as f:
                    data = json.load(f)
                self.character_library = data.get("characters", {})
                logging.info("Character library loaded: %d tags", len(self.character_library))
            except Exception as e:
                logging.exception("Error loading character library: %s", e)
                self.character_library = {}

    def _save_character_library(self):
        """Persist learned character tags to JSON file."""
        try:
            data = {"characters": self.character_library}
            with open(self.library_file, 'w') as f:
                json.dump(data, f, indent=2)
            logging.info("Character library saved: %d tags", len(self.character_library))
        except Exception as e:
            logging.exception("Error saving character library: %s", e)

    def save_options(self):
        """Save current options to config file."""
        try:
            config = {
                "watermark_path": self.watermark_path.get(),
                "watermark_size": self.watermark_size.get(),
                "watermark_opacity": self.watermark_opacity.get(),
                "watermark_corner": self.watermark_corner.get(),
                "randomize_corner": self.randomize_corner.get(),
                "output_dir": self.output_dir.get(),
                "autoname_enabled": self.autoname_enabled.get(),
                "post_processing": {
                    "enabled": self.pp_enabled.get(),
                    "upscale": self.pp_upscale.get(),
                    "kuwahara_radius": self.pp_kuwahara_radius.get(),
                    "median_size": self.pp_median_size.get(),
                    "downscale": self.pp_downscale.get(),
                    "noise_strength": self.pp_noise_strength.get(),
                    "noise_monochromatic": self.pp_noise_mono.get(),
                    "noise_invert": self.pp_noise_invert.get(),
                    "noise_channels": self.pp_noise_channels.get(),
                },
            }
            
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)

            logging.info("Options saved to %s", self.config_file)
        except Exception as e:
            logging.exception("Error saving options: %s", e)

    def on_closing(self):
        """Save options and close application."""
        if self.processing:
            if not messagebox.askyesno("Procesando",
                                       "Hay un proceso en curso. ¿Cerrar de todos modos?"):
                return
            self.cancel_event.set()
            self.root.after(200, self.root.destroy)
            return
        self.save_options()
        self.root.destroy()

    def on_option_changed(self):
        """Called when any option changes."""
        self.update_preview()
        if self._save_after_id:
            self.root.after_cancel(self._save_after_id)
        self._save_after_id = self.root.after(500, self.save_options)

    def drop_files(self, event):
        # Parse the dropped data using Tkinter's splitlist to handle spaces and braces
        files = self.root.tk.splitlist(event.data)
        for f in files:
            if os.path.isfile(f):
                ext = os.path.splitext(f)[1].lower()
                if ext in IMAGE_EXTS | VIDEO_EXTS:
                    if f not in self.files_to_process:
                        self.files_to_process.append(f)
                        self.file_listbox.insert(tk.END, f)
            elif os.path.isdir(f):
                for root_dir, _, filenames in os.walk(f):
                    for filename in filenames:
                        if os.path.splitext(filename)[1].lower() in IMAGE_EXTS | VIDEO_EXTS:
                            full_path = os.path.join(root_dir, filename)
                            if full_path not in self.files_to_process:
                                self.files_to_process.append(full_path)
                                self.file_listbox.insert(tk.END, full_path)


    def drop_watermark(self, event):
        path = self.clean_path(event.data)
        if os.path.isfile(path):
            self.watermark_path.set(path)

    def _render_watermark_preview(self, watermark: Image.Image,
                                   canvas_w: int, canvas_h: int) -> None:
        """Compose and draw watermark onto the preview canvas."""
        padding = PREVIEW_PADDING
        pw = canvas_w - padding
        ph = canvas_h - padding
        if pw <= 0 or ph <= 0:
            return

        preview_img = Image.new("RGBA", (pw, ph), (200, 200, 200, 255))
        wm_w, wm_h = watermark.size
        size_pct = self.watermark_size.get()
        target_w = compute_watermark_width(pw, size_pct)

        if target_w > 0 and wm_w > 0:
            scale = target_w / wm_w
            wm = watermark.resize((int(wm_w * scale), int(wm_h * scale)), resample=Image.LANCZOS)
            opacity = self.watermark_opacity.get()
            if opacity < 100:
                wm = wm.copy()
                wm.putalpha(wm.split()[3].point(lambda p: int(p * opacity / 100)))
            corner = self.watermark_corner.get()
            pos = position_for_corner(pw, ph, wm.width, wm.height, corner)
            preview_img.paste(wm, pos, wm)

        self.preview_photo = ImageTk.PhotoImage(preview_img)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(padding // 2, padding // 2,
                                         image=self.preview_photo, anchor="nw")
        self.size_label.config(text=f"{size_pct:.1f}%")
        self.opacity_label.config(text=f"{self.watermark_opacity.get():.0f}%")

        # UX-02: fidelity notes
        note_y = canvas_h - padding // 2
        note_x = padding // 2 + 4
        if self.randomize_corner.get():
            self.preview_canvas.create_text(note_x, note_y,
                text="↺ Random corner per image", fill="#888", anchor="sw")
        if self.pp_enabled.get():
            offset = 16 if self.randomize_corner.get() else 0
            self.preview_canvas.create_text(note_x, note_y - offset,
                text="⚠ Preview approx — post-processing not shown", fill="#888", anchor="sw")

    def update_preview(self, *args) -> None:
        """Update real-time preview."""
        wm_path = self.watermark_path.get()
        if not wm_path or not os.path.exists(wm_path):
            self.preview_canvas.delete("all")
            self.preview_canvas.create_text(
                self.preview_canvas.winfo_width() // 2,
                self.preview_canvas.winfo_height() // 2,
                text="Loading watermark...", fill="#666666")
            return

        try:
            cw = max(self.preview_canvas.winfo_width(), 300)
            ch = max(self.preview_canvas.winfo_height(), 300)

            mtime = os.path.getmtime(wm_path)
            if self._wm_cache and self._wm_cache[:2] == (wm_path, mtime):
                watermark = self._wm_cache[2]
            else:
                watermark = Image.open(wm_path).convert('RGBA')
                self._wm_cache = (wm_path, mtime, watermark)

            self._render_watermark_preview(watermark, cw, ch)
        except Exception as e:
            self.preview_canvas.delete("all")
            self.preview_canvas.create_text(
                self.preview_canvas.winfo_width() // 2,
                self.preview_canvas.winfo_height() // 2,
                text=f"Error: {e}", fill="#ff0000")

    def on_preview_resize(self, event):
        """Fires when preview canvas resizes."""
        self.update_preview()

    def get_watermark_position(self, base_img: Image.Image, watermark_img: Image.Image, corner: str) -> tuple[int, int]:
        """Calculate watermark position based on selected corner."""
        return position_for_corner(base_img.width, base_img.height,
                                   watermark_img.width, watermark_img.height,
                                   corner, MARGIN)

    def clean_path(self, path):
        path = path.strip()
        if path.startswith('{') and path.endswith('}'):
            path = path[1:-1]
        return path

    def clear_list(self):
        self.files_to_process = []
        self.file_listbox.delete(0, tk.END)

    def browse_watermark(self):
        file_path = filedialog.askopenfilename(filetypes=[("Images", "*.png;*.jpg;*.jpeg")])
        if file_path:
            self.watermark_path.set(file_path)

    def browse_output_dir(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.output_dir.set(d)

    def drop_output_dir(self, event):
        path = self.clean_path(event.data)
        if os.path.isdir(path):
            self.output_dir.set(path)

    # ------------------------------------------------------------------
    # Tab builders
    # ------------------------------------------------------------------
    def _build_postprocessing_tab(self, parent):
        tk.Checkbutton(parent, text="Enable Post-Processing",
                       variable=self.pp_enabled).pack(anchor="w", padx=10, pady=8)

        # Upscale
        f = tk.LabelFrame(parent, text="1) Resize Relative (Upscale)", padx=10, pady=6)
        f.pack(fill="x", padx=10, pady=4)
        tk.Label(f, text="Scale (W=H):").pack(anchor="w")
        tk.Scale(f, from_=1.0, to=4.0, resolution=0.1, orient="horizontal",
                 variable=self.pp_upscale).pack(fill="x")

        # Kuwahara
        f = tk.LabelFrame(parent, text="2) Kuwahara Blur (mean)", padx=10, pady=6)
        f.pack(fill="x", padx=10, pady=4)
        tk.Label(f, text="Radius (0 = disabled):").pack(anchor="w")
        tk.Scale(f, from_=0, to=8, orient="horizontal",
                 variable=self.pp_kuwahara_radius).pack(fill="x")

        # Median
        f = tk.LabelFrame(parent, text="3) Median Filter", padx=10, pady=6)
        f.pack(fill="x", padx=10, pady=4)
        tk.Label(f, text="Size (kernel = 2*size+1, 0 = disabled):").pack(anchor="w")
        tk.Scale(f, from_=0, to=5, orient="horizontal",
                 variable=self.pp_median_size).pack(fill="x")

        # Downscale
        f = tk.LabelFrame(parent, text="4) Resize Relative (Downscale)", padx=10, pady=6)
        f.pack(fill="x", padx=10, pady=4)
        tk.Label(f, text="Scale (W=H):").pack(anchor="w")
        tk.Scale(f, from_=0.1, to=2.0, resolution=0.05, orient="horizontal",
                 variable=self.pp_downscale).pack(fill="x")

        # Noise
        f = tk.LabelFrame(parent, text="5) Gaussian Noise", padx=10, pady=6)
        f.pack(fill="x", padx=10, pady=4)
        tk.Label(f, text="Strength (0 = disabled):").pack(anchor="w")
        tk.Scale(f, from_=0.0, to=0.5, resolution=0.01, orient="horizontal",
                 variable=self.pp_noise_strength).pack(fill="x")
        tk.Checkbutton(f, text="Monochromatic", variable=self.pp_noise_mono).pack(anchor="w")
        tk.Checkbutton(f, text="Invert", variable=self.pp_noise_invert).pack(anchor="w")
        ch_frame = tk.Frame(f); ch_frame.pack(anchor="w")
        tk.Label(ch_frame, text="Channels:").pack(side="left")
        ttk.Combobox(ch_frame, textvariable=self.pp_noise_channels, width=6,
                     values=("rgb", "r", "g", "b", "rg", "rb", "gb"),
                     state="readonly").pack(side="left", padx=4)

        tk.Button(parent, text="Reset to defaults",
                  command=self._reset_postprocessing).pack(pady=8)

    def _reset_postprocessing(self):
        d = DEFAULT_PIPELINE
        self.pp_enabled.set(d["enabled"])
        self.pp_upscale.set(d["upscale"])
        self.pp_kuwahara_radius.set(d["kuwahara_radius"])
        self.pp_median_size.set(d["median_size"])
        self.pp_downscale.set(d["downscale"])
        self.pp_noise_strength.set(d["noise_strength"])
        self.pp_noise_mono.set(d["noise_monochromatic"])
        self.pp_noise_invert.set(d["noise_invert"])
        self.pp_noise_channels.set(d["noise_channels"])

    def _build_autoname_tab(self, parent):
        tk.Checkbutton(parent, text="Enable metadata auto-naming",
                       variable=self.autoname_enabled).pack(anchor="w", padx=10, pady=8)
        tk.Label(parent, text="Note: Auto-naming applies to images only (not video).",
                 fg="#888", font=("Arial", 8)).pack(anchor="w", padx=10)
        info = (
            "When enabled, before processing PNG images with metadata,\n"
            "a dialog will show candidate tags extracted from the\n"
            "positive prompt (tags that appear before '1girl' / '1boy',\n"
            "after LoRA triggers).\n\n"
            "Check all character tags that apply, or type your own\n"
            "(comma-separated), or press 'Skip' to keep the\n"
            "original filename.\n\n"
            "The original name is REPLACED with:\n"
            "  <char1>_<N>.png\n"
            "  <char1>+<char2>+<char3>_<N>.png\n"
            "Characters are joined with '+' for easy automated parsing.\n"
            "<N> is a per-combination counter within the batch."
        )
        tk.Label(parent, text=info, justify="left", anchor="w",
                 wraplength=420).pack(fill="x", padx=10, pady=6)

        # Library status + manage button
        lib_frame = tk.Frame(parent)
        lib_frame.pack(fill="x", padx=10, pady=6)
        self.lib_status_lbl = tk.Label(lib_frame, text="", fg="#555")
        self.lib_status_lbl.pack(side="left")
        tk.Button(lib_frame, text="Manage Library",
                  command=self._manage_library).pack(side="right")
        self._update_library_status()

    def _update_library_status(self):
        n = len(self.character_library)
        if n == 0:
            self.lib_status_lbl.config(
                text="📚 No learned characters yet. Selected tags will be auto-learned.")
        else:
            top = sorted(self.character_library.values(),
                         key=lambda x: -x["count"])[:3]
            names = ", ".join(e["tag"] for e in top)
            self.lib_status_lbl.config(
                text=f"📚 {n} learned: {names}...")

    def _manage_library(self):
        """Dialog to view and remove learned character tags."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Character Library")
        dlg.geometry("500x520")
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(dlg, text="Learned character tags (★ = auto-selected in future)",
                 font=("Arial", 10, "bold")).pack(pady=10)

        list_frame = tk.Frame(dlg)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)

        # Sort by count desc
        entries = sorted(self.character_library.items(),
                         key=lambda kv: -kv[1]["count"])

        if not entries:
            tk.Label(list_frame, text="No characters learned yet. Select tags during\n"
                     "auto-naming and they'll appear here.").pack(pady=20)
        else:
            canvas = tk.Canvas(list_frame, highlightthickness=0)
            scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
            scroll_frame = tk.Frame(canvas)

            scroll_frame.bind("<Configure>",
                              lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)

            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            for key, entry in entries:
                row = tk.Frame(scroll_frame)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=f"★ {entry['tag']}",
                         font=("Arial", 10, "bold"), fg="#2e7d32",
                         width=30, anchor="w").pack(side="left")
                tk.Label(row, text=f"×{entry['count']}",
                         fg="#888", width=6).pack(side="left")
                btn = tk.Button(row, text="✕", fg="#c0392b",
                                command=lambda k=key: self._remove_from_library(k, dlg))
                btn.pack(side="right", padx=2)

        # Buttons
        btns = tk.Frame(dlg)
        btns.pack(fill="x", padx=10, pady=10)
        tk.Button(btns, text="Clear All",
                  command=lambda: self._clear_library(dlg),
                  fg="#c0392b").pack(side="left")
        tk.Button(btns, text="Close", command=dlg.destroy,
                  width=10).pack(side="right")

    def _remove_from_library(self, key, dialog):
        """Remove a single tag from the library."""
        if key in self.character_library:
            del self.character_library[key]
            self._save_character_library()
            self._update_library_status()
            dialog.destroy()
            self._manage_library()  # reopen to refresh

    def _clear_library(self, dialog):
        """Clear all learned tags after confirmation."""
        if messagebox.askyesno("Clear Library",
                               "Remove ALL learned character tags?\nThis cannot be undone.",
                               parent=dialog):
            self.character_library.clear()
            self._save_character_library()
            self._update_library_status()
            dialog.destroy()
            self._manage_library()  # reopen to refresh

    # ------------------------------------------------------------------
    # Auto-name dialog flow
    # ------------------------------------------------------------------
    def _build_candidate_checkboxes(self, parent: tk.Frame, candidates: list[str]) -> tuple[list, tk.Label]:
        """Populate candidate checkboxes inside parent frame; return (check_vars, count_lbl)."""
        check_vars: list[tuple[tk.BooleanVar, str]] = []
        count_lbl = tk.Label(parent, text="", fg="#666")
        count_lbl.pack(anchor="w")

        def _update_count():
            n = sum(1 for v, _ in check_vars if v.get())
            count_lbl.config(text="" if n == 0 else
                             ("✓ 1 character selected" if n == 1 else f"✓ {n} characters selected"))

        if not candidates:
            tk.Label(parent, text="(no candidates detected)").pack()
            return check_vars, count_lbl

        def _sort_key(tag: str):
            e = self.character_library.get(tag.lower())
            return (0, -e.get("count", 0), -len(tag)) if e else (1, 0, -len(tag))

        sorted_candidates = sorted(candidates, key=_sort_key)
        known = [(c, self.character_library[c.lower()]) for c in sorted_candidates
                 if c.lower() in self.character_library]
        auto_select: set[str] = set()
        if known:
            best_tag, best_entry = known[0]
            best_count = best_entry.get("count", 0)
            second_count = known[1][1].get("count", 0) if len(known) > 1 else 0
            if best_count >= 1 and best_count >= second_count * 2:
                auto_select.add(best_tag.lower())

        for c in sorted_candidates:
            lib = self.character_library.get(c.lower())
            label = f"★ {c}  (×{lib['count']})" if lib else c
            var = tk.BooleanVar(value=c.lower() in auto_select)
            check_vars.append((var, c))
            cb = tk.Checkbutton(parent, text=label, variable=var, anchor="w", command=_update_count)
            if lib:
                cb.config(fg="#2e7d32", font=("Arial", 9, "bold"))
            cb.pack(fill="x", anchor="w")
        _update_count()
        return check_vars, count_lbl

    def _build_dialog_buttons(self, parent: tk.Frame, result: dict, cancel_all: dict,
                               check_vars: list, custom_var: tk.StringVar,
                               dialog: tk.Toplevel) -> None:
        """Pack Apply / Skip / Cancel buttons into parent frame."""
        def on_apply():
            custom_text = custom_var.get().strip()
            result["value"] = (
                [p.strip() for p in custom_text.split(",") if p.strip()]
                if custom_text else [t for v, t in check_vars if v.get()]
            )
            dialog.destroy()

        def on_skip():
            result["value"] = []
            dialog.destroy()

        def on_cancel_all():
            cancel_all["value"] = True
            dialog.destroy()

        tk.Button(parent, text="Apply", command=on_apply, width=14,
                  bg="#4CAF50", fg="white").pack(side="right", padx=4)
        tk.Button(parent, text="Skip", command=on_skip, width=10).pack(side="right", padx=4)
        tk.Button(parent, text="Cancel batch", command=on_cancel_all,
                  width=14).pack(side="left", padx=4)

    def _prompt_character_for_image(self, image_path: str, candidates: list[str],
                                     prompt_text: str | None) -> list[str] | None:
        """Modal dialog: returns chosen character names, [] to skip, None to cancel batch."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Select characters")
        dialog.geometry("720x560")
        dialog.transient(self.root)
        dialog.grab_set()

        result: dict = {"value": []}
        cancel_all: dict = {"value": False}

        # Thumbnail + filename
        top = tk.Frame(dialog)
        top.pack(fill="x", padx=10, pady=10)
        try:
            with Image.open(image_path) as im:
                im.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE))
                photo = ImageTk.PhotoImage(im.copy())
            lbl = tk.Label(top, image=photo)
            lbl.image = photo
            lbl.pack(side="left")
        except Exception:
            tk.Label(top, text="(no preview)").pack(side="left")
        tk.Label(top, text=os.path.basename(image_path), font=("Arial", 11, "bold"),
                 wraplength=420, justify="left").pack(side="left", padx=10)

        # Candidate checkboxes
        f_cand = tk.LabelFrame(dialog, text="Candidate tags — check all that apply",
                               padx=10, pady=8)
        f_cand.pack(fill="x", padx=10, pady=4)
        check_vars, _ = self._build_candidate_checkboxes(f_cand, candidates)

        # Custom entry
        f_cust = tk.LabelFrame(dialog, text="Or type custom (comma-separated)", padx=10, pady=6)
        f_cust.pack(fill="x", padx=10, pady=4)
        custom_var = tk.StringVar()
        tk.Entry(f_cust, textvariable=custom_var).pack(fill="x")

        # Buttons
        btns = tk.Frame(dialog)
        btns.pack(side="bottom", fill="x", padx=10, pady=10)
        self._build_dialog_buttons(btns, result, cancel_all, check_vars, custom_var, dialog)

        # Prompt preview
        f_pp = tk.LabelFrame(dialog, text="Positive prompt (excerpt)", padx=8, pady=6)
        f_pp.pack(fill="both", expand=True, padx=10, pady=4)
        txt = tk.Text(f_pp, height=6, wrap="word")
        txt.insert("1.0", (prompt_text or "")[:PROMPT_EXCERPT_LEN])
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True)

        dialog.wait_window()
        return None if cancel_all["value"] else result["value"]

    _VIDEO_EXTS = {'.mp4', '.avi', '.mov', '.mkv'}

    def _collect_autoname_choices(self):
        """Show dialog for every image (not video); populate self.autoname_map.
        Returns False if user cancelled the batch.
        
        Shows dialog for ALL image files (not just PNGs with ComfyUI metadata)
        so the user can always type custom character names manually.
        Videos are skipped — auto-naming applies to images only.
        """
        self.autoname_map = {}
        self.autoname_counters = defaultdict(int)
        if not self.autoname_enabled.get():
            return True

        library_changed = False
        for path in list(self.files_to_process):
            if os.path.splitext(path)[1].lower() in self._VIDEO_EXTS:
                continue
            prompt, candidates = get_candidates_for_image(path)
            choice = self._prompt_character_for_image(path, candidates, prompt)
            if choice is None:
                return False  # user cancelled batch
            if choice:  # non-empty list
                self.autoname_map[path] = choice
                # ── Learn: add selected tags to character library ──
                for tag in choice:
                    key = tag.lower()
                    entry = self.character_library.get(key, {"tag": tag, "count": 0})
                    entry["count"] = entry.get("count", 0) + 1
                    if entry["tag"] != tag and len(tag) > len(entry["tag"]):
                        # Prefer longer/original casing
                        entry["tag"] = tag
                    self.character_library[key] = entry
                    library_changed = True

        if library_changed:
            self._save_character_library()
        return True

    # ------------------------------------------------------------------
    def start_processing_thread(self):
        if not self.files_to_process:
            messagebox.showerror("Error", "Please drag and drop images to process.")
            return
        if not self.watermark_path.get():
            messagebox.showerror("Error", "Please select a watermark image.")
            return

        # Run auto-name dialogs synchronously in the main thread first.
        if not self._collect_autoname_choices():
            self.status_var.set("Cancelled by user.")
            return

        threading.Thread(target=self.process_images, daemon=True).start()

    def _compute_max_workers(self, files: list[str], pp_enabled: bool) -> int:
        """Probe file dimensions and return appropriate worker count."""
        max_dim = 0
        for fp in files:
            ext = os.path.splitext(fp)[1].lower()
            if ext in VIDEO_EXTS:
                try:
                    vcap = cv2.VideoCapture(fp)
                    vw = int(vcap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    vh = int(vcap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    vcap.release()
                    max_dim = max(max_dim, vw, vh)
                except Exception:
                    pass
            else:
                try:
                    with Image.open(fp) as probe:
                        max_dim = max(max_dim, *probe.size)
                except Image.DecompressionBombError:
                    logging.warning("DecompressionBombError probing %s — skipping", fp)
                except Exception:
                    pass

        cpu_count = os.cpu_count() or 4
        if pp_enabled and max_dim > 3000:
            return 1
        if pp_enabled and max_dim > 2000:
            return min(cpu_count, 2)
        if max_dim > 3000:
            return min(cpu_count, 3)
        return min(cpu_count, 8)

    def _run_batch(self, files: list[str], watermark: Image.Image, cfg: dict,
                   total_files: int) -> tuple[int, list[str]]:
        """Submit all files to the thread pool; return (ok_count, error_basenames)."""
        counters_lock = threading.Lock()
        max_workers = self._compute_max_workers(files, cfg["pp_cfg"]["enabled"])

        custom_out = cfg["custom_out"]
        for file_path in files:
            out_dir = custom_out or os.path.join(os.path.dirname(file_path), "watermarked_clean")
            os.makedirs(out_dir, exist_ok=True)

        ok = 0
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._process_single_image, fp, watermark, cfg, counters_lock): fp
                for fp in files
            }
            for fut in as_completed(futures):
                if self.cancel_event.is_set():
                    break
                fp = futures[fut]
                try:
                    fut.result()
                    ok += 1
                except Exception:
                    logging.exception("Error processing %s", fp)
                    errors.append(os.path.basename(fp))
                self.root.after(0, lambda v=ok: self.progress.configure(value=v))
                self.root.after(0, lambda v=ok: self.status_var.set(
                    f"Processing {v}/{total_files}..."))
        return ok, errors

    def process_images(self) -> None:
        wm_path = self.watermark_path.get()
        custom_out = self.output_dir.get().strip()

        with self._reserved_lock:
            self._reserved_paths.clear()
        self.corner_selector.reset()
        self.processing = True
        self.cancel_event.clear()

        try:
            try:
                watermark = Image.open(wm_path).convert("RGBA")
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", f"Failed to load watermark: {e}"))
                return

            cfg = {
                "watermark_size": self.watermark_size.get(),
                "watermark_opacity": self.watermark_opacity.get(),
                "watermark_corner": self.watermark_corner.get(),
                "randomize_corner": self.randomize_corner.get(),
                "pp_cfg": self._current_pp_config(),
                "custom_out": custom_out,
            }

            files = self.files_to_process
            total_files = len(files)
            self.root.after(0, lambda: self.status_var.set(f"Processing 0/{total_files}..."))
            self.root.after(0, lambda: self.progress.configure(maximum=total_files, value=0))

            ok, errors = self._run_batch(files, watermark, cfg, total_files)

            if self.cancel_event.is_set():
                self.root.after(0, lambda: self.status_var.set("Cancelled."))
                return

            if errors:
                msg = (f"Procesados {ok} de {total_files}. "
                       f"Fallaron {len(errors)}:\n" + "\n".join(errors[:5]))
                self.root.after(0, lambda m=msg: messagebox.showwarning("Advertencia", m))
            else:
                self.root.after(0, lambda: messagebox.showinfo(
                    "Success", f"Processed {ok}/{total_files} files."))
            self.root.after(0, lambda: self.status_var.set(f"Completed {ok}/{total_files}."))

        finally:
            self.processing = False
            self.cancel_event.clear()

    def _unique_save_path(self, save_path: str) -> str:
        """Return a conflict-free path (thread-safe). Registers the chosen path."""
        return _core_unique_save_path(save_path, self._reserved_paths, self._reserved_lock)

    def _process_single_image(self, file_path: str, watermark: Image.Image, cfg: dict, counters_lock: threading.Lock) -> None:
        """Process one image. Called from worker threads — no GUI access."""
        out_dir = cfg["custom_out"] or os.path.join(os.path.dirname(file_path),
                                                     "watermarked_clean")

        if os.path.splitext(file_path)[1].lower() in VIDEO_EXTS:
            self._overlay_watermark_video_worker(file_path, watermark, out_dir, cfg)
        else:
            self._overlay_watermark_worker(file_path, watermark, out_dir, cfg, counters_lock)

    # ------------------------------------------------------------------
    # Worker methods (called from thread pool — no tkinter variable access)
    # ------------------------------------------------------------------
    @staticmethod
    def _ffmpeg_path() -> 'str | None':
        """Find ffmpeg: check next to exe/script first, then PATH."""
        import shutil
        local = os.path.join(FastWatermarkApp._app_dir(), 'ffmpeg.exe')
        if os.path.isfile(local):
            return local
        return shutil.which('ffmpeg')

    def _overlay_watermark_video_worker(self, video_path: str, watermark: Image.Image, save_folder: str, cfg: dict) -> None:
        """Thread-safe video watermarking. Reads all params from cfg dict."""
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError(f"Failed to open video: {video_path}")

        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        filename = os.path.basename(video_path)
        save_path = os.path.join(save_folder, filename)
        # FIX-05: never overwrite source video
        if os.path.normpath(save_path) == os.path.normpath(video_path):
            stem = os.path.splitext(filename)[0]
            save_path = os.path.join(save_folder, f"{stem}_wm.mp4")
        # FIX-01: unique output path
        save_path = self._unique_save_path(save_path)

        # FIX-07: write frames to temp file, then remux with audio via ffmpeg
        tmp_path = os.path.join(save_folder, '_wmtmp_' + os.path.basename(save_path))
        out = cv2.VideoWriter(tmp_path, fourcc, fps, (width, height))

        try:
            wm_width, wm_height = watermark.size
            ref_dim = min(width, height)
            target_w = compute_watermark_width(ref_dim, cfg["watermark_size"])
            scale = target_w / wm_width

            wm_resized = watermark.resize(
                (int(wm_width * scale), int(wm_height * scale)), resample=Image.LANCZOS)

            wm_np = np.array(wm_resized)
            wm_bgr = wm_np[:, :, :3][:, :, ::-1]
            wm_mask = wm_np[:, :, 3]

            wm_h, wm_w = wm_bgr.shape[:2]

            corner = cfg["watermark_corner"]
            if cfg["randomize_corner"]:
                corner = self.corner_selector.choose()

            x, y = position_for_corner(width, height, wm_w, wm_h, corner, MARGIN)

            wm_bgr_f = wm_bgr.astype(float)
            wm_mask_f = (wm_mask.astype(float) / 255.0) * (cfg["watermark_opacity"] / 100)

            frame_count = 0
            while True:
                # FIX-03: cancel check inside frame loop
                if self.cancel_event.is_set():
                    break

                ret, frame = cap.read()
                if not ret:
                    break

                frame_count += 1
                if frame_count % 30 == 0 and total_frames > 0:
                    pct = int(frame_count * 100 / total_frames)
                    self.root.after(0, lambda p=pct: self.status_var.set(f'Video: {p}%...'))

                if frame.shape[0] < y + wm_h or frame.shape[1] < x + wm_w:
                    out.write(frame)
                    continue

                roi = frame[y:y+wm_h, x:x+wm_w].astype(float)
                for c in range(3):
                    roi[:, :, c] = wm_bgr_f[:, :, c] * wm_mask_f + roi[:, :, c] * (1 - wm_mask_f)
                frame[y:y+wm_h, x:x+wm_w] = roi.astype(np.uint8)
                out.write(frame)
        finally:
            cap.release()
            out.release()
            # FIX-07: remux with audio
            import subprocess
            ffmpeg = self._ffmpeg_path()
            remuxed = False
            if ffmpeg and os.path.exists(tmp_path):
                try:
                    subprocess.run(
                        [ffmpeg, '-y', '-i', tmp_path, '-i', video_path,
                         '-map', '0:v:0', '-map', '1:a:0?',
                         '-c:v', 'copy', '-c:a', 'copy', '-shortest', save_path],
                        check=True, capture_output=True)
                    remuxed = True
                except Exception as e:
                    logging.warning("ffmpeg remux failed (%s), saving without audio.", e)
            if not remuxed and os.path.exists(tmp_path):
                os.replace(tmp_path, save_path)
                logging.warning("ffmpeg not found or failed — saved without audio: %s",
                                os.path.basename(save_path))
            # Clean up temp file
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def get_watermark_position_video(self, video_w: int, video_h: int, wm_w: int, wm_h: int, corner: str) -> tuple[int, int]:
        """Calculate video watermark position based on selected corner."""
        return position_for_corner(video_w, video_h, wm_w, wm_h, corner, MARGIN)

    def _current_pp_config(self):
        return {
            "enabled": self.pp_enabled.get(),
            "upscale": self.pp_upscale.get(),
            "upscale_method": "lanczos",
            "kuwahara_radius": self.pp_kuwahara_radius.get(),
            "kuwahara_method": "mean",
            "median_size": self.pp_median_size.get(),
            "downscale": self.pp_downscale.get(),
            "downscale_method": "lanczos",
            "noise_strength": self.pp_noise_strength.get(),
            "noise_monochromatic": self.pp_noise_mono.get(),
            "noise_invert": self.pp_noise_invert.get(),
            "noise_channels": self.pp_noise_channels.get(),
        }

    def _build_output_filename(self, image_path: str, counters_lock: threading.Lock | None = None) -> str:
        """Auto-naming: thread-safe output filename builder."""
        if counters_lock is None:
            counters_lock = threading.Lock()
        return _core_build_output_filename(
            image_path, self.autoname_map, self.autoname_counters, counters_lock)

    def _overlay_watermark_worker(self, image_path: str, watermark: Image.Image, save_folder: str, cfg: dict, counters_lock: threading.Lock) -> None:
        """Thread-safe image watermarking. Reads all params from cfg dict."""
        try:
            im_open = Image.open(image_path)
        except Image.DecompressionBombError:
            logging.warning("DecompressionBombError: file too large, skipping %s", image_path)
            raise
        with im_open as im:
            pp_cfg = cfg["pp_cfg"]

            # ── Get image in workable format (avoid unnecessary copies) ──
            if im.mode not in ("RGB", "RGBA"):
                im_proc = im.convert("RGB")
            else:
                im_proc = im  # no copy — apply_pipeline creates new images

            if pp_cfg["enabled"]:
                im_proc = apply_pipeline(im_proc, pp_cfg)

            im_width, im_height = im_proc.size
            wm_width, wm_height = watermark.size

            ref_dim = min(im_width, im_height)
            target_w = compute_watermark_width(ref_dim, cfg["watermark_size"])
            scale_factor = target_w / wm_width

            wm_resized = watermark.resize(
                (int(wm_width * scale_factor), int(wm_height * scale_factor)),
                resample=Image.LANCZOS)

            if cfg["watermark_opacity"] < 100:
                wm_resized = wm_resized.copy()
                alpha = wm_resized.split()[3].point(
                    lambda p: int(p * (cfg["watermark_opacity"] / 100)))
                wm_resized.putalpha(alpha)

            if im_proc.mode != 'RGBA':
                base = im_proc.convert("RGBA")
            else:
                base = im_proc

            layer = Image.new("RGBA", base.size, (0, 0, 0, 0))

            corner = cfg["watermark_corner"]
            if cfg["randomize_corner"]:
                corner = self.corner_selector.choose()

            position = self.get_watermark_position(base, wm_resized, corner)
            layer.paste(wm_resized, position, wm_resized)
            result = Image.alpha_composite(base, layer)

            # ── Release intermediates immediately ──
            _needs_gc = pp_cfg["enabled"] or max(im_proc.size) > 2000
            del im_proc, base, layer, wm_resized
            if _needs_gc:
                gc.collect()  # force cleanup of large numpy arrays from PP pipeline / large images

            filename = self._build_output_filename(image_path, counters_lock)
            ext = os.path.splitext(filename)[1].lower()
            save_path = os.path.join(save_folder, filename)

            # Safety: never overwrite the original input file.
            if os.path.normpath(save_path) == os.path.normpath(image_path):
                base_name = os.path.splitext(filename)[0]
                save_path = os.path.join(save_folder, f"{base_name}_wm{ext}")
                logging.warning("Output would overwrite input. Saved as: %s",
                                os.path.basename(save_path))

            # FIX-01: guarantee no two outputs share a path
            save_path = self._unique_save_path(save_path)

            # Save WITHOUT metadata — no pnginfo/exif/icc_profile passed.
            fmt = ext.lstrip(".")
            save_without_metadata(result, save_path, fmt)

            del result

if __name__ == "__main__":
    root = TkinterDnD.Tk()
    app = FastWatermarkApp(root)
    root.mainloop()
