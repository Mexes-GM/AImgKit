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
            'AImgKit.App'
        )
    except Exception:
        pass

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

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

__version__ = "2.0.1"

# FIX-09: allow up to 300 MP; DecompressionBombError raised beyond this
Image.MAX_IMAGE_PIXELS = 300_000_000


def _config_dir():
    """Return (and create) per-user config/log directory."""
    d = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'AImgKit')
    os.makedirs(d, exist_ok=True)
    return d


# ── Logging setup ──────────────────────────────────────────────────────────
_log_path = os.path.join(_config_dir(), 'aimgkit.log')
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

# ── Modern theme (CustomTkinter) ────────────────────────────────────────────
ACCENT = "#4CAF50"          # primary action green — reserved for primary action only
ACCENT_HOVER = "#43A047"
DANGER = "#c0392b"
MUTED = "gray"
VALUE_FG = "#dce4ee"        # neutral light color for numeric readouts (not the accent)
TAGLINE_FG = "#8a8f98"      # muted brand tagline
PREVIEW_BG = "#2b2b2b"      # neutral canvas backdrop that works in dark/light
DROPZONE_IDLE = "#3a3a3a"   # listbox/dropzone border at rest
DROPZONE_HOVER = ACCENT     # dropzone border while a drag hovers over it


class CTkDnD(ctk.CTk, TkinterDnD.DnDWrapper):
    """CustomTkinter root window with tkinterdnd2 drag & drop enabled.

    Combines CTk's themed window with TkinterDnD's drop-target machinery so
    that CTkEntry / tk.Listbox widgets keep supporting drop_target_register().
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.TkdndVersion = TkinterDnD._require(self)


class AImgKitApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AImgKit")
        self.root.geometry("1260x820")
        self.root.minsize(980, 620)
        
        # Window icon (title bar + taskbar, all resolutions)
        self._set_window_icon()
        
        # Config file path — in per-user %LOCALAPPDATA%/AImgKit (FIX-10)
        cfg_dir = self._config_dir()
        self.config_file = os.path.join(cfg_dir, 'watermark_config.json')
        self.library_file = os.path.join(cfg_dir, 'character_library.json')

        # Migration: bring over config/library from previous locations if the
        # new per-user dir is still empty (rebrand FastWatermark -> AImgKit).
        import shutil
        _legacy_localappdata = os.path.join(
            os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'FastWatermark')
        _migrations = [
            # (source, destination)
            (os.path.join(_legacy_localappdata, 'watermark_config.json'), self.config_file),
            (os.path.join(_legacy_localappdata, 'character_library.json'), self.library_file),
            # FIX-10: even older config saved next to the script/exe
            (os.path.join(self._app_dir(), 'watermark_config.json'), self.config_file),
        ]
        for _src, _dst in _migrations:
            if os.path.exists(_src) and not os.path.exists(_dst):
                try:
                    shutil.copy2(_src, _dst)
                    logging.info("Migrated %s to %s", _src, _dst)
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

        # ===== Watermark 2 =====
        self.watermark2_path = tk.StringVar()
        self.watermark2_size = tk.DoubleVar(value=WATERMARK_RELATIVE_WIDTH)
        self.watermark2_opacity = tk.DoubleVar(value=WATERMARK_OPACITY)
        self.watermark2_corner = tk.StringVar(value="bottom-right")
        self.watermark2_randomize = tk.BooleanVar(value=False)
        # Proxy vars bound to the sliders — always reflect the active slot
        self._wm_size_proxy = tk.DoubleVar(value=WATERMARK_RELATIVE_WIDTH)
        self._wm_opacity_proxy = tk.DoubleVar(value=WATERMARK_OPACITY)

        # ===== Post-processing filter variables =====
        self.pp_enabled = tk.BooleanVar(value=DEFAULT_PIPELINE["enabled"])
        self.pp_jpeg_removal_strength = tk.IntVar(value=DEFAULT_PIPELINE["jpeg_removal_strength"])
        self.pp_upscale = tk.DoubleVar(value=DEFAULT_PIPELINE["upscale"])
        self.pp_upscale_method = tk.StringVar(value=DEFAULT_PIPELINE["upscale_method"])
        self.pp_kuwahara_radius = tk.IntVar(value=DEFAULT_PIPELINE["kuwahara_radius"])
        self.pp_kuwahara_method = tk.StringVar(value=DEFAULT_PIPELINE["kuwahara_method"])
        self.pp_median_size = tk.IntVar(value=DEFAULT_PIPELINE["median_size"])
        self.pp_downscale = tk.DoubleVar(value=DEFAULT_PIPELINE["downscale"])
        self.pp_downscale_method = tk.StringVar(value=DEFAULT_PIPELINE["downscale_method"])
        self.pp_noise_strength = tk.DoubleVar(value=DEFAULT_PIPELINE["noise_strength"])
        self.pp_noise_mono = tk.BooleanVar(value=DEFAULT_PIPELINE["noise_monochromatic"])
        self.pp_noise_invert = tk.BooleanVar(value=DEFAULT_PIPELINE["noise_invert"])
        self.pp_noise_channels = tk.StringVar(value=DEFAULT_PIPELINE["noise_channels"])
        # new effects
        self.pp_sharpen_amount = tk.DoubleVar(value=DEFAULT_PIPELINE["sharpen_amount"])
        self.pp_sharpen_radius = tk.StringVar(value=str(DEFAULT_PIPELINE["sharpen_radius"]))
        self.pp_sharpen_threshold = tk.IntVar(value=DEFAULT_PIPELINE["sharpen_threshold"])
        self.pp_hsb_hue = tk.DoubleVar(value=DEFAULT_PIPELINE["hsb_hue"])
        self.pp_hsb_sat = tk.DoubleVar(value=DEFAULT_PIPELINE["hsb_sat"])
        self.pp_hsb_val = tk.DoubleVar(value=DEFAULT_PIPELINE["hsb_val"])
        self.pp_chroma_shift = tk.IntVar(value=DEFAULT_PIPELINE["chroma_shift"])
        self.pp_vignette_strength = tk.DoubleVar(value=DEFAULT_PIPELINE["vignette_strength"])
        self.pp_vignette_feather = tk.DoubleVar(value=DEFAULT_PIPELINE["vignette_feather"])
        self.pp_jpeg_quality = tk.IntVar(value=DEFAULT_PIPELINE["jpeg_quality"])
        self.pp_jpeg_subsampling = tk.StringVar(value=str(DEFAULT_PIPELINE["jpeg_subsampling"]))
        self.pp_grain_strength = tk.DoubleVar(value=DEFAULT_PIPELINE["grain_strength"])
        self.pp_grain_size = tk.IntVar(value=DEFAULT_PIPELINE["grain_size"])
        self.pp_grain_mono = tk.BooleanVar(value=DEFAULT_PIPELINE["grain_mono"])

        # ===== Auto-naming variables =====
        self.autoname_enabled = tk.BooleanVar(value=False)
        # Map file_path -> list of chosen characters (up to 2, filled by dialog before worker)
        self.autoname_map = {}
        
        # Cache for preview image
        self.preview_photo = None
        self._wm_cache: tuple = ()
        self._save_after_id = None
        self._preview_resize_id = None

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

    # ------------------------------------------------------------------
    # UI helpers (CustomTkinter)
    # ------------------------------------------------------------------
    def _section(self, parent, title, fill="x", expand=False):
        """Create a titled card (replaces tk.LabelFrame) and return it."""
        frame = ctk.CTkFrame(parent, border_width=1, border_color="gray25")
        frame.pack(fill=fill, expand=expand, padx=10, pady=6)
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(size=14, weight="bold"),
                     anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        return frame

    def _apply_listbox_theme(self):
        """Color the tk.Listbox to match the fixed dark appearance."""
        if not hasattr(self, "file_listbox"):
            return
        self.file_listbox.configure(
            bg="#2b2b2b", fg="#dce4ee",
            selectbackground=ACCENT, selectforeground="white")

    def _set_progress(self, value, total):
        """CTkProgressBar uses a 0.0-1.0 fraction (unlike ttk's maximum/value)."""
        frac = 0.0 if total <= 0 else max(0.0, min(1.0, value / total))
        self.progress.set(frac)

    def create_widgets(self):
        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill="both", expand=True)

        # Create main frames
        left_frame = ctk.CTkFrame(body)
        left_frame.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        right_frame = ctk.CTkFrame(body, width=640)
        right_frame.pack(side="right", fill="both", expand=False, padx=10, pady=10)
        right_frame.pack_propagate(False)

        # ===== LEFT SIDE =====

        # Output directory
        frame_out = self._section(
            left_frame, "1 · Output folder (optional)")
        row_out = ctk.CTkFrame(frame_out, fg_color="transparent")
        row_out.pack(fill="x", padx=10, pady=(0, 10))
        entry_out = ctk.CTkEntry(row_out, textvariable=self.output_dir,
                                 placeholder_text="Empty \u2192 saves to <source>/watermarked_clean")
        entry_out.pack(side="left", fill="x", expand=True)
        entry_out.drop_target_register(DND_FILES)
        entry_out.dnd_bind('<<Drop>>', self.drop_output_dir)
        ctk.CTkButton(row_out, text="Clear", width=64,
                      fg_color="gray30", hover_color="gray25",
                      command=lambda: self.output_dir.set("")).pack(side="right", padx=(8, 0))
        ctk.CTkButton(row_out, text="Browse...", width=90,
                      command=self.browse_output_dir).pack(side="right", padx=(8, 0))

        # Drop Zone (Listbox)
        frame_drop = self._section(left_frame, "2 · Add files",
                                   fill="both", expand=True)
        # Bordered holder so we can recolor the border while a drag hovers.
        list_holder = ctk.CTkFrame(frame_drop, fg_color="#2b2b2b",
                                   border_width=2, border_color=DROPZONE_IDLE,
                                   corner_radius=8)
        list_holder.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._dropzone_holder = list_holder

        # tk.Listbox kept: CustomTkinter has no native multi-select Listbox,
        # and we rely on EXTENDED selection + index ops + DnD here.
        self.file_listbox = tk.Listbox(list_holder, selectmode=tk.EXTENDED,
                                       borderwidth=0, highlightthickness=0,
                                       activestyle="none", font=("Segoe UI", 10))
        self.file_listbox.pack(fill="both", expand=True, side="left",
                               padx=2, pady=2)

        scrollbar = ctk.CTkScrollbar(list_holder, command=self.file_listbox.yview)
        scrollbar.pack(side="right", fill="y", pady=2)
        self.file_listbox.config(yscrollcommand=scrollbar.set)
        self._apply_listbox_theme()

        # Empty-state placeholder, centered over the listbox.
        self.drop_placeholder = tk.Label(
            list_holder, bg="#2b2b2b", fg="#a7afbd", justify="center",
            text="\u2913\n\nDrag & drop images or videos here\nor a whole folder",
            font=("Segoe UI", 13))
        self._update_dropzone_placeholder()

        # Enable Drag & Drop for Listbox (+ hover feedback)
        self.file_listbox.drop_target_register(DND_FILES)
        self.file_listbox.dnd_bind('<<Drop>>', self._on_drop_files)
        self.file_listbox.dnd_bind('<<DropEnter>>', self._on_drag_enter)
        self.file_listbox.dnd_bind('<<DropLeave>>', self._on_drag_leave)

        # The placeholder overlays the listbox, so it must accept drops too —
        # otherwise dropping onto the empty state (the common case) would miss.
        self.drop_placeholder.drop_target_register(DND_FILES)
        self.drop_placeholder.dnd_bind('<<Drop>>', self._on_drop_files)
        self.drop_placeholder.dnd_bind('<<DropEnter>>', self._on_drag_enter)
        self.drop_placeholder.dnd_bind('<<DropLeave>>', self._on_drag_leave)

        # Clear List Button
        ctk.CTkButton(left_frame, text="Clear List",
                      fg_color="gray30", hover_color="gray25",
                      command=self.clear_list).pack(pady=6)

        # Progress + status grouped in one coherent card
        status_card = ctk.CTkFrame(left_frame)
        status_card.pack(fill="x", padx=10, pady=(6, 4))
        ctk.CTkLabel(status_card, textvariable=self.status_var,
                     anchor="w", text_color=VALUE_FG).pack(
            fill="x", padx=12, pady=(10, 4))
        self.progress = ctk.CTkProgressBar(status_card, height=14)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=12, pady=(0, 12))

        # Process Button — disabled until a watermark + at least one file exist,
        # so it never offers a false affordance.
        self.run_button = ctk.CTkButton(
            left_frame, text="Start Batch Processing",
            command=self.start_processing_thread,
            height=44, font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER)
        self.run_button.pack(pady=12, padx=10, fill="x")
        self._update_run_button_state()

        # ===== RIGHT SIDE (tabbed) =====
        ctk.CTkLabel(right_frame, text="Settings",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     anchor="w", text_color=TAGLINE_FG).pack(
            anchor="w", padx=12, pady=(10, 0))
        notebook = ctk.CTkTabview(right_frame)
        # Navigation, not action: the selected tab uses a neutral elevation,
        # not the primary action green (which is reserved for the CTA).
        notebook.configure(segmented_button_selected_color="gray34",
                           segmented_button_selected_hover_color="gray30",
                           segmented_button_unselected_color="gray20",
                           segmented_button_unselected_hover_color="gray25")
        notebook.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        wm_tab = notebook.add("Watermark")
        pp_tab = notebook.add("Post-Processing")
        an_tab = notebook.add("Auto-Name")

        # ---- Watermark tab ----
        # Track which watermark slot is active (1 or 2)
        self._active_wm_slot = 1

        # Per-slot data: (path_var, size_var, opacity_var, corner_var, randomize_var,
        #                  browse_cmd, clear_cmd, select_corner_cmd, refresh_corner_cmd)
        # Built after widgets exist — populated in _build_wm_slot_controls below.

        wm_scroll = ctk.CTkScrollableFrame(wm_tab, fg_color="transparent", height=340)
        wm_scroll.pack(fill="x")

        # ── Slot toggle ──────────────────────────────────────────────────────
        toggle_row = ctk.CTkFrame(wm_scroll, fg_color="transparent")
        toggle_row.pack(fill="x", padx=10, pady=(6, 2))
        self._wm_slot_btn1 = ctk.CTkButton(toggle_row, text="Watermark 1", width=140,
                                            command=lambda: self._switch_wm_slot(1))
        self._wm_slot_btn2 = ctk.CTkButton(toggle_row, text="Watermark 2 (optional)", width=180,
                                            command=lambda: self._switch_wm_slot(2))
        self._wm_slot_btn1.pack(side="left", expand=True)
        self._wm_slot_btn2.pack(side="left", expand=True)

        # ── Path row ─────────────────────────────────────────────────────────
        frame_path = self._section(wm_scroll, "Watermark image")
        row_path = ctk.CTkFrame(frame_path, fg_color="transparent")
        row_path.pack(fill="x", padx=10, pady=(0, 10))
        self._wm_path_entry = ctk.CTkEntry(row_path, textvariable=self.watermark_path,
                                            placeholder_text="Drop a watermark or click Browse...")
        self._wm_path_entry.pack(side="left", fill="x", expand=True)
        self._wm_path_entry.drop_target_register(DND_FILES)
        self._wm_path_entry.dnd_bind('<<Drop>>', self.drop_watermark)
        self._wm_browse_btn = ctk.CTkButton(row_path, text="Browse...", width=90,
                                             command=self._browse_active_wm)
        self._wm_browse_btn.pack(side="right", padx=(8, 0))
        self._wm_clear_btn = ctk.CTkButton(row_path, text="✕", width=32,
                                            fg_color="gray30", hover_color="gray25",
                                            command=lambda: self._wm_active_path().set(""))
        self._wm_clear_btn.pack(side="right", padx=(4, 0))

        # ── Size ─────────────────────────────────────────────────────────────
        frame_size = self._section(wm_scroll, "Size")
        head_size = ctk.CTkFrame(frame_size, fg_color="transparent")
        head_size.pack(fill="x", padx=10)
        ctk.CTkLabel(head_size, text="Size (%):", anchor="w").pack(side="left")
        self.size_label = ctk.CTkLabel(head_size, text="28%", text_color=VALUE_FG,
                                       font=ctk.CTkFont(weight="bold"))
        self.size_label.pack(side="right")
        self.size_scale = ctk.CTkSlider(frame_size, from_=6, to=50, number_of_steps=44,
                                        variable=self._wm_size_proxy,
                                        command=self._on_size_slide)
        self.size_scale.pack(fill="x", padx=10, pady=(4, 10))

        # ── Opacity ───────────────────────────────────────────────────────────
        frame_opacity = self._section(wm_scroll, "Transparency")
        head_op = ctk.CTkFrame(frame_opacity, fg_color="transparent")
        head_op.pack(fill="x", padx=10)
        ctk.CTkLabel(head_op, text="Opacity:", anchor="w").pack(side="left")
        self.opacity_label = ctk.CTkLabel(head_op, text="80%", text_color=VALUE_FG,
                                          font=ctk.CTkFont(weight="bold"))
        self.opacity_label.pack(side="right")
        self.opacity_scale = ctk.CTkSlider(frame_opacity, from_=0, to=100,
                                           number_of_steps=100,
                                           variable=self._wm_opacity_proxy,
                                           command=self._on_opacity_slide)
        self.opacity_scale.pack(fill="x", padx=10, pady=(4, 10))

        # ── Position ──────────────────────────────────────────────────────────
        frame_position = self._section(wm_scroll, "Position")
        ctk.CTkLabel(frame_position, text="Corner:", anchor="w").pack(anchor="w", padx=10)
        grid = ctk.CTkFrame(frame_position, fg_color="transparent")
        grid.pack(padx=10, pady=(4, 6))
        grid.grid_columnconfigure((0, 1), weight=1)
        self._corner_buttons = {}
        corner_layout = [
            ("Top Left", "top-left", 0, 0), ("Top Right", "top-right", 0, 1),
            ("Bottom Left", "bottom-left", 1, 0), ("Bottom Right", "bottom-right", 1, 1),
        ]
        for label, value, r, c in corner_layout:
            b = ctk.CTkButton(grid, text=label, width=140, height=40,
                              command=lambda v=value: self._select_corner(v))
            b.grid(row=r, column=c, padx=4, pady=4)
            self._corner_buttons[value] = b
        self._refresh_corner_buttons()
        self._wm_randomize_switch = ctk.CTkSwitch(frame_position, text="Randomize corner",
                                                   variable=self.randomize_corner,
                                                   command=self._on_randomize_toggle)
        self._wm_randomize_switch.pack(anchor="w", padx=14, pady=(8, 10))

        # Init slot toggle appearance
        self._switch_wm_slot(1)

        # Preview
        frame_preview = self._section(wm_tab, "Preview", fill="both", expand=True)
        self.preview_canvas = tk.Canvas(frame_preview, bg=PREVIEW_BG,
                                        highlightthickness=1, highlightbackground="#555555")
        self.preview_canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.preview_canvas.bind("<Configure>", self.on_preview_resize)

        # ---- Post-Processing tab ----
        self._build_postprocessing_tab(pp_tab)
        # ---- Auto-Naming tab ----
        self._build_autoname_tab(an_tab)
        
        # Add traces to save options when modified
        for v in (self.watermark_path, self.output_dir,
                  self.watermark_size, self.watermark_opacity,
                  self.watermark_corner, self.randomize_corner,
                  self.watermark2_path, self.watermark2_size, self.watermark2_opacity,
                  self.watermark2_corner, self.watermark2_randomize,
                  self.pp_enabled, self.pp_jpeg_removal_strength,
                  self.pp_upscale, self.pp_upscale_method,
                  self.pp_kuwahara_radius, self.pp_kuwahara_method,
                  self.pp_median_size, self.pp_downscale, self.pp_downscale_method,
                  self.pp_noise_strength,
                  self.pp_noise_mono, self.pp_noise_invert, self.pp_noise_channels,
                  self.pp_sharpen_amount, self.pp_sharpen_radius, self.pp_sharpen_threshold,
                  self.pp_hsb_hue, self.pp_hsb_sat, self.pp_hsb_val,
                  self.pp_chroma_shift,
                  self.pp_vignette_strength, self.pp_vignette_feather,
                  self.pp_jpeg_quality, self.pp_jpeg_subsampling,
                  self.pp_grain_strength, self.pp_grain_size, self.pp_grain_mono,
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
                if "watermark2_path" in config:
                    self.watermark2_path.set(config["watermark2_path"])
                if "watermark2_size" in config:
                    self.watermark2_size.set(config["watermark2_size"])
                if "watermark2_opacity" in config:
                    self.watermark2_opacity.set(config["watermark2_opacity"])
                if "watermark2_corner" in config:
                    self.watermark2_corner.set(config["watermark2_corner"])
                if "watermark2_randomize" in config:
                    self.watermark2_randomize.set(config["watermark2_randomize"])
                if "output_dir" in config:
                    self.output_dir.set(config["output_dir"])

                # Post-processing
                pp = config.get("post_processing", {}) or {}
                if "enabled" in pp: self.pp_enabled.set(pp["enabled"])
                if "jpeg_removal_strength" in pp: self.pp_jpeg_removal_strength.set(pp["jpeg_removal_strength"])
                if "upscale" in pp: self.pp_upscale.set(pp["upscale"])
                if "upscale_method" in pp: self.pp_upscale_method.set(pp["upscale_method"])
                if "kuwahara_radius" in pp: self.pp_kuwahara_radius.set(pp["kuwahara_radius"])
                if "kuwahara_method" in pp: self.pp_kuwahara_method.set(pp["kuwahara_method"])
                if "median_size" in pp: self.pp_median_size.set(pp["median_size"])
                if "downscale" in pp: self.pp_downscale.set(pp["downscale"])
                if "downscale_method" in pp: self.pp_downscale_method.set(pp["downscale_method"])
                if "noise_strength" in pp: self.pp_noise_strength.set(pp["noise_strength"])
                if "noise_monochromatic" in pp: self.pp_noise_mono.set(pp["noise_monochromatic"])
                if "noise_invert" in pp: self.pp_noise_invert.set(pp["noise_invert"])
                if "noise_channels" in pp: self.pp_noise_channels.set(pp["noise_channels"])
                if "sharpen_amount" in pp: self.pp_sharpen_amount.set(pp["sharpen_amount"])
                if "sharpen_radius" in pp: self.pp_sharpen_radius.set(str(pp["sharpen_radius"]))
                if "sharpen_threshold" in pp: self.pp_sharpen_threshold.set(pp["sharpen_threshold"])
                if "hsb_hue" in pp: self.pp_hsb_hue.set(pp["hsb_hue"])
                if "hsb_sat" in pp: self.pp_hsb_sat.set(pp["hsb_sat"])
                if "hsb_val" in pp: self.pp_hsb_val.set(pp["hsb_val"])
                if "chroma_shift" in pp: self.pp_chroma_shift.set(pp["chroma_shift"])
                if "vignette_strength" in pp: self.pp_vignette_strength.set(pp["vignette_strength"])
                if "vignette_feather" in pp: self.pp_vignette_feather.set(pp["vignette_feather"])
                if "jpeg_quality" in pp: self.pp_jpeg_quality.set(pp["jpeg_quality"])
                if "jpeg_subsampling" in pp: self.pp_jpeg_subsampling.set(str(pp["jpeg_subsampling"]))
                if "grain_strength" in pp: self.pp_grain_strength.set(pp["grain_strength"])
                if "grain_size" in pp: self.pp_grain_size.set(pp["grain_size"])
                if "grain_mono" in pp: self.pp_grain_mono.set(pp["grain_mono"])
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
                "watermark2_path": self.watermark2_path.get(),
                "watermark2_size": self.watermark2_size.get(),
                "watermark2_opacity": self.watermark2_opacity.get(),
                "watermark2_corner": self.watermark2_corner.get(),
                "watermark2_randomize": self.watermark2_randomize.get(),
                "output_dir": self.output_dir.get(),
                "autoname_enabled": self.autoname_enabled.get(),
                "post_processing": {
                    "enabled": self.pp_enabled.get(),
                    "jpeg_removal_strength": self.pp_jpeg_removal_strength.get(),
                    "upscale": self.pp_upscale.get(),
                    "upscale_method": self.pp_upscale_method.get(),
                    "kuwahara_radius": self.pp_kuwahara_radius.get(),
                    "kuwahara_method": self.pp_kuwahara_method.get(),
                    "median_size": self.pp_median_size.get(),
                    "downscale": self.pp_downscale.get(),
                    "downscale_method": self.pp_downscale_method.get(),
                    "noise_strength": self.pp_noise_strength.get(),
                    "noise_monochromatic": self.pp_noise_mono.get(),
                    "noise_invert": self.pp_noise_invert.get(),
                    "noise_channels": self.pp_noise_channels.get(),
                    "sharpen_amount": self.pp_sharpen_amount.get(),
                    "sharpen_radius": int(self.pp_sharpen_radius.get()),
                    "sharpen_threshold": self.pp_sharpen_threshold.get(),
                    "hsb_hue": self.pp_hsb_hue.get(),
                    "hsb_sat": self.pp_hsb_sat.get(),
                    "hsb_val": self.pp_hsb_val.get(),
                    "chroma_shift": self.pp_chroma_shift.get(),
                    "vignette_strength": self.pp_vignette_strength.get(),
                    "vignette_feather": self.pp_vignette_feather.get(),
                    "jpeg_quality": self.pp_jpeg_quality.get(),
                    "jpeg_subsampling": int(self.pp_jpeg_subsampling.get()[0]),
                    "grain_strength": self.pp_grain_strength.get(),
                    "grain_size": self.pp_grain_size.get(),
                    "grain_mono": self.pp_grain_mono.get(),
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
        self._update_run_button_state()
        if self._save_after_id:
            self.root.after_cancel(self._save_after_id)
        self._save_after_id = self.root.after(500, self.save_options)

    def _update_dropzone_placeholder(self):
        """Show the centered empty-state hint only when the list is empty."""
        if not hasattr(self, "drop_placeholder"):
            return
        if self.files_to_process:
            self.drop_placeholder.place_forget()
        else:
            self.drop_placeholder.place(relx=0.5, rely=0.5, anchor="center")

    def _update_run_button_state(self):
        """Enable the CTA only when a watermark and at least one file exist, and not already processing."""
        if not hasattr(self, "run_button"):
            return
        if self.processing:
            self.run_button.configure(
                state="normal", text="Cancel Batch",
                fg_color=DANGER, hover_color="#a93226",
                command=self._cancel_batch)
            return
        self.run_button.configure(
            command=self.start_processing_thread,
            fg_color=ACCENT, hover_color=ACCENT_HOVER)
        ready = bool(self.watermark_path.get().strip()) and bool(self.files_to_process)
        if ready:
            self.run_button.configure(state="normal", text="Start Batch Processing")
        else:
            missing = "watermark" if not self.watermark_path.get().strip() else "files"
            self.run_button.configure(
                state="disabled",
                text=f"Start Batch Processing  (add {missing})")

    def _cancel_batch(self):
        """Request cancellation of the running batch."""
        self.cancel_event.set()
        self.status_var.set("Cancelling...")

    def _on_drag_enter(self, event):
        """Highlight the dropzone border while a drag hovers over it."""
        if hasattr(self, "_dropzone_holder"):
            self._dropzone_holder.configure(border_color=DROPZONE_HOVER)
        return event.action

    def _on_drag_leave(self, event):
        """Restore the dropzone border once the drag leaves."""
        if hasattr(self, "_dropzone_holder"):
            self._dropzone_holder.configure(border_color=DROPZONE_IDLE)
        return event.action

    def _on_drop_files(self, event):
        """Drop handler wrapper: reset border + refresh empty-state."""
        if hasattr(self, "_dropzone_holder"):
            self._dropzone_holder.configure(border_color=DROPZONE_IDLE)
        self.drop_files(event)
        self._update_dropzone_placeholder()
        self._update_run_button_state()

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
        self.update_pp_preview()


    def drop_watermark(self, event):
        path = self.clean_path(event.data)
        if os.path.isfile(path):
            self.watermark_path.set(path)

    @staticmethod
    def _preview_backdrop(w: int, h: int) -> "Image.Image":
        """Checkerboard canvas — the universal 'image goes here' backdrop,
        far more informative than a flat gray rectangle."""
        tile = 16
        ys = (np.arange(h) // tile)[:, None]
        xs = (np.arange(w) // tile)[None, :]
        mask = ((ys + xs) % 2 == 0)
        arr = np.empty((h, w, 3), dtype=np.uint8)
        arr[mask] = (58, 58, 58)
        arr[~mask] = (74, 74, 74)
        return Image.fromarray(arr, "RGB").convert("RGBA")

    def _render_watermark_preview(self, watermark: Image.Image,
                                   canvas_w: int, canvas_h: int) -> None:
        """Compose and draw watermark onto the preview canvas."""
        padding = PREVIEW_PADDING
        pw = canvas_w - padding
        ph = canvas_h - padding
        if pw <= 0 or ph <= 0:
            return

        preview_img = self._preview_backdrop(pw, ph)
        wm_w, wm_h = watermark.size
        size_pct = self._wm_size_proxy.get()
        # Use min(pw, ph) as ref_dim to match the worker's scaling logic
        # (which uses min(im_width, im_height)).  Using only pw overestimates
        # the watermark size for portrait-like canvases.
        ref_dim = min(pw, ph)
        target_w = compute_watermark_width(ref_dim, size_pct)

        if target_w > 0 and wm_w > 0:
            scale = target_w / wm_w
            wm = watermark.resize((int(wm_w * scale), int(wm_h * scale)), resample=Image.LANCZOS)
            opacity = self._wm_opacity_proxy.get()
            if opacity < 100:
                wm = wm.copy()
                wm.putalpha(wm.split()[3].point(lambda p: int(p * opacity / 100)))
            corner = getattr(self, "_active_corner_var", self.watermark_corner).get()
            pos = position_for_corner(pw, ph, wm.width, wm.height, corner)
            preview_img.paste(wm, pos, wm)

        self.preview_photo = ImageTk.PhotoImage(preview_img)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(padding // 2, padding // 2,
                                         image=self.preview_photo, anchor="nw")
        self.size_label.configure(text=f"{size_pct:.0f}%")
        self.opacity_label.configure(text=f"{self._wm_opacity_proxy.get():.0f}%")

        # UX-02: fidelity notes
        note_y = canvas_h - padding // 2
        note_x = padding // 2 + 4
        if self.randomize_corner.get():
            self.preview_canvas.create_text(note_x, note_y,
                text="↺ Random corner per image", fill="#888", anchor="sw")

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
            cw = self.preview_canvas.winfo_width()
            ch = self.preview_canvas.winfo_height()
            if cw <= 1 or ch <= 1:
                # Canvas not laid out yet — retry once its geometry is known.
                self.root.after(50, self.update_preview)
                return

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
        """Fires when preview canvas resizes (debounced to avoid rebuild thrash)."""
        if self._preview_resize_id:
            self.root.after_cancel(self._preview_resize_id)
        self._preview_resize_id = self.root.after(60, self.update_preview)

    def _select_corner(self, value):
        """Set the active corner from the spatial 2x2 picker."""
        corner_var = getattr(self, "_active_corner_var", self.watermark_corner)
        corner_var.set(value)
        self._refresh_corner_buttons()
        self.update_preview()

    def _refresh_corner_buttons(self):
        """Highlight the active corner; dim all when randomization is on."""
        if not hasattr(self, "_corner_buttons"):
            return
        corner_var = getattr(self, "_active_corner_var", self.watermark_corner)
        rand_var   = getattr(self, "_active_rand_var",   self.randomize_corner)
        randomizing = rand_var.get()
        current = corner_var.get()
        for value, btn in self._corner_buttons.items():
            if randomizing:
                btn.configure(fg_color="gray25", hover_color="gray20",
                              text_color="gray45")
            elif value == current:
                btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOVER,
                              text_color="white")
            else:
                btn.configure(fg_color="gray25", hover_color="gray20",
                              text_color=VALUE_FG)

    def _on_randomize_toggle(self):
        """Randomize switch: dim/restore the corner picker + refresh preview."""
        self._refresh_corner_buttons()
        self.update_preview()

    def _wm_active_path(self):
        return self.watermark_path if self._active_wm_slot == 1 else self.watermark2_path

    def _browse_active_wm(self):
        path = filedialog.askopenfilename(filetypes=[("Images", "*.png;*.jpg;*.jpeg")])
        if path:
            self._wm_active_path().set(path)

    def _browse_watermark2(self):
        path = filedialog.askopenfilename(filetypes=[("Images", "*.png;*.jpg;*.jpeg")])
        if path:
            self.watermark2_path.set(path)

    def _switch_wm_slot(self, slot: int):
        """Switch the shared controls to show/edit WM1 or WM2."""
        self._active_wm_slot = slot
        is1 = slot == 1
        path_var  = self.watermark_path    if is1 else self.watermark2_path
        size_var  = self.watermark_size    if is1 else self.watermark2_size
        op_var    = self.watermark_opacity if is1 else self.watermark2_opacity
        corner_var = self.watermark_corner if is1 else self.watermark2_corner
        rand_var  = self.randomize_corner  if is1 else self.watermark2_randomize

        # Rewire path entry
        self._wm_path_entry.configure(
            textvariable=path_var,
            placeholder_text="Drop watermark or click Browse..." if is1
                             else "Drop second watermark or click Browse...")
        # Rewire browse/clear commands — use stable lambdas, no reconfigure needed
        # (reconfiguring command on CTkButton can trigger it in some versions)
        # Rewire DnD on path entry
        self._wm_path_entry.dnd_bind(
            '<<Drop>>', self.drop_watermark if is1 else
            lambda e: self.watermark2_path.set(self.clean_path(e.data))
                if os.path.isfile(self.clean_path(e.data)) else None)

        # Sync proxy vars → sliders follow automatically via variable=
        self._wm_size_proxy.set(size_var.get())
        self.size_label.configure(text=f"{size_var.get():.0f}%")
        self._wm_opacity_proxy.set(op_var.get())
        self.opacity_label.configure(text=f"{op_var.get():.0f}%")

        # Rewire corner buttons
        for value, btn in self._corner_buttons.items():
            btn.configure(command=lambda v=value: self._select_corner(v))
        self.watermark_corner = corner_var if is1 else corner_var  # always same attr for _refresh
        # Temporarily point the attr to the right var so _refresh_corner_buttons works
        self._active_corner_var = corner_var
        self._active_rand_var = rand_var
        self._refresh_corner_buttons()

        # Rewire randomize switch
        self._wm_randomize_switch.configure(variable=rand_var, command=self._on_randomize_toggle)

        # Toggle button styles
        active_color = ACCENT
        inactive_color = "gray25"
        self._wm_slot_btn1.configure(fg_color=active_color if is1 else inactive_color,
                                      hover_color=ACCENT_HOVER if is1 else "gray20")
        self._wm_slot_btn2.configure(fg_color=active_color if not is1 else inactive_color,
                                      hover_color=ACCENT_HOVER if not is1 else "gray20")

    def _on_size_slide(self, v):
        var = self.watermark_size if self._active_wm_slot == 1 else self.watermark2_size
        var.set(v)
        self._wm_size_proxy.set(v)
        self.size_label.configure(text=f"{v:.0f}%")
        self.update_preview()

    def _on_opacity_slide(self, v):
        var = self.watermark_opacity if self._active_wm_slot == 1 else self.watermark2_opacity
        var.set(v)
        self._wm_opacity_proxy.set(v)
        self.opacity_label.configure(text=f"{v:.0f}%")
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
        self._update_dropzone_placeholder()
        self._update_run_button_state()
        self.update_pp_preview()

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
        # Controls in a fixed-height scroll, preview canvas below (expands to fill)
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent", height=310)
        scroll.pack(fill="x")

        ctk.CTkSwitch(scroll, text="Enable Post-Processing",
                      variable=self.pp_enabled,
                      command=self.update_pp_preview).pack(anchor="w", padx=12, pady=8)

        # JPEG Artifact Removal (pre-process)
        f = self._section(scroll, "0) JPEG Artifact Removal (pre-process)")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Strength (0 = disabled):", anchor="w").pack(side="left")
        lbl_jar = ctk.CTkLabel(h, text=str(self.pp_jpeg_removal_strength.get()), text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_jar.pack(side="right")
        ctk.CTkSlider(f, from_=0, to=10, number_of_steps=10, variable=self.pp_jpeg_removal_strength,
                      command=lambda v: (lbl_jar.configure(text=str(int(v))), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 4))
        ctk.CTkLabel(f, text="3=light · 5=medium · 8=strong · 10=aggressive\nHigher values remove more artifacts but soften fine detail.",
                     text_color=MUTED, font=ctk.CTkFont(size=11), justify="left").pack(anchor="w", padx=10, pady=(0, 10))

        # Upscale
        f = self._section(scroll, "1) Resize Relative (Upscale)")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Scale (W=H):", anchor="w").pack(side="left")
        lbl_up = ctk.CTkLabel(h, text=f"{self.pp_upscale.get():.2f}×", text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_up.pack(side="right")
        ctk.CTkSlider(f, from_=1.0, to=4.0, number_of_steps=30, variable=self.pp_upscale,
                      command=lambda v: (lbl_up.configure(text=f"{v:.2f}×"), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 6))
        mrow_up = ctk.CTkFrame(f, fg_color="transparent"); mrow_up.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(mrow_up, text="Method:").pack(side="left")
        ctk.CTkComboBox(mrow_up, variable=self.pp_upscale_method, width=110, state="readonly",
                        values=["lanczos", "bicubic", "bilinear", "hamming", "box", "nearest"],
                        command=lambda _: self.update_pp_preview()).pack(side="left", padx=6)

        # Kuwahara
        f = self._section(scroll, "2) Kuwahara Blur")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Radius (0 = disabled):", anchor="w").pack(side="left")
        lbl_kw = ctk.CTkLabel(h, text=str(self.pp_kuwahara_radius.get()), text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_kw.pack(side="right")
        ctk.CTkSlider(f, from_=0, to=8, number_of_steps=8, variable=self.pp_kuwahara_radius,
                      command=lambda v: (lbl_kw.configure(text=str(int(v))), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 6))
        mrow_kw = ctk.CTkFrame(f, fg_color="transparent"); mrow_kw.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(mrow_kw, text="Method:").pack(side="left")
        ctk.CTkComboBox(mrow_kw, variable=self.pp_kuwahara_method, width=110, state="readonly",
                        values=["mean", "gaussian"],
                        command=lambda _: self.update_pp_preview()).pack(side="left", padx=6)

        # Median
        f = self._section(scroll, "3) Median Filter")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Size (kernel = 2*size+1, 0 = disabled):", anchor="w").pack(side="left")
        lbl_med = ctk.CTkLabel(h, text=str(self.pp_median_size.get()), text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_med.pack(side="right")
        ctk.CTkSlider(f, from_=0, to=5, number_of_steps=5, variable=self.pp_median_size,
                      command=lambda v: (lbl_med.configure(text=str(int(v))), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 10))

        # Downscale
        f = self._section(scroll, "4) Resize Relative (Downscale)")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Scale (W=H):", anchor="w").pack(side="left")
        lbl_dn = ctk.CTkLabel(h, text=f"{self.pp_downscale.get():.2f}×", text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_dn.pack(side="right")
        ctk.CTkSlider(f, from_=0.1, to=2.0, number_of_steps=38, variable=self.pp_downscale,
                      command=lambda v: (lbl_dn.configure(text=f"{v:.2f}×"), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 6))
        mrow_dn = ctk.CTkFrame(f, fg_color="transparent"); mrow_dn.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(mrow_dn, text="Method:").pack(side="left")
        ctk.CTkComboBox(mrow_dn, variable=self.pp_downscale_method, width=110, state="readonly",
                        values=["lanczos", "bicubic", "bilinear", "hamming", "box", "nearest"],
                        command=lambda _: self.update_pp_preview()).pack(side="left", padx=6)

        # Noise
        f = self._section(scroll, "5) Gaussian Noise")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Strength (0 = disabled):", anchor="w").pack(side="left")
        lbl_ns = ctk.CTkLabel(h, text=f"{self.pp_noise_strength.get():.2f}", text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_ns.pack(side="right")
        ctk.CTkSlider(f, from_=0.0, to=0.5, number_of_steps=50, variable=self.pp_noise_strength,
                      command=lambda v: (lbl_ns.configure(text=f"{v:.2f}"), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 6))
        ctk.CTkCheckBox(f, text="Monochromatic", variable=self.pp_noise_mono,
                        command=self.update_pp_preview).pack(anchor="w", padx=10, pady=2)
        ctk.CTkCheckBox(f, text="Invert", variable=self.pp_noise_invert,
                        command=self.update_pp_preview).pack(anchor="w", padx=10, pady=2)
        ch_frame = ctk.CTkFrame(f, fg_color="transparent")
        ch_frame.pack(anchor="w", fill="x", padx=10, pady=(4, 10))
        ctk.CTkLabel(ch_frame, text="Channels:").pack(side="left")
        ctk.CTkComboBox(ch_frame, variable=self.pp_noise_channels, width=90,
                        values=["rgb", "r", "g", "b", "rg", "rb", "gb"],
                        state="readonly",
                        command=lambda _: self.update_pp_preview()).pack(side="left", padx=6)

        # Sharpen
        f = self._section(scroll, "6) Sharpen (Unsharp Mask)")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Amount (0 = disabled):", anchor="w").pack(side="left")
        lbl_sha = ctk.CTkLabel(h, text=f"{self.pp_sharpen_amount.get():.2f}", text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_sha.pack(side="right")
        ctk.CTkSlider(f, from_=0.0, to=2.0, number_of_steps=40, variable=self.pp_sharpen_amount,
                      command=lambda v: (lbl_sha.configure(text=f"{v:.2f}"), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 6))
        sr_row = ctk.CTkFrame(f, fg_color="transparent"); sr_row.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(sr_row, text="Radius:").pack(side="left")
        ctk.CTkComboBox(sr_row, variable=self.pp_sharpen_radius, width=70, state="readonly",
                        values=["1", "2", "3"],
                        command=lambda _: self.update_pp_preview()).pack(side="left", padx=6)
        ctk.CTkLabel(sr_row, text="  Threshold:").pack(side="left")
        lbl_sth = ctk.CTkLabel(sr_row, text=str(self.pp_sharpen_threshold.get()), text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_sth.pack(side="right")
        ctk.CTkSlider(f, from_=0, to=15, number_of_steps=15, variable=self.pp_sharpen_threshold,
                      command=lambda v: (lbl_sth.configure(text=str(int(v))), self.update_pp_preview())).pack(fill="x", padx=10, pady=(0, 10))

        # HSB
        f = self._section(scroll, "7) Hue / Saturation / Brightness")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Hue shift (°):", anchor="w").pack(side="left")
        lbl_hue = ctk.CTkLabel(h, text=f"{self.pp_hsb_hue.get():.0f}°", text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_hue.pack(side="right")
        ctk.CTkSlider(f, from_=-15, to=15, number_of_steps=30, variable=self.pp_hsb_hue,
                      command=lambda v: (lbl_hue.configure(text=f"{v:.0f}°"), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 6))
        h2 = ctk.CTkFrame(f, fg_color="transparent"); h2.pack(fill="x", padx=10)
        ctk.CTkLabel(h2, text="Saturation:", anchor="w").pack(side="left")
        lbl_sat = ctk.CTkLabel(h2, text=f"{self.pp_hsb_sat.get():.2f}×", text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_sat.pack(side="right")
        ctk.CTkSlider(f, from_=0.5, to=1.5, number_of_steps=20, variable=self.pp_hsb_sat,
                      command=lambda v: (lbl_sat.configure(text=f"{v:.2f}×"), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 6))
        h3 = ctk.CTkFrame(f, fg_color="transparent"); h3.pack(fill="x", padx=10)
        ctk.CTkLabel(h3, text="Brightness:", anchor="w").pack(side="left")
        lbl_val = ctk.CTkLabel(h3, text=f"{self.pp_hsb_val.get():.2f}×", text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_val.pack(side="right")
        ctk.CTkSlider(f, from_=0.5, to=1.5, number_of_steps=20, variable=self.pp_hsb_val,
                      command=lambda v: (lbl_val.configure(text=f"{v:.2f}×"), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 10))

        # Chromatic Aberration
        f = self._section(scroll, "8) Chromatic Aberration")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Shift px (0 = disabled):", anchor="w").pack(side="left")
        lbl_ca = ctk.CTkLabel(h, text=str(self.pp_chroma_shift.get()), text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_ca.pack(side="right")
        ctk.CTkSlider(f, from_=0, to=8, number_of_steps=8, variable=self.pp_chroma_shift,
                      command=lambda v: (lbl_ca.configure(text=str(int(v))), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 10))

        # Vignette
        f = self._section(scroll, "9) Vignette")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Strength (0 = disabled):", anchor="w").pack(side="left")
        lbl_vig = ctk.CTkLabel(h, text=f"{self.pp_vignette_strength.get():.2f}", text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_vig.pack(side="right")
        ctk.CTkSlider(f, from_=0.0, to=1.0, number_of_steps=20, variable=self.pp_vignette_strength,
                      command=lambda v: (lbl_vig.configure(text=f"{v:.2f}"), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 6))
        h2 = ctk.CTkFrame(f, fg_color="transparent"); h2.pack(fill="x", padx=10)
        ctk.CTkLabel(h2, text="Feather:", anchor="w").pack(side="left")
        lbl_vf = ctk.CTkLabel(h2, text=f"{self.pp_vignette_feather.get():.1f}", text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_vf.pack(side="right")
        ctk.CTkSlider(f, from_=0.5, to=3.0, number_of_steps=25, variable=self.pp_vignette_feather,
                      command=lambda v: (lbl_vf.configure(text=f"{v:.1f}"), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 10))

        # JPEG simulation
        f = self._section(scroll, "10) JPEG Simulation")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Quality (100 = disabled):", anchor="w").pack(side="left")
        lbl_jq = ctk.CTkLabel(h, text=str(self.pp_jpeg_quality.get()), text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_jq.pack(side="right")
        ctk.CTkSlider(f, from_=60, to=100, number_of_steps=40, variable=self.pp_jpeg_quality,
                      command=lambda v: (lbl_jq.configure(text=str(int(v))), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 6))
        jsub_row = ctk.CTkFrame(f, fg_color="transparent"); jsub_row.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(jsub_row, text="Chroma subsampling:").pack(side="left")
        ctk.CTkComboBox(jsub_row, variable=self.pp_jpeg_subsampling, width=110, state="readonly",
                        values=["0 (4:4:4)", "1 (4:2:2)", "2 (4:2:0)"],
                        command=lambda _: self.update_pp_preview()).pack(side="left", padx=6)

        # Film Grain
        f = self._section(scroll, "11) Film Grain")
        h = ctk.CTkFrame(f, fg_color="transparent"); h.pack(fill="x", padx=10)
        ctk.CTkLabel(h, text="Strength (0 = disabled):", anchor="w").pack(side="left")
        lbl_gr = ctk.CTkLabel(h, text=f"{self.pp_grain_strength.get():.2f}", text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_gr.pack(side="right")
        ctk.CTkSlider(f, from_=0.0, to=0.3, number_of_steps=30, variable=self.pp_grain_strength,
                      command=lambda v: (lbl_gr.configure(text=f"{v:.2f}"), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 6))
        h2 = ctk.CTkFrame(f, fg_color="transparent"); h2.pack(fill="x", padx=10)
        ctk.CTkLabel(h2, text="Grain size:", anchor="w").pack(side="left")
        lbl_gs = ctk.CTkLabel(h2, text=str(self.pp_grain_size.get()), text_color=VALUE_FG, font=ctk.CTkFont(weight="bold")); lbl_gs.pack(side="right")
        ctk.CTkSlider(f, from_=1, to=8, number_of_steps=7, variable=self.pp_grain_size,
                      command=lambda v: (lbl_gs.configure(text=str(int(v))), self.update_pp_preview())).pack(fill="x", padx=10, pady=(4, 6))
        ctk.CTkCheckBox(f, text="Monochromatic", variable=self.pp_grain_mono,
                        command=self.update_pp_preview).pack(anchor="w", padx=10, pady=(2, 10))

        ctk.CTkButton(scroll, text="Reset to defaults",
                      fg_color="gray30", hover_color="gray25",
                      command=self._reset_postprocessing).pack(pady=10)

        # ── Preview canvas (same pattern as Watermark tab) ──────────────────
        frame_preview = self._section(parent, "Preview", fill="both", expand=True)

        # Collapse toggle
        self._pp_preview_collapsed = False
        self._pp_preview_frame = frame_preview

        def _toggle_pp_preview():
            self._pp_preview_collapsed = not self._pp_preview_collapsed
            if self._pp_preview_collapsed:
                zoom_row.pack_forget()
                self.pp_preview_canvas.pack_forget()
                frame_preview.pack(fill="x", expand=False, padx=10, pady=6)
                scroll.pack(fill="both", expand=True)
                scroll.configure(height=10)  # let geometry manager size it
                _toggle_btn.configure(text="▲ Show Preview")
            else:
                scroll.pack(fill="x", expand=False)
                scroll.configure(height=310)
                frame_preview.pack(fill="both", expand=True, padx=10, pady=6)
                zoom_row.pack(fill="x", padx=10, pady=(0, 2))
                self.pp_preview_canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))
                _toggle_btn.configure(text="▼ Hide Preview")
                self.update_pp_preview()

        _toggle_btn = ctk.CTkButton(
            frame_preview, text="▼ Hide Preview", anchor="w", height=28,
            fg_color="transparent", hover_color="gray20",
            command=_toggle_pp_preview)
        _toggle_btn.pack(fill="x", padx=10, pady=(0, 2))

        # Zoom label row
        zoom_row = ctk.CTkFrame(frame_preview, fg_color="transparent")
        zoom_row.pack(fill="x", padx=10, pady=(0, 2))
        ctk.CTkLabel(zoom_row, text="Scroll to zoom · 1× = fit · preview uses 1024px thumbnail",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(side="left")
        self._pp_zoom_label = ctk.CTkLabel(zoom_row, text="1×",
                                           text_color=VALUE_FG,
                                           font=ctk.CTkFont(size=11, weight="bold"))
        self._pp_zoom_label.pack(side="right")

        self.pp_preview_canvas = tk.Canvas(frame_preview, bg=PREVIEW_BG,
                                           highlightthickness=1,
                                           highlightbackground="#555555")
        self.pp_preview_canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.pp_preview_canvas.bind("<Configure>", lambda e: self.update_pp_preview())
        self.pp_preview_canvas.bind("<MouseWheel>",  self._pp_on_zoom)   # Windows
        self.pp_preview_canvas.bind("<Button-4>",    self._pp_on_zoom)   # Linux scroll up
        self.pp_preview_canvas.bind("<Button-5>",    self._pp_on_zoom)   # Linux scroll down
        self.pp_preview_canvas.bind("<ButtonPress-1>",  self._pp_pan_start)
        self.pp_preview_canvas.bind("<B1-Motion>",       self._pp_pan_move)

        self._pp_preview_job   = None
        self._pp_preview_photo = None
        self._pp_zoom          = 1.0
        self._pp_pan           = [0, 0]   # pixel offset in display space
        self._pp_drag_start    = None
        self._pp_src_cache     = None
        self._pp_out_cache     = None

    def _pp_pan_start(self, event) -> None:
        if self._pp_zoom > 1.0:
            self._pp_drag_start = (event.x, event.y)

    def _pp_pan_move(self, event) -> None:
        if self._pp_drag_start is None:
            return
        dx = event.x - self._pp_drag_start[0]
        dy = event.y - self._pp_drag_start[1]
        self._pp_drag_start = (event.x, event.y)
        self._pp_pan[0] += dx
        self._pp_pan[1] += dy
        self._render_pp_canvas()

    def _pp_on_zoom(self, event) -> None:
        delta = getattr(event, "delta", 0)
        if event.num == 4:   delta =  120
        if event.num == 5:   delta = -120
        step = 0.25 if delta > 0 else -0.25
        self._pp_zoom = max(1.0, min(8.0, self._pp_zoom + step))
        if self._pp_zoom == 1.0:
            self._pp_pan = [0, 0]
        self._pp_zoom_label.configure(text=f"{self._pp_zoom:.2f}×")
        self._render_pp_canvas()

    def update_pp_preview(self, *_args) -> None:
        if not hasattr(self, "pp_preview_canvas"):
            return
        if self._pp_preview_job is not None:
            self.root.after_cancel(self._pp_preview_job)
        self._pp_preview_job = self.root.after(300, self._run_pp_preview)

    def _run_pp_preview(self) -> None:
        self._pp_preview_job = None
        canvas = self.pp_preview_canvas
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return

        src_path = next(
            (f for f in self.files_to_process
             if os.path.splitext(f)[1].lower() in IMAGE_EXTS),
            None
        )
        if src_path is None:
            self._pp_src_cache = self._pp_out_cache = None
            canvas.delete("all")
            canvas.create_text(cw // 2, ch // 2,
                text="Add images to the list to preview filters",
                fill="#666", width=cw - 20)
            return

        cfg = self._current_pp_config()
        cfg["enabled"] = True

        def _worker():
            try:
                with Image.open(src_path) as im:
                    src = im.copy().convert("RGB")
                src.thumbnail((1024, 1024), Image.LANCZOS)
                out = apply_pipeline(src, cfg)
                return src, out
            except Exception as e:
                return None, str(e)

        def _done(result):
            src, out = result
            if src is None:
                canvas.delete("all")
                canvas.create_text(cw // 2, ch // 2, text=f"Error: {out}",
                                   fill="#f55", width=cw - 20)
                return
            self._pp_src_cache = src
            self._pp_out_cache = out
            self._render_pp_canvas()

        threading.Thread(target=lambda: self.root.after(0, _done, _worker()),
                         daemon=True).start()

    def _render_pp_canvas(self) -> None:
        """Composite src/out onto the canvas using the current zoom level."""
        if self._pp_src_cache is None or self._pp_out_cache is None:
            return
        canvas = self.pp_preview_canvas
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return

        src, out = self._pp_src_cache, self._pp_out_cache
        label_h = 20
        zoom = self._pp_zoom

        if zoom == 1.0:
            # Side-by-side fit view
            half_w = cw // 2 - 4
            fit_h  = ch - label_h

            def _fit(img):
                r = min(half_w / img.width, fit_h / img.height)
                return img.resize((max(1, int(img.width * r)),
                                   max(1, int(img.height * r))), Image.LANCZOS)

            left  = _fit(src)
            right = _fit(out)
            mid_y = (ch - label_h) // 2

            self._pp_preview_photo = (ImageTk.PhotoImage(left),
                                      ImageTk.PhotoImage(right))
            canvas.delete("all")
            canvas.create_image((cw // 2 - left.width) // 2,
                                 mid_y - left.height // 2,
                                 image=self._pp_preview_photo[0], anchor="nw")
            canvas.create_image(cw // 2 + (cw // 2 - right.width) // 2,
                                 mid_y - right.height // 2,
                                 image=self._pp_preview_photo[1], anchor="nw")
            canvas.create_line(cw // 2, 0, cw // 2, ch, fill="#555", dash=(4, 4))
            canvas.create_text(cw // 4,     ch - 10, text="Original",   fill="#888")
            canvas.create_text(cw * 3 // 4, ch - 10,
                               text=f"Processed  {out.width}×{out.height}px", fill="#888")
        else:
            # Zoom into the processed image — crop a window of size (cw/zoom, ch/zoom)
            # centred on the image + pan offset, then upscale to canvas size.
            avail_h = ch - label_h
            base_r  = min(cw / out.width, avail_h / out.height)
            disp_w  = int(out.width  * base_r)
            disp_h  = int(out.height * base_r)

            crop_w = max(1, int(disp_w / zoom))
            crop_h = max(1, int(disp_h / zoom))
            # Centre + pan offset (pan is in display pixels, clamp so crop stays in bounds)
            cx = disp_w // 2 - self._pp_pan[0]
            cy = disp_h // 2 - self._pp_pan[1]
            cx = max(crop_w // 2, min(disp_w - crop_w // 2, cx))
            cy = max(crop_h // 2, min(disp_h - crop_h // 2, cy))

            x0 = max(0, int((cx - crop_w // 2) / base_r))
            y0 = max(0, int((cy - crop_h // 2) / base_r))
            x1 = min(out.width,  x0 + max(1, int(crop_w / base_r)))
            y1 = min(out.height, y0 + max(1, int(crop_h / base_r)))

            region = out.crop((x0, y0, x1, y1)).resize(
                (max(1, int((x1 - x0) * base_r * zoom)),
                 max(1, int((y1 - y0) * base_r * zoom))),
                Image.LANCZOS
            )
            self._pp_preview_photo = (ImageTk.PhotoImage(region),)
            canvas.delete("all")
            canvas.create_image(cw // 2, avail_h // 2,
                                 image=self._pp_preview_photo[0], anchor="center")
            canvas.create_text(cw // 2, ch - 10,
                               text=f"Processed  {out.width}×{out.height}px  —  {zoom:.2f}×",
                               fill="#888")

    def _reset_postprocessing(self):
        d = DEFAULT_PIPELINE
        self.pp_enabled.set(d["enabled"])
        self.pp_jpeg_removal_strength.set(d["jpeg_removal_strength"])
        self.pp_upscale.set(d["upscale"])
        self.pp_upscale_method.set(d["upscale_method"])
        self.pp_kuwahara_radius.set(d["kuwahara_radius"])
        self.pp_median_size.set(d["median_size"])
        self.pp_downscale.set(d["downscale"])
        self.pp_downscale_method.set(d["downscale_method"])
        self.pp_noise_strength.set(d["noise_strength"])
        self.pp_noise_mono.set(d["noise_monochromatic"])
        self.pp_noise_invert.set(d["noise_invert"])
        self.pp_noise_channels.set(d["noise_channels"])
        self.pp_sharpen_amount.set(d["sharpen_amount"])
        self.pp_sharpen_radius.set(str(d["sharpen_radius"]))
        self.pp_sharpen_threshold.set(d["sharpen_threshold"])
        self.pp_hsb_hue.set(d["hsb_hue"])
        self.pp_hsb_sat.set(d["hsb_sat"])
        self.pp_hsb_val.set(d["hsb_val"])
        self.pp_chroma_shift.set(d["chroma_shift"])
        self.pp_vignette_strength.set(d["vignette_strength"])
        self.pp_vignette_feather.set(d["vignette_feather"])
        self.pp_jpeg_quality.set(d["jpeg_quality"])
        self.pp_jpeg_subsampling.set(str(d["jpeg_subsampling"]))
        self.pp_grain_strength.set(d["grain_strength"])
        self.pp_grain_size.set(d["grain_size"])
        self.pp_grain_mono.set(d["grain_mono"])

    def _build_autoname_tab(self, parent):
        ctk.CTkSwitch(parent, text="Enable metadata auto-naming",
                      variable=self.autoname_enabled).pack(anchor="w", padx=12, pady=10)
        ctk.CTkLabel(parent,
                     text="Renames image outputs from ComfyUI character tags.\n"
                          "Images only — videos keep their name.",
                     justify="left", anchor="w", text_color=MUTED).pack(
            anchor="w", padx=12)

        # Detailed explanation tucked behind a toggle to cut cognitive load.
        self._autoname_help_open = False
        self._autoname_help_btn = ctk.CTkButton(
            parent, text="\u25b8 How it works", anchor="w",
            fg_color="transparent", hover_color="gray20", height=28,
            command=self._toggle_autoname_help)
        self._autoname_help_btn.pack(fill="x", padx=10, pady=(8, 0))

        help_text = (
            "Before processing, a dialog shows candidate tags pulled from\n"
            "the positive prompt (tags before '1girl'/'1boy', after LoRA\n"
            "triggers). Check the ones that apply, type your own\n"
            "(comma-separated), or Skip to keep the original name.\n\n"
            "Output names become:\n"
            "  <char1>_<N>.png\n"
            "  <char1>+<char2>_<N>.png   (joined with '+' for easy parsing)\n"
            "<N> is a per-combination counter within the batch."
        )
        self._autoname_help_lbl = ctk.CTkLabel(
            parent, text=help_text, justify="left", anchor="w",
            text_color=MUTED, wraplength=400)

        # Library status + manage button
        lib_frame = ctk.CTkFrame(parent, fg_color="transparent")
        lib_frame.pack(fill="x", padx=12, pady=6)
        self.lib_status_lbl = ctk.CTkLabel(lib_frame, text="", text_color="gray")
        self.lib_status_lbl.pack(side="left")
        ctk.CTkButton(lib_frame, text="Manage Library", width=130,
                      command=self._manage_library).pack(side="right")
        self._update_library_status()

    def _toggle_autoname_help(self):
        """Expand/collapse the detailed auto-naming explanation."""
        self._autoname_help_open = not self._autoname_help_open
        if self._autoname_help_open:
            self._autoname_help_btn.configure(text="\u25be How it works")
            self._autoname_help_lbl.pack(fill="x", padx=12, pady=(2, 6),
                                         after=self._autoname_help_btn)
        else:
            self._autoname_help_btn.configure(text="\u25b8 How it works")
            self._autoname_help_lbl.pack_forget()

    def _update_library_status(self):
        n = len(self.character_library)
        if n == 0:
            self.lib_status_lbl.configure(
                text="📚 No learned characters yet. Selected tags will be auto-learned.")
        else:
            top = sorted(self.character_library.values(),
                         key=lambda x: -x["count"])[:3]
            names = ", ".join(e["tag"] for e in top)
            self.lib_status_lbl.configure(
                text=f"📚 {n} learned: {names}...")

    def _manage_library(self):
        """Dialog to view and remove learned character tags."""
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("Character Library")
        dlg.geometry("500x520")
        dlg.minsize(400, 360)
        dlg.transient(self.root)
        _cleanup_minimize_guard = self._make_dialog_minimize_safe(dlg)

        def _close_dialog():
            _cleanup_minimize_guard()
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", _close_dialog)

        ctk.CTkLabel(dlg, text="Learned character tags (★ = auto-selected in future)",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=10)

        # Scrollable list area using plain tk for speed
        list_container = ctk.CTkFrame(dlg)
        list_container.pack(fill="both", expand=True, padx=10, pady=5)

        canvas = tk.Canvas(list_container, highlightthickness=0,
                           bg=ctk.ThemeManager.theme["CTkFrame"]["fg_color"][1])
        scrollbar = ctk.CTkScrollbar(list_container, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=canvas["bg"])
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_configure(e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(inner_id, width=canvas.winfo_width())
        inner.bind("<Configure>", _on_configure)
        canvas.bind("<Configure>", _on_configure)

        def _populate():
            for w in inner.winfo_children():
                w.destroy()
            entries = sorted(self.character_library.items(), key=lambda kv: -kv[1]["count"])
            if not entries:
                tk.Label(inner, text="No characters learned yet.\nSelect tags during auto-naming and they'll appear here.",
                         bg=canvas["bg"], fg="gray", justify="center").pack(pady=20)
                return
            for key, entry in entries:
                row = tk.Frame(inner, bg=canvas["bg"])
                row.pack(fill="x", pady=1)
                tk.Label(row, text=f"★ {entry['tag']}", font=("", 10, "bold"),
                         fg="#2e7d32", bg=canvas["bg"], width=28, anchor="w").pack(side="left")
                tk.Label(row, text=f"×{entry['count']}", fg="gray",
                         bg=canvas["bg"], width=5).pack(side="left")
                tk.Button(row, text="✕", fg="white", bg=DANGER, relief="flat",
                          cursor="hand2", width=2,
                          command=lambda k=key: _remove(k)).pack(side="right", padx=2)

        def _remove(key):
            if key in self.character_library:
                del self.character_library[key]
                self._save_character_library()
                self._update_library_status()
                _populate()

        def _clear():
            if messagebox.askyesno("Clear Library",
                                   "Remove ALL learned character tags?\nThis cannot be undone.",
                                   parent=dlg):
                self.character_library.clear()
                self._save_character_library()
                self._update_library_status()
                _populate()

        _populate()

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(fill="x", padx=10, pady=10)
        ctk.CTkButton(btns, text="Clear All", fg_color=DANGER, hover_color="#a93226",
                      command=_clear).pack(side="left")
        ctk.CTkButton(btns, text="Close", command=_close_dialog,
                      width=100).pack(side="right")


    # ------------------------------------------------------------------
    # Auto-name dialog flow
    # ------------------------------------------------------------------
    def _build_candidate_checkboxes(self, parent, candidates: list[str]) -> tuple[list, "ctk.CTkLabel"]:
        """Populate candidate checkboxes inside parent frame; return (check_vars, count_lbl)."""
        check_vars: list[tuple[tk.BooleanVar, str]] = []
        count_lbl = ctk.CTkLabel(parent, text="", text_color="gray")
        count_lbl.pack(anchor="w", padx=10)

        def _update_count():
            n = sum(1 for v, _ in check_vars if v.get())
            count_lbl.configure(text="" if n == 0 else
                                ("✓ 1 character selected" if n == 1 else f"✓ {n} characters selected"))

        if not candidates:
            ctk.CTkLabel(parent, text="(no candidates detected)").pack(padx=10, pady=4)
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
            cb = ctk.CTkCheckBox(parent, text=label, variable=var, command=_update_count)
            if lib:
                cb.configure(text_color="#2e7d32", font=ctk.CTkFont(weight="bold"))
            cb.pack(fill="x", anchor="w", padx=10, pady=2)
        _update_count()
        return check_vars, count_lbl

    def _build_dialog_buttons(self, parent, result: dict, cancel_all: dict,
                               check_vars: list, custom_var: tk.StringVar,
                               dialog) -> None:
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

        ctk.CTkButton(parent, text="Apply", command=on_apply, width=120,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="right", padx=4)
        ctk.CTkButton(parent, text="Skip", command=on_skip, width=90,
                      fg_color="gray30", hover_color="gray25").pack(side="right", padx=4)
        ctk.CTkButton(parent, text="Cancel batch", command=on_cancel_all, width=120,
                      fg_color="gray30", hover_color="gray25").pack(side="left", padx=4)

    def _make_dialog_minimize_safe(self, dialog):
        """Keep a modal CTkToplevel restorable when the main window is minimized.

        On Windows a ``transient`` + ``grab_set`` dialog that gets minimized
        leaves an invisible window still holding the grab, which freezes the
        whole app (it can't be reached via the taskbar or Alt+Tab). This wires
        the dialog to follow the main window: the grab is released and the
        dialog hidden on minimize, then restored and re-grabbed when the main
        window comes back.

        Returns a cleanup callable that removes the bindings; call it before the
        dialog is destroyed.
        """
        def _acquire_grab(retries: int = 20):
            # grab_set fails on a not-yet-viewable window, so guard + retry and
            # never leave a grab dangling on a hidden window.
            if not dialog.winfo_exists():
                return
            if dialog.winfo_viewable():
                try:
                    dialog.grab_set()
                    dialog.focus_force()
                    return
                except tk.TclError:
                    pass
            if retries > 0:
                dialog.after(50, lambda: _acquire_grab(retries - 1))

        def _on_root_minimize(event=None):
            if event is not None and event.widget is not self.root:
                return
            if dialog.winfo_exists():
                try:
                    dialog.grab_release()
                except tk.TclError:
                    pass
                dialog.withdraw()

        def _on_root_restore(event=None):
            if event is not None and event.widget is not self.root:
                return
            if dialog.winfo_exists():
                dialog.deiconify()
                dialog.lift()
                _acquire_grab()

        map_id = self.root.bind("<Map>", _on_root_restore, add="+")
        unmap_id = self.root.bind("<Unmap>", _on_root_minimize, add="+")
        dialog.after(150, _acquire_grab)

        def _cleanup():
            try:
                self.root.unbind("<Map>", map_id)
                self.root.unbind("<Unmap>", unmap_id)
            except tk.TclError:
                pass

        return _cleanup

    def _prompt_character_for_image(self, image_path: str, candidates: list[str],
                                     prompt_text: str | None) -> list[str] | None:
        """Modal dialog: returns chosen character names, [] to skip, None to cancel batch."""
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Select characters")
        dialog.geometry("860x540")
        dialog.minsize(680, 440)
        dialog.transient(self.root)

        _cleanup_minimize_guard = self._make_dialog_minimize_safe(dialog)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)  # block closing via X

        result: dict = {"value": []}
        cancel_all: dict = {"value": False}

        # Buttons anchored to bottom of dialog — packed FIRST so they're never clipped
        btns = ctk.CTkFrame(dialog, fg_color="transparent")
        btns.pack(side="bottom", fill="x", padx=10, pady=10)

        body = ctk.CTkFrame(dialog, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        body.columnconfigure(0, weight=0)  # image column — fixed
        body.columnconfigure(1, weight=1)  # right column — stretches
        body.rowconfigure(0, weight=1)

        # ── Left: large image ──────────────────────────────────────────
        img_col = ctk.CTkFrame(body, fg_color="transparent")
        img_col.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        try:
            with Image.open(image_path) as im:
                im.thumbnail((400, 400))
                photo = ImageTk.PhotoImage(im.copy())
            lbl = tk.Label(img_col, image=photo, bd=0, highlightthickness=0)
            lbl.image = photo
            lbl.pack()
        except Exception:
            ctk.CTkLabel(img_col, text="(no preview)").pack()
        ctk.CTkLabel(img_col, text=os.path.basename(image_path),
                     font=ctk.CTkFont(size=11), wraplength=390,
                     justify="center").pack(pady=(6, 0))

        # ── Right: info + buttons ──────────────────────────────────────
        right_col = ctk.CTkFrame(body, fg_color="transparent")
        right_col.grid(row=0, column=1, sticky="nsew")

        # Candidates
        f_cand = ctk.CTkFrame(right_col, border_width=1, border_color="gray25")
        f_cand.pack(fill="x", padx=0, pady=(0, 6))
        ctk.CTkLabel(f_cand, text="Candidate tags — check all that apply",
                     font=ctk.CTkFont(size=14, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        check_vars, _ = self._build_candidate_checkboxes(f_cand, candidates)

        # Custom entry
        f_cust = ctk.CTkFrame(right_col, border_width=1, border_color="gray25")
        f_cust.pack(fill="x", padx=0, pady=(0, 6))
        ctk.CTkLabel(f_cust, text="Or type custom (comma-separated)",
                     font=ctk.CTkFont(size=14, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        custom_var = tk.StringVar()
        ctk.CTkEntry(f_cust, textvariable=custom_var).pack(fill="x", padx=10, pady=(0, 10))

        # Positive prompt — expands to fill remaining space
        f_pp = ctk.CTkFrame(right_col, border_width=1, border_color="gray25")
        f_pp.pack(fill="both", expand=True, padx=0, pady=(0, 6))
        ctk.CTkLabel(f_pp, text="Positive prompt (excerpt)",
                     font=ctk.CTkFont(size=14, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        txt = ctk.CTkTextbox(f_pp, wrap="word")
        txt.insert("1.0", (prompt_text or "")[:PROMPT_EXCERPT_LEN])
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self._build_dialog_buttons(btns, result, cancel_all, check_vars, custom_var, dialog)

        dialog.wait_window()
        _cleanup_minimize_guard()
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
        if self.processing:
            return  # guard against re-entry (button should already show Cancel)
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

        self.processing = True
        self._update_run_button_state()

        # Warn if post-processing is enabled but some files are videos
        # (post-processing pipeline is not applied to video frames).
        if self.pp_enabled.get():
            video_count = sum(
                1 for f in self.files_to_process
                if os.path.splitext(f)[1].lower() in VIDEO_EXTS)
            if video_count:
                messagebox.showwarning(
                    "Post-Processing + Video",
                    f"{video_count} video file(s) in the list will be watermarked "
                    f"but post-processing filters will NOT be applied to video.\n"
                    f"Only images are affected by the post-processing pipeline.")

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
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as e:
                raise RuntimeError(f"Cannot create output directory '{out_dir}': {e}") from e

        ok = 0
        errors: list[str] = []
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {
                executor.submit(self._process_single_image, fp, watermark, cfg, counters_lock): fp
                for fp in files
            }
            for fut in as_completed(futures):
                fp = futures[fut]
                try:
                    fut.result()
                    ok += 1
                except Exception:
                    logging.exception("Error processing %s", fp)
                    errors.append(os.path.basename(fp))
                self.root.after(0, lambda v=ok: self._set_progress(v, total_files))
                self.root.after(0, lambda v=ok: self.status_var.set(
                    f"Processing {v}/{total_files}..."))
                if self.cancel_event.is_set():
                    break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
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

            watermark2 = None
            wm2_path = self.watermark2_path.get().strip()
            if wm2_path and os.path.exists(wm2_path):
                try:
                    watermark2 = Image.open(wm2_path).convert("RGBA")
                except Exception as e:
                    logging.warning("Failed to load watermark2: %s", e)

            cfg = {
                "watermark_size": self.watermark_size.get(),
                "watermark_opacity": self.watermark_opacity.get(),
                "watermark_corner": self.watermark_corner.get(),
                "randomize_corner": self.randomize_corner.get(),
                "watermark2": watermark2,
                "watermark2_size": self.watermark2_size.get(),
                "watermark2_opacity": self.watermark2_opacity.get(),
                "watermark2_corner": self.watermark2_corner.get(),
                "watermark2_randomize": self.watermark2_randomize.get(),
                "pp_cfg": self._current_pp_config(),
                "custom_out": custom_out,
            }

            files = self.files_to_process
            total_files = len(files)
            self.root.after(0, lambda: self.status_var.set(f"Processing 0/{total_files}..."))
            self.root.after(0, lambda: self.progress.set(0))

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

        except Exception as e:
            logging.exception("Fatal error during batch")
            self.root.after(0, lambda m=str(e): messagebox.showerror("Error", f"Batch failed:\n{m}"))
            self.root.after(0, lambda: self.status_var.set("Error — batch aborted."))

        finally:
            self.processing = False
            self.cancel_event.clear()
            self.root.after(0, self._update_run_button_state)

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
        local = os.path.join(AImgKitApp._app_dir(), 'ffmpeg.exe')
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

            # Watermark 2 setup (optional)
            wm2_data = None
            wm2_obj = cfg.get("watermark2")
            if wm2_obj is not None:
                wm2_w_orig, wm2_h_orig = wm2_obj.size
                ref_dim2 = min(width, height)
                target_w2 = compute_watermark_width(ref_dim2, cfg["watermark2_size"])
                scale2 = target_w2 / wm2_w_orig
                wm2_resized = wm2_obj.resize(
                    (int(wm2_w_orig * scale2), int(wm2_h_orig * scale2)), resample=Image.LANCZOS)
                wm2_np = np.array(wm2_resized)
                wm2_bgr_f = wm2_np[:, :, :3][:, :, ::-1].astype(float)
                wm2_mask_f = (wm2_np[:, :, 3].astype(float) / 255.0) * (cfg["watermark2_opacity"] / 100)
                wm2_h_px, wm2_w_px = wm2_bgr_f.shape[:2]
                corner2 = cfg["watermark2_corner"]
                if cfg["watermark2_randomize"]:
                    corner2 = self.corner_selector.choose(exclude=corner)
                x2, y2 = position_for_corner(width, height, wm2_w_px, wm2_h_px, corner2, MARGIN)
                wm2_data = (wm2_bgr_f, wm2_mask_f, wm2_h_px, wm2_w_px, x2, y2)

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

                if wm2_data is not None:
                    b2, m2, h2, w2, px2, py2 = wm2_data
                    if frame.shape[0] >= py2 + h2 and frame.shape[1] >= px2 + w2:
                        roi2 = frame[py2:py2+h2, px2:px2+w2].astype(float)
                        for c in range(3):
                            roi2[:, :, c] = b2[:, :, c] * m2 + roi2[:, :, c] * (1 - m2)
                        frame[py2:py2+h2, px2:px2+w2] = roi2.astype(np.uint8)

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

    def _current_pp_config(self):
        # jpeg_subsampling stored as string "0 (4:4:4)" etc — extract leading int
        jsub_raw = self.pp_jpeg_subsampling.get()
        jsub = int(jsub_raw[0]) if jsub_raw else 2
        return {
            "enabled": self.pp_enabled.get(),
            "jpeg_removal_strength": self.pp_jpeg_removal_strength.get(),
            "upscale": self.pp_upscale.get(),
            "upscale_method": self.pp_upscale_method.get(),
            "kuwahara_radius": self.pp_kuwahara_radius.get(),
            "kuwahara_method": self.pp_kuwahara_method.get(),
            "median_size": self.pp_median_size.get(),
            "downscale": self.pp_downscale.get(),
            "downscale_method": self.pp_downscale_method.get(),
            "sharpen_amount": self.pp_sharpen_amount.get(),
            "sharpen_radius": int(self.pp_sharpen_radius.get()),
            "sharpen_threshold": self.pp_sharpen_threshold.get(),
            "hsb_hue": self.pp_hsb_hue.get(),
            "hsb_sat": self.pp_hsb_sat.get(),
            "hsb_val": self.pp_hsb_val.get(),
            "chroma_shift": self.pp_chroma_shift.get(),
            "vignette_strength": self.pp_vignette_strength.get(),
            "vignette_feather": self.pp_vignette_feather.get(),
            "jpeg_quality": self.pp_jpeg_quality.get(),
            "jpeg_subsampling": jsub,
            "grain_strength": self.pp_grain_strength.get(),
            "grain_size": self.pp_grain_size.get(),
            "grain_mono": self.pp_grain_mono.get(),
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

            # ── Second watermark (optional) ──
            wm2 = cfg.get("watermark2")
            if wm2 is not None:
                wm2_w, wm2_h = wm2.size
                ref_dim2 = min(result.width, result.height)
                target_w2 = compute_watermark_width(ref_dim2, cfg["watermark2_size"])
                wm2_resized = wm2.resize(
                    (int(wm2_w * (target_w2 / wm2_w)), int(wm2_h * (target_w2 / wm2_w))),
                    resample=Image.LANCZOS)
                if cfg["watermark2_opacity"] < 100:
                    wm2_resized = wm2_resized.copy()
                    wm2_resized.putalpha(wm2_resized.split()[3].point(
                        lambda p: int(p * cfg["watermark2_opacity"] / 100)))
                corner2 = cfg["watermark2_corner"]
                if cfg["watermark2_randomize"]:
                    corner2 = self.corner_selector.choose(exclude=corner)
                layer2 = Image.new("RGBA", result.size, (0, 0, 0, 0))
                pos2 = self.get_watermark_position(result, wm2_resized, corner2)
                layer2.paste(wm2_resized, pos2, wm2_resized)
                result = Image.alpha_composite(result, layer2)
                del layer2, wm2_resized

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
    ctk.set_appearance_mode("dark")          # "dark" | "light" | "system"
    ctk.set_default_color_theme("green")     # matches the ACCENT action color
    root = CTkDnD()
    app = AImgKitApp(root)
    root.mainloop()
