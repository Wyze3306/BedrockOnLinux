"""bol.gui — the desktop GUI (customtkinter: modern, rounded, self-contained)."""
# SPDX-License-Identifier: MIT

import base64
import os
import shutil
import subprocess
import re
import sys
import threading
import zipfile
from pathlib import Path
from PIL import Image, ImageDraw

from .auth import (
    NativeAuth,
    msa_logout,
    msa_signed_in,
    msa_gamertag,
)
from .config import (
    LOGS,
    PRETTY,
    VERSION,
    get_install_location,
    set_install_location,
    clear_install_location,
    default_install_location,
)
from .content import _mojang_dir, import_content
from .games import list_mc_versions
from .gamesetup import do_setup
from .inject import run_injector
from .launch import launch
from . import log
from .log import BolError, _LEVELS, warn
from .prefix import (
    _mc_running,
    kill_wine,
    prefix_operation_lock,
    reset_prefix,
)
from .update import check_for_update, self_update
from .util import load_settings, save_settings

RE_MD_TOKENS = re.compile(r"(\*\*|`|__|\[[^\]]+\]\([^)]+\))")
RE_MD_LINK = re.compile(r"^\[([^\]]+)\]\(([^)]+)\)$")


def _desktop_error(message):
    warn(message)
    notifier = shutil.which("notify-send")
    if notifier:
        try:
            subprocess.run(
                [notifier, "--app-name", PRETTY, PRETTY, message],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------
class Theme:
    BG          = ("#f2f4f7", "#0f1115")
    FG          = ("#0f1115", "#f2f4f7")
    SUB         = ("#5d6577", "#8b93a7")
    MUTED       = ("#8b93a7", "#5d6577")

    CARD        = ("#ffffff", "#181b22")
    CARD_2      = ("#e8ebf0", "#20242e")
    CARD_3      = ("#d8dce4", "#2a2f3b")
    
    BORDER      = ("#d0d4dc", "#2a2e38")

    RED         = ("#d35446", "#e2685a")
    RED_HOV     = ("#bc4a3d", "#ea7c70")
    RED_DIM     = ("#fceceb", "#341f1d")
    RED_LIGHT   = ("#451c17", "#f2b3ac")
    RED_MUTED   = ("#b06259", "#c98279")
    RED_DARK    = ("#f2b3ac", "#451c17")

    GREEN       = ("#43a047", "#43a047")
    GREEN_HOV   = ("#3b8e3f", "#4fc153")
    GREEN_DIM   = ("#e6f4e6", "#1c2c1c")
    GREEN_LIGHT = ("#33421f", "#cfe8c2")
    GREEN_MUTED = ("#6e8a69", "#9fb89a")
    GREEN_DARK  = ("#cfe8c2", "#33421f")

    BLUE        = ("#3b7bc4", "#5b9bd9")
    BLUE_HOV    = ("#2a6bb4", "#6caae6")
    BLUE_DIM    = ("#e6f0fa", "#16202c")
    BLUE_LIGHT  = ("#1f3342", "#c2d6e8")
    BLUE_MUTED  = ("#69829e", "#9ab3c9")
    BLUE_DARK   = ("#c2d6e8", "#1f3342")

    GOLD        = ("#d8a230", "#e3b34a")
    GOLD_HOV    = ("#c2912a", "#f3c35a")
    GOLD_DIM    = ("#fcf3e1", "#33291a")
    GOLD_LIGHT  = ("#473510", "#f2ce79")
    GOLD_MUTED  = ("#b58c31", "#c9a657")
    GOLD_DARK   = ("#f2ce79", "#473510")

    BROWN       = ("#8c6446", "#a67958")
    BROWN_HOV   = ("#75533a", "#bf8d69")
    BROWN_DIM   = ("#f5ede8", "#2b1c12")
    BROWN_LIGHT = ("#302217", "#d6b49c")
    BROWN_MUTED = ("#805e45", "#a68267")
    BROWN_DARK  = ("#d6b49c", "#302217")

    CONSOLE_BG  = ("#f8f9fa", "#0b0d11")
    CONSOLE_FG  = ("#3b8e3f", "#7fd97f")

    @classmethod
    def r(cls, color):
        import customtkinter as ctk
        if isinstance(color, tuple):
            return color[0] if ctk.get_appearance_mode() == "Light" else color[1]
        return color


def _create_play_icon(size=16, fg_color="white", bg_color="black"):
    import tkinter as tk
    fg = Theme.r(fg_color)
    bg = Theme.r(bg_color)
    if not isinstance(fg, str): fg = fg[0] if isinstance(fg, tuple) else "#ffffff"
    if not isinstance(bg, str): bg = bg[0] if isinstance(bg, tuple) else "#000000"
    if fg == "white": fg = "#ffffff"
    if fg == "black": fg = "#000000"
    if bg == "white": bg = "#ffffff"
    if bg == "black": bg = "#000000"
    
    data = []
    for y in range(size):
        row = []
        for x in range(size):
            if 5 <= x <= 15:
                dy = abs(y - size/2.0)
                max_dy = (15 - x) * 0.5
                if dy <= max_dy + 0.5:
                    row.append(fg)
                else:
                    row.append(bg)
            else:
                row.append(bg)
        data.append(row)
    img = tk.PhotoImage(width=size, height=size)
    img.put(data)
    return img

def _create_kill_icon(size=16, fg_color="white", bg_color="black"):
    import tkinter as tk
    fg = Theme.r(fg_color)
    bg = Theme.r(bg_color)
    if not isinstance(fg, str): fg = fg[0] if isinstance(fg, tuple) else "#ffffff"
    if not isinstance(bg, str): bg = bg[0] if isinstance(bg, tuple) else "#000000"
    if fg == "white": fg = "#ffffff"
    if fg == "black": fg = "#000000"
    if bg == "white": bg = "#ffffff"
    if bg == "black": bg = "#000000"
    
    data = []
    pad = 3
    thickness = 2
    for y in range(size):
        row = []
        for x in range(size):
            if pad <= x <= size - pad and pad <= y <= size - pad:
                d1 = abs(x - y)
                d2 = abs(x - (size - 1 - y))
                if d1 <= thickness or d2 <= thickness:
                    row.append(fg)
                else:
                    row.append(bg)
            else:
                row.append(bg)
        data.append(row)
    img = tk.PhotoImage(width=size, height=size)
    img.put(data)
    return img

def gui():
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        _desktop_error(
            "The launcher needs XWayland. Install or enable XWayland, then "
            "open BedrockOnLinux again; command-line tools remain available.")
        return
    from .deps import ensure_gui_deps
    ensure_gui_deps()
    try:
        import tkinter as tk
        from tkinter import messagebox
        import customtkinter as ctk
    except Exception as e:
        _desktop_error(
            f"GUI toolkit unavailable ({e}). Install python3-tk and "
            "customtkinter, or use the command line.")
        return

    T = Theme
    from .util import load_settings, save_settings
    s = load_settings()
    _init_beta = s.get("ui_is_beta", False)
    T.THEME_ACCENT = T.GOLD if _init_beta else T.GREEN
    T.THEME_HOV    = T.GOLD_HOV if _init_beta else T.GREEN_HOV
    T.THEME_DIM    = T.GOLD_DIM if _init_beta else T.GREEN_DIM

    ctk.set_appearance_mode("Light" if s.get("light_theme", False) else "Dark")
    try:
        root = ctk.CTk(className=PRETTY)
    except Exception as e:
        _desktop_error(
            f"No usable X11/XWayland display ({e}). Enable XWayland or use "
            "the command line.")
        return
    root.title(PRETTY)
    root.geometry("980x650")
    root.minsize(860, 640)
    root.configure(fg_color=T.BG)

    def font(size=13, weight="normal", family=None):
        return ctk.CTkFont(size=size, weight=weight, family=family)

    MONO = "monospace"

    icon_img = None
    here = Path(__file__).resolve().parent
    for p in (here.parent / "data/icon.png",
              here / "data/icon.png",
              Path("/app/share/icons/hicolor/256x256/apps/") /
              "io.github.wyze3306.BedrockOnLinux.png",
              Path("/usr/lib/bedrock-on-linux/data/icon.png"),
              Path("/usr/share/icons/hicolor/256x256/apps/bedrock-on-linux.png")):
        if p.exists():
            try:
                icon_img = tk.PhotoImage(file=str(p))
                root.iconphoto(True, icon_img)
                root._icon = icon_img
                break
            except Exception:
                pass
    if icon_img is None:
        try:
            with zipfile.ZipFile(Path(sys.argv[0])) as archive:
                encoded = base64.b64encode(archive.read("data/icon.png"))
            icon_img = tk.PhotoImage(data=encoded)
            root.iconphoto(True, icon_img)
            root._icon = icon_img
        except (OSError, KeyError, zipfile.BadZipFile, tk.TclError):
            pass

    def logo_label(parent, px, bg):
        """A scaled logo image in a ctk.CTkLabel."""
        if icon_img is None:
            return None
        try:
            im = icon_img.subsample(max(1, icon_img.width() // px))
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                lab = ctk.CTkLabel(parent, image=im, text="", fg_color=bg)
            lab.image = im
            return lab
        except Exception:
            return None

    def mkbtn(parent, text, cmd, kind="ghost", **kw):
        base = {
            "play":    dict(fg_color=T.THEME_ACCENT, hover_color=T.THEME_HOV,
                             text_color="white"),
            "primary": dict(fg_color=T.THEME_ACCENT, hover_color=T.THEME_HOV,
                             text_color="white"),
            "danger":  dict(fg_color=T.RED, hover_color=T.RED_HOV,
                             text_color="white"),
            "ghost":   dict(fg_color=T.CARD_2, hover_color=T.CARD_3,
                             text_color=T.FG),
            "flat":    dict(fg_color="transparent", hover_color=T.CARD_2,
                             text_color=T.SUB),
        }[kind]
        opts = dict(corner_radius=10, font=font(13), command=cmd)
        opts.update(base)
        opts.update(kw)
        return ctk.CTkButton(parent, text=text, **opts)

    def center_over_root(win, w, h):
        root.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    def dialog(title, w, h):
        """A CTkToplevel with consistent chrome: centered, dark, Esc-to-close."""
        d = ctk.CTkToplevel(root)
        d.title(title)
        d.configure(fg_color=T.CARD)
        d.transient(root)
        d.resizable(False, False)
        center_over_root(d, w, h)
        d.after(120, d.lift)
        d.bind("<Escape>", lambda e: d.destroy())
        return d

    class Tooltip:
        """Small delayed hover label for icon-only buttons."""

        def __init__(self, widget, text):
            self.widget, self.text, self.win, self._job = widget, text, None, None
            widget.bind("<Enter>", self._schedule, add="+")
            widget.bind("<Leave>", self._hide, add="+")

        def _schedule(self, _e=None):
            self._job = root.after(500, self._show)

        def _show(self):
            if self.win is not None:
                return
            widget_y = self.widget.winfo_rooty() - root.winfo_rooty()
            x = self.widget.winfo_rootx() - root.winfo_rootx() + self.widget.winfo_width() // 2
            
            if widget_y > root.winfo_height() // 2:
                y = widget_y - 8
                anchor = "s"
            else:
                y = widget_y + self.widget.winfo_height() + 8
                anchor = "n"
            
            self.win = ctk.CTkFrame(root, fg_color=T.CARD_3, corner_radius=6)
            lab = ctk.CTkLabel(self.win, text=self.text, fg_color="transparent", text_color=T.FG,
                               font=font(10))
            lab.pack(padx=8, pady=4)
            self.win.place(x=x, y=y, anchor=anchor)
            self.win.lift()

        def _hide(self, _e=None):
            if self._job:
                root.after_cancel(self._job)
                self._job = None
            if self.win is not None:
                self.win.destroy()
                self.win = None

    na = NativeAuth()
    ui = {"versions": [], "labels": [], "busy": False, "details": False,
          "launch_active": False, "changelog_active": False,
          "changelogs_loaded": False, "changelog_head": None, "settings_head": None}
    tab_game = None
    tab_launcher = None

    # ==================================================================
    # Top bar: brand + account
    # ==================================================================
    top = ctk.CTkFrame(root, fg_color="transparent")
    top.pack(fill="x", padx=22, pady=(18, 8))

    brand = ctk.CTkFrame(top, fg_color=T.CARD, corner_radius=14)
    brand.pack(side="left")
    icon_btn = ctk.CTkFrame(brand, fg_color=T.CARD_2, corner_radius=8)
    icon_btn.pack(side="left", padx=(6, 3), pady=5)
    
    ll = logo_label(icon_btn, 24, T.CARD_2)
    if not ll:
        ll = ctk.CTkLabel(icon_btn, text="GitHub", fg_color=T.CARD_2, text_color=T.SUB, font=("sans-serif", 10, "bold"))
    ll.pack(padx=6, pady=6)

    brand_lbl = ctk.CTkLabel(brand, text="What's New", font=font(16, "bold"),
                             text_color=T.FG)
    brand_lbl.pack(side="left", padx=(3, 6), pady=8)
    
    brand_lbl.bind("<Button-1>", lambda e: toggle_changelog())
    brand_lbl.bind("<Enter>", lambda e: brand_lbl.configure(text_color=T.THEME_ACCENT))
    brand_lbl.bind("<Leave>", lambda e: brand_lbl.configure(text_color=T.FG))
    Tooltip(brand_lbl, "Changelog")

    ctk.CTkLabel(brand, text=f"v{VERSION}", font=font(12, "bold"), text_color=T.SUB
                 ).pack(side="left", padx=(0, 10), pady=8)

    def _open_github(_e=None):
        subprocess.Popen(
            ["xdg-open", "https://github.com/Wyze3306/BedrockOnLinux"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
    def _icon_hover(on):
        c = T.CARD_3 if on else T.CARD_2
        icon_btn.configure(fg_color=c)
        if ll:
            ll.configure(fg_color=c)
            
    for w in [icon_btn, ll]:
        w.bind("<Button-1>", _open_github)
        w.bind("<Enter>", lambda e: _icon_hover(True))
        w.bind("<Leave>", lambda e: _icon_hover(False))
        Tooltip(w, "GitHub")

    acct = ctk.CTkFrame(top, fg_color=T.CARD, corner_radius=14)
    acct.pack(side="right")
    acct_dot = ctk.CTkLabel(acct, text="●", text_color=T.SUB, font=font(12),
                             width=10)
    acct_dot.pack(side="left", padx=(14, 4), pady=8)
    acct_txt = tk.StringVar(value="Not signed in")
    acct_txt_lbl = ctk.CTkLabel(acct, textvariable=acct_txt, text_color=T.FG,
                 font=font(13))
    acct_txt_lbl.pack(side="left", padx=(0, 8))
    acct_btn = mkbtn(acct, "Sign in", lambda: acct_click(), kind="ghost",
                      width=88, height=30, font=font(12, "bold"))
    acct_btn.pack(side="left", padx=(0, 8), pady=8)
    
    acct_txt_lbl._tooltip = Tooltip(acct_txt_lbl, "")
    acct_btn._tooltip = Tooltip(acct_btn, "")

    # ==================================================================
    # View area
    # ==================================================================
    view_area = ctk.CTkFrame(root, fg_color="transparent")
    view_area.pack(fill="both", expand=True, padx=22, pady=6)

    hero = ctk.CTkFrame(view_area, fg_color=T.CARD, corner_radius=18,
                         border_width=1, border_color=T.BORDER)
    hw = ctk.CTkFrame(hero, fg_color="transparent")
    hw.place(relx=0.5, rely=0.44, anchor="center")
    hl = logo_label(hw, 118, T.CARD)
    if hl:
        hl.pack()
    ctk.CTkLabel(hw, text="Minecraft Bedrock", font=font(28, "bold"),
                 text_color=T.FG).pack(pady=(16, 2))
    ctk.CTkLabel(hw, text="Bedrock Edition for Linux", font=font(13),
                 text_color=T.SUB).pack()

    selected_chip = ctk.CTkLabel(
        hw, text="", font=font(12, "bold"), text_color=T.THEME_ACCENT,
        fg_color=T.THEME_DIM, bg_color=T.CARD, corner_radius=8)
    selected_chip.pack(pady=(14, 0))

    # ==================================================================
    # Status + progress
    # ==================================================================
    status = ctk.CTkFrame(root, fg_color="transparent")
    status.pack(fill="x", padx=26, pady=(4, 0))
    status_txt = tk.StringVar(value="Select a version to play.")
    status_lbl = ctk.CTkLabel(status, textvariable=status_txt, text_color=T.SUB,
                               font=font(12), anchor="w")
    status_lbl.pack(fill="x")
    prog = ctk.CTkProgressBar(status, height=8, corner_radius=4,
                               progress_color=T.THEME_ACCENT, fg_color=T.CARD_2)
    prog.set(0)

    # ==================================================================
    # Control dock: version picker · details · settings · play
    # ==================================================================
    dock = ctk.CTkFrame(root, fg_color=T.CARD, corner_radius=16,
                         border_width=1, border_color=T.BORDER)
    dock.pack(fill="x", padx=22, pady=(10, 16))
    
    _sash_state = {"y": 0, "h": 220, "max_h": 600}
    def sash_click(e):
        if not ui.get("details"): return
        _sash_state["y"] = e.y_root
        _sash_state["h"] = detwrap.winfo_height()
        allowable = view_area.winfo_height() - 340
        _sash_state["max_h"] = _sash_state["h"] + max(0, allowable)
    def sash_drag(e):
        if not ui.get("details"): return
        new_h = _sash_state["h"] - (e.y_root - _sash_state["y"])
        if new_h < 100: new_h = 100
        if new_h > _sash_state["max_h"]: new_h = _sash_state["max_h"]
        detwrap.configure(height=new_h)
        root.minsize(860, 640 + new_h)
        
    for _w in (status, status_lbl, prog, getattr(prog, "_canvas", prog)):
        _w.bind("<Button-1>", sash_click, add="+")
        _w.bind("<B1-Motion>", sash_drag, add="+")

    bar = ctk.CTkFrame(dock, fg_color="transparent")
    bar.pack(fill="x", padx=16, pady=14)

    vbox = ctk.CTkFrame(bar, fg_color="transparent")
    vbox.pack(side="left")
    mc_var = tk.StringVar(value="")

    _pick = {"win": None, "hover": False, "bind_id": None}

    def close_picker():
        bid = _pick.get("bind_id")
        if bid:
            root.unbind("<Configure>", bid)
            _pick["bind_id"] = None
            
        try:
            ver_arrow.configure(text="▾")
            if not _pick.get("hover"):
                ver_field.configure(fg_color=T.CARD_2)
                ver_lbl.configure(text_color=T.FG)
        except NameError:
            pass
        w = _pick["win"]
        _pick["win"] = None
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass

    def set_version(label):
        mc_var.set(label or "")
        _update_selected_chip()
        close_picker()

    def _sync_theme(w, is_beta=None):
        if w == acct_dot:
            return
        if is_beta is None:
            lab = mc_var.get()
            is_beta = "BETA" in lab if lab else False
            
        c_new = T.GOLD if is_beta else T.GREEN
        h_new = T.GOLD_HOV if is_beta else T.GREEN_HOV
        d_new = T.GOLD_DIM if is_beta else T.GREEN_DIM
        c_old = T.GREEN if is_beta else T.GOLD
        h_old = T.GREEN_HOV if is_beta else T.GOLD_HOV
        d_old = T.GREEN_DIM if is_beta else T.GOLD_DIM
        
        T.THEME_ACCENT = c_new
        T.THEME_HOV = h_new
        T.THEME_DIM = d_new
        
        for attr in ("fg_color", "text_color", "progress_color", "hover_color", "segmented_button_selected_color", "segmented_button_selected_hover_color"):
            try:
                cur = w.cget(attr)
                if cur == c_old:
                    w.configure(**{attr: c_new})
                elif cur == h_old:
                    w.configure(**{attr: h_new})
                elif cur == d_old:
                    w.configure(**{attr: d_new})
            except Exception:
                pass
        if hasattr(w, "_segmented_button"):
            try: w._segmented_button.configure(selected_color=c_new, selected_hover_color=h_new)
            except Exception: pass
        if hasattr(w, "tag_configure"):
            try: w.tag_configure("link", foreground=T.r(c_new))
            except Exception: pass
            try: w.tag_configure("release_title", foreground=T.r(c_new))
            except Exception: pass
        elif hasattr(w, "_textbox"):
            try: w._textbox.tag_configure("link", foreground=T.r(c_new))
            except Exception: pass
            try: w._textbox.tag_configure("release_title", foreground=T.r(c_new))
            except Exception: pass
            
        if w.__class__.__name__ == "Text" and getattr(w, "_is_logbox", False):
            try:
                w.configure(bg=T.r(T.CONSOLE_BG), fg=T.r(T.CONSOLE_FG), insertbackground=T.r(T.FG))
            except Exception:
                pass
            
        if getattr(w, "_is_play_btn", False):
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    if ui.get("busy"):
                        w.configure(fg_color=T.RED, hover_color=T.RED, text_color=T.FG)
                        w._img_norm = _create_kill_icon(16, T.FG, T.RED)
                    else:
                        w.configure(fg_color=T.THEME_ACCENT, hover_color=T.THEME_ACCENT, text_color=T.FG)
                        w._img_norm = _create_play_icon(16, T.FG, T.THEME_ACCENT)
                    w.configure(image=w._img_norm)
            except Exception:
                pass
            
        for child in w.winfo_children():
            _sync_theme(child, is_beta)

    def _update_selected_chip():
        lab = mc_var.get()
        if not lab:
            selected_chip.configure(text="")
            return
        is_beta = "BETA" in lab
        
        s = load_settings()
        changed = False
        if s.get("ui_is_beta") != is_beta:
            s["ui_is_beta"] = is_beta
            changed = True
            
        cur_mc_ver = lab.split('  ')[0]
        if s.get("mc_version") != cur_mc_ver:
            s["mc_version"] = cur_mc_ver
            changed = True
            
        if changed:
            save_settings(s)
            
        selected_chip.configure(
            text=f"  {lab.split('  ')[0]}"
                 f"{'  ·  BETA' if is_beta else ''}  ",
            text_color=T.GOLD if is_beta else T.GREEN,
            fg_color=T.GOLD_DIM if is_beta else T.GREEN_DIM)
            
        try:
            active = _pick.get("hover") or _pick.get("win")
            ver_lbl.configure(text_color=T.THEME_ACCENT if active else T.FG)
            ver_arrow.configure(text_color=T.SUB)
        except Exception:
            pass
            
        try:
            if hasattr(play_btn, "_tooltip"):
                is_kill = "Kill" in play_btn._tooltip.text
                play_btn._tooltip.text = f"{'Kill' if is_kill else 'Play'} {cur_mc_ver}"
        except Exception:
            pass

        _sync_theme(root, is_beta)
        
        if ui.get("changelogs_loaded") and tab_game is not None and changelog_view.winfo_ismapped():
            from .util import mc_releases
            load_tab_changelog(tab_game, lambda: mc_releases(fetch_all=False), render_game_changelog)

    def open_picker():
        if _pick["win"] is not None:
            close_picker()
            return
        labels = ui.get("labels") or []
        if not labels:
            return
        x = ver_field.winfo_rootx() - root.winfo_rootx()
        y = ver_field.winfo_rooty() - root.winfo_rooty()
        w = ver_field.winfo_width()
        
        s = load_settings()
        saved_h = s.get("picker_height")
        if saved_h is not None:
            h = min(max(100, int(saved_h)), y - 24)
        else:
            h = min(360, 40 + 32 * min(len(labels), 8))
        
        win = ctk.CTkFrame(root, width=w, height=h, fg_color=T.CARD_2, bg_color=T.CARD, border_width=1, border_color=T.BORDER, corner_radius=12)
        _pick["win"] = win
        win.pack_propagate(False)
        win.place(x=x, y=y - h - 4)
        win.lift()
        
        def drag_resize(event):
            cur_y = ver_field.winfo_rooty() - root.winfo_rooty()
            mouse_y = event.y_root - root.winfo_rooty()
            mouse_y = max(24, min(mouse_y, cur_y - 4 - 100))
            new_h = cur_y - 4 - mouse_y
            win.configure(height=new_h)
            win.place(y=mouse_y)
            
        def end_drag(event):
            cur_y = ver_field.winfo_rooty() - root.winfo_rooty()
            mouse_y = event.y_root - root.winfo_rooty()
            mouse_y = max(24, min(mouse_y, cur_y - 4 - 100))
            new_h = cur_y - 4 - mouse_y
            s2 = load_settings()
            s2["picker_height"] = new_h
            save_settings(s2)
            
        def update_position(e=None):
            if _pick["win"] != win: return
            try:
                cur_x = ver_field.winfo_rootx() - root.winfo_rootx()
                cur_y = ver_field.winfo_rooty() - root.winfo_rooty()
                cur_h = win.cget("height")
                win.place(x=cur_x, y=cur_y - int(cur_h) - 4)
            except Exception:
                pass
                
        _pick["bind_id"] = root.bind("<Configure>", update_position, add="+")
            
        grip_container = ctk.CTkFrame(win, width=w - 40, height=18, fg_color="transparent", cursor="sb_v_double_arrow")
        grip_container.pack(side="top", pady=(2, 0))
        grip_container.pack_propagate(False)
        
        grip = ctk.CTkFrame(grip_container, width=40, height=4, fg_color=T.BORDER, corner_radius=2)
        grip.place(relx=0.5, rely=0.5, anchor="center")
        
        for widget in (grip_container, grip):
            widget.bind("<B1-Motion>", drag_resize)
            widget.bind("<ButtonRelease-1>", end_drag)
            widget.bind("<Button-1>", lambda e: search.focus_set())

        ver_arrow.configure(text="▴")

        search = ctk.CTkEntry(win, placeholder_text="Filter versions…",
                               fg_color=T.CARD_3, border_width=0,
                               text_color=T.FG, corner_radius=8, height=30,
                               font=font(12))
        search.pack(fill="x", padx=6, pady=(6, 4))
        search.focus_set()

        sf = ctk.CTkScrollableFrame(win, fg_color=T.CARD_2, corner_radius=8)
        sf.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        cur = mc_var.get()
        
        _pick["no_match"] = ctk.CTkLabel(sf, text="No matches", text_color=T.MUTED, font=font(12))
        _pick["buttons"] = []
        for lab in labels:
            is_beta = "BETA" in lab
            c_bg = T.GOLD if is_beta else T.GREEN
            c_dim = T.GOLD_DIM if is_beta else T.GREEN_DIM
            
            row = ctk.CTkButton(
                sf, text=lab, anchor="w", height=30, corner_radius=6,
                fg_color=c_dim if lab == cur else "transparent",
                hover_color=c_dim,
                text_color=c_bg if lab == cur else T.FG,
                font=font(12), command=lambda l=lab: set_version(l))
            if lab != cur:
                def _enter(e, r=row, c=c_bg): r.configure(text_color=c)
                def _leave(e, r=row): r.configure(text_color=T.FG)
                row.bind("<Enter>", _enter, add="+")
                row.bind("<Leave>", _leave, add="+")
            _pick["buttons"].append((lab, row))
            row.pack(fill="x", pady=1)

        def rebuild(_e=None):
            q = search.get().strip().lower()
            shown_any = False
            for lab, row in _pick["buttons"]:
                if not q or q in lab.lower():
                    row.pack(fill="x", pady=1)
                    shown_any = True
                else:
                    row.pack_forget()
                    
            if not shown_any:
                _pick["no_match"].pack(pady=10)
            else:
                _pick["no_match"].pack_forget()
                
        def on_enter(_e=None):
            q = search.get().strip().lower()
            shown = [lab for lab in labels if q in lab.lower()] if q else labels
            if shown:
                set_version(shown[0])
                return "break"
            return "break"

        search.bind("<KeyRelease>", rebuild)
        search.bind("<Return>", on_enter)
        search.bind("<Escape>", lambda e: close_picker())
        search.bind("<KeyRelease>", rebuild)
        search.bind("<Return>", on_enter)
        search.bind("<Escape>", lambda e: close_picker())
        
        rebuild()
        win.bind("<Escape>", lambda e: close_picker())
        
    def global_click(event):
        w = _pick.get("win")
        if w is None:
            return
        try:
            wx, wy = w.winfo_rootx(), w.winfo_rooty()
            ww, wh = w.winfo_width(), w.winfo_height()
            vx, vy = ver_field.winfo_rootx(), ver_field.winfo_rooty()
            vw, vh = ver_field.winfo_width(), ver_field.winfo_height()
            mx, my = event.x_root, event.y_root
            
            in_w = (wx <= mx <= wx + ww) and (wy <= my <= wy + wh)
            in_v = (vx <= mx <= vx + vw) and (vy <= my <= vy + vh)
            
            if not in_w and not in_v:
                close_picker()
        except Exception:
            pass

    root.bind_all("<Button-1>", global_click, add="+")

    ver_field = ctk.CTkFrame(vbox, fg_color=T.CARD_2, bg_color=T.CARD, corner_radius=12,
                              width=220, height=52)
    ver_field.pack(anchor="w")
    ver_field.pack_propagate(False)
    ver_lbl = ctk.CTkLabel(ver_field, textvariable=mc_var, text_color=T.FG,
                            font=font(16), anchor="w")
    ver_lbl.pack(side="left", fill="x", expand=True, padx=(14, 0))
    ver_arrow = ctk.CTkLabel(ver_field, text="▾", text_color=T.SUB, font=font(16, "bold"))
    ver_arrow.pack(side="right", padx=(0, 12))
    Tooltip(ver_field, "Change Version")

    def _ver_hover(on):
        _pick["hover"] = on
        if on or _pick["win"] is not None:
            ver_field.configure(fg_color=T.CARD_3)
            ver_lbl.configure(text_color=T.THEME_ACCENT)
        else:
            ver_field.configure(fg_color=T.CARD_2)
            ver_lbl.configure(text_color=T.FG)
            
    for _w in (ver_field, ver_lbl, ver_arrow):
        _w.bind("<Enter>", lambda e: _ver_hover(True))
        _w.bind("<Leave>", lambda e: _ver_hover(False))
        _w.bind("<Button-1>", lambda e: open_picker())

    rbox = ctk.CTkFrame(bar, fg_color="transparent")
    rbox.pack(side="right")
    
    hbox = ctk.CTkFrame(rbox, fg_color="transparent")
    hbox.pack(fill="x")

    play_btn = mkbtn(hbox, "  PLAY", lambda: do_play(), kind="play",
                      width=110, height=52, corner_radius=12,
                      font=font(16, "bold"), text_color=T.FG)
    play_btn.configure(anchor="center")
    play_btn._is_play_btn = True
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        play_btn._img_norm = _create_play_icon(16, T.FG, T.THEME_ACCENT)
        play_btn.configure(image=play_btn._img_norm, fg_color=T.THEME_ACCENT, hover_color=T.THEME_ACCENT)
    play_btn.pack(side="right")
    play_btn._tooltip = Tooltip(play_btn, "Play Game")

    settings_btn = mkbtn(hbox, "⛭", lambda: toggle_settings(), kind="ghost",
                          width=52, height=52, corner_radius=12, font=font(36))
    settings_btn.pack(side="right", padx=(0, 8))
    Tooltip(settings_btn, "Settings")

    det_btn = mkbtn(hbox, "Details", lambda: toggle_details(), kind="ghost",
                     width=70, height=52, corner_radius=12, font=font(16))
    det_btn.pack(side="right", padx=(0, 8))
    Tooltip(det_btn, "Show Activity Logs")

    # ==================================================================
    # Details / log panel
    # ==================================================================
    detwrap = ctk.CTkFrame(dock, fg_color=T.CARD_2, corner_radius=12, height=220)
    detwrap.pack_propagate(False)
    log_head = ctk.CTkFrame(detwrap, fg_color="transparent")
    log_head.pack(fill="x", padx=10, pady=(8, 0))
    ctk.CTkLabel(log_head, text="ACTIVITY LOG", text_color=T.MUTED,
                 font=font(10, "bold")).pack(side="left")

    logbox = tk.Text(detwrap, height=10, bg=T.r(T.CONSOLE_BG), fg=T.r(T.CONSOLE_FG), bd=0,
                      font=(MONO, 10), highlightthickness=0,
                      padx=12, pady=10, insertbackground=T.r(T.FG), wrap="word")
    logbox._is_logbox = True

    def clear_log():
        logbox.delete("1.0", "end")

    def copy_log():
        root.clipboard_clear()
        root.clipboard_append(logbox.get("1.0", "end-1c"))
        copy_log_btn.configure(text="Copied ✓")
        root.after(1200, lambda: copy_log_btn.configure(text="Copy"))

    copy_log_btn = mkbtn(log_head, "Copy", copy_log, kind="flat",
                          width=56, height=24, font=font(11))
    copy_log_btn.pack(side="right")
    mkbtn(log_head, "Clear", clear_log, kind="flat",
          width=52, height=24, font=font(11)).pack(side="right", padx=(0, 4))

    logbox.pack(fill="both", expand=True, padx=10, pady=(6, 10))
    for _tg, (_lbl, _a1, _a2, _lc, _mc) in _LEVELS.items():
        _nm = _lbl.strip()
        logbox.tag_configure("L_" + _nm, foreground=_lc,
                              font=(MONO, 10, "bold"))
        logbox.tag_configure("M_" + _nm, foreground=_mc)

    def toggle_details():
        ui["details"] = not ui["details"]
        if ui["details"]:
            h = int(detwrap.cget("height"))
            root.minsize(860, 640 + h)
            for _w in (status, status_lbl, prog):
                try: _w.configure(cursor="sb_v_double_arrow")
                except: pass
            try: prog._canvas.configure(cursor="sb_v_double_arrow")
            except: pass
            detwrap.pack(fill="both", padx=14, pady=(0, 14))
            det_btn.configure(text_color=T.FG, fg_color=T.CARD_3)
        else:
            root.minsize(860, 640)
            for _w in (status, status_lbl, prog):
                try: _w.configure(cursor="")
                except: pass
            try: prog._canvas.configure(cursor="")
            except: pass
            detwrap.pack_forget()
            det_btn.configure(text_color=T.FG, fg_color=T.CARD_2)

    # ==================================================================
    # Status / progress helpers
    # ==================================================================
    def set_status(t, color=T.SUB):
        root.after(0, lambda: (status_txt.set(t),
                                status_lbl.configure(text_color=color)))

    def _show_bar():
        if not prog.winfo_ismapped():
            prog.pack(fill="x", pady=(8, 2))

    def bar_busy():
        def ap():
            _show_bar()
            prog.configure(mode="indeterminate")
            prog.start()
        root.after(0, ap)

    def set_progress(g, t):
        def ap():
            _show_bar()
            prog.stop()
            prog.configure(mode="determinate")
            prog.set(g / max(1, t))
            status_txt.set(f"Downloading Minecraft…  {int(100 * g / max(1, t))}%")
            status_lbl.configure(text_color=T.FG)
        root.after(0, ap)

    def end_progress():
        def ap():
            prog.stop()
            prog.pack_forget()
        root.after(0, ap)

    def _friendly(line):
        m = line
        for tag in ("::", "OK", "!!", "xx"):
            if m.startswith(tag):
                m = m[len(tag):].strip()
                break
        low = m.lower()
        if "downloading minecraft" in low:
            return None
        if ("building winegdk" in low or "cloning winegdk" in low
                or "updating winegdk" in low):
            return ("Setting up the game engine — first run, "
                    "this can take a while…")
        if "installing minecraft" in low or "reinstalling minecraft" in low:
            return "Installing Minecraft…"
        if "preparing gdk-proton" in low or "extracting" in low:
            return "Preparing the engine…"
        if "pre-auth" in low or "signing in" in low:
            return "Signing in to Xbox Live…"
        if "minecraft is running" in low:
            return ("Minecraft is running — close the game to come back here.",
                    True)
        if "starting minecraft" in low or "launching minecraft" in low:
            return "Starting Minecraft…"
        if "game closed" in low:
            return ("Minecraft closed.", True)
        return None

    def glog(line):
        lvl = _LEVELS.get(line[:2])
        if lvl:
            nm = lvl[0].strip()
            logbox.insert("end", lvl[0] + "  ", "L_" + nm)
            logbox.insert("end", line[2:].strip() + "\n", "M_" + nm)
        else:
            logbox.insert("end", line + "\n")
        logbox.see("end")
        if not ui["busy"]:
            return
        if line.startswith("xx"):
            set_status(line[2:].strip(), T.RED)
            return
        txt = _friendly(line)
        if txt:
            steady = False
            if isinstance(txt, tuple):
                txt, steady = txt
            if steady:
                set_status(txt, T.GREEN if "running" in txt.lower() else T.SUB)
                end_progress()
            else:
                set_status(txt, T.FG)
                bar_busy()
    log._LOG_SINK = lambda m: root.after(0, glog, m)

    # ==================================================================
    # Account
    # ==================================================================
    def acct_state(ph):
        gt = msa_gamertag() or "Xbox Live"
        if ph == "in":
            acct_dot.configure(text_color=T.GREEN)
            acct_txt_lbl.configure(cursor="", text_color=T.FG)
            acct_txt.set("Signed in")
            acct_txt_lbl._tooltip.text = f"Signed in as {gt}"
            acct_btn.configure(text="Sign out", fg_color=T.CARD_2, hover_color=T.CARD_3, text_color=T.FG)
            acct_btn._tooltip.text = f"Sign out of {gt}"
            acct_btn._mode = "out"
            acct_btn._confirm_out = False
            acct_btn._confirm_cancel = False
        elif ph == "auth":
            acct_txt_lbl.configure(cursor="", text_color=T.FG)
            acct_dot.configure(text_color=T.GOLD)
            acct_txt.set("Sign-in pending…")
            acct_txt_lbl._tooltip.text = "Sign-in pending"
            acct_btn.configure(text="Cancel", fg_color=T.CARD_2, hover_color=T.CARD_3, text_color=T.FG)
            acct_btn._tooltip.text = "Cancel sign-in"
            acct_btn._mode = "cancel"
            acct_btn._confirm_out = False
            acct_btn._confirm_cancel = False
        else:
            acct_txt_lbl.configure(cursor="", text_color=T.FG)
            acct_dot.configure(text_color=T.SUB)
            acct_txt.set("Not signed in")
            acct_txt_lbl._tooltip.text = "Not signed in"
            acct_btn.configure(text="Sign in", fg_color=T.CARD_2, hover_color=T.CARD_3, text_color=T.FG)
            acct_btn._tooltip.text = "Sign in to Microsoft"
            acct_btn._mode = "in"
            acct_btn._confirm_out = False
            acct_btn._confirm_cancel = False

    def acct_click():
        mode = getattr(acct_btn, "_mode", "in")
        if mode == "auth_loading":
            return
        if mode == "out":
            if getattr(acct_btn, "_confirm_out", False):
                na.stop()
                try:
                    with prefix_operation_lock("sign out of Microsoft"):
                        msa_logout()
                except BolError as exc:
                    warn(str(exc))
                    acct_state("in" if msa_signed_in() else "out")
                    return
                acct_state("out")
            else:
                acct_btn._confirm_out = True
                gt = msa_gamertag() or "Xbox Live"
                acct_btn.configure(text="Sign out?", fg_color=T.RED, hover_color=T.RED_HOV, text_color="white")
                acct_btn._tooltip.text = f"Sign out of {gt}?"
        elif mode == "cancel":
            if getattr(acct_btn, "_confirm_cancel", False):
                na.stop()
                from .auth import msa_signed_in
                acct_state("in" if msa_signed_in() else "out")
                if ui.get("auth_dialog") and ui["auth_dialog"].winfo_exists():
                    ui["auth_dialog"].destroy()
            else:
                acct_btn._confirm_cancel = True
                acct_btn.configure(text="Cancel?", fg_color=T.RED, hover_color=T.RED_HOV, text_color="white")
        else:
            acct_btn._mode = "auth_loading"
            acct_btn.configure(text="Loading…")
            threading.Thread(target=lambda: na.start(on_auth, on_online),
                              daemon=True).start()

    def _cancel_signout(e):
        cp = str(e.widget)
        bp = str(acct_btn)
        if getattr(acct_btn, "_confirm_out", False):
            if cp != bp and not cp.startswith(bp + "."):
                acct_btn._confirm_out = False
                acct_btn.configure(text="Sign out", fg_color=T.CARD_2, hover_color=T.CARD_3, text_color=T.FG)
                gt = msa_gamertag() or "Xbox Live"
                acct_btn._tooltip.text = f"Sign out of {gt}"
        if getattr(acct_btn, "_confirm_cancel", False):
            if cp != bp and not cp.startswith(bp + "."):
                acct_btn._confirm_cancel = False
                acct_btn.configure(text="Cancel", fg_color=T.CARD_2, hover_color=T.CARD_3, text_color=T.FG)
    root.bind_all("<Button-1>", _cancel_signout, add="+")

    def on_auth(url, code):
        root.after(0, lambda: (acct_state("auth"), code_dialog(url, code)))

    def on_online():
        root.after(0, lambda: acct_state("in"))
        if ui.get("auth_dialog") and ui["auth_dialog"].winfo_exists():
            root.after(0, ui["auth_dialog"].destroy)
        def _bg_fetch():
            from .auth import msa_load, msa_refresh, xbl_preauth, _account_cache_epoch
            from .config import DATA
            try:
                tok = msa_load()
                if not tok: return
                fresh = msa_refresh(tok.get("refresh_token"))
                if fresh and fresh.get("access_token"):
                    ep = _account_cache_epoch(DATA / "winegdk-preauth")
                    if xbl_preauth(fresh.get("access_token"), ep):
                        root.after(0, lambda: acct_state("in"))
            except Exception:
                pass
        threading.Thread(target=_bg_fetch, daemon=True).start()

    def code_dialog(url, code):
        url = f"https://login.live.com/oauth20_remoteconnect.srf?otc={code}"
        d = dialog("Sign in to Microsoft", 380, 370)
        ui["auth_dialog"] = d
        def on_close():
            na.stop()
            from .auth import msa_signed_in
            acct_state("in" if msa_signed_in() else "out")
            d.destroy()
        d.protocol("WM_DELETE_WINDOW", on_close)
        d.bind("<Escape>", lambda e: on_close())
        row = ctk.CTkFrame(d, fg_color="transparent")
        row.pack(anchor="center", pady=(24, 8))
        mkbtn(row, "Sign In to your Microsoft account", lambda: subprocess.Popen(
            ["xdg-open", url], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL), kind="primary",
            font=font(16, "bold"), height=42, width=280, text_color=T.FG).pack(side="left")

        ctk.CTkLabel(d, text="Scan this QR or open the link and enter this code:",
                     text_color=T.SUB).pack(anchor="center", pady=(8, 12))

        qr_frame = ctk.CTkFrame(d, fg_color="transparent", width=150, height=150)
        qr_frame.pack_propagate(False)
        qr_frame.pack(anchor="center", pady=(0, 16))
        
        bg_color = d.cget("fg_color")
        if isinstance(bg_color, tuple):
            bg_color = d._apply_appearance_mode(bg_color)
        qr_lbl = tk.Label(qr_frame, bg=bg_color, bd=0)
        qr_lbl.place(relx=0.5, rely=0.5, anchor="center")

        if icon_img is not None:
            factors = [10, 9, 8, 7, 6, 7, 8, 9]
            step_idx = 0
            def animate_qr():
                nonlocal step_idx
                if getattr(qr_lbl, "_is_loaded", False) or not qr_lbl.winfo_exists():
                    return
                try:
                    im = icon_img.subsample(factors[step_idx])
                    qr_lbl.configure(image=im)
                    qr_lbl.image = im
                except Exception: pass
                step_idx = (step_idx + 1) % len(factors)
                qr_lbl.after(125, animate_qr)
            animate_qr()

        def _load_qr():
            from .qrcodegen import QrCode
            from PIL import Image
            try:
                qr = QrCode.encode_text(url, QrCode.Ecc.LOW)
                border = 2
                size = qr.get_size() + border * 2
                img = Image.new("1", (size, size), 1)
                pixels = img.load()
                for y in range(qr.get_size()):
                    for x in range(qr.get_size()):
                        if qr.get_module(x, y):
                            pixels[x + border, y + border] = 0
                img = img.resize((150, 150), Image.Resampling.NEAREST).convert("RGB")
                ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(150, 150))
                def _apply_qr():
                    if not qr_frame.winfo_exists():
                        return
                    qr_lbl._is_loaded = True
                    qr_lbl.destroy()
                    ctk.CTkLabel(qr_frame, text="", image=ctk_img).pack(expand=True, fill="both")
                    qr_frame._img = ctk_img
                root.after(200, _apply_qr)
            except Exception:
                root.after(0, lambda: qr_lbl.winfo_exists() and (setattr(qr_lbl, "_is_loaded", True), qr_lbl.configure(image="", text="(Unavailable)", fg="gray")))
        threading.Thread(target=_load_qr, daemon=True).start()

        code_row = ctk.CTkFrame(d, fg_color="transparent")
        code_row.pack(anchor="center", pady=(8, 24))
        
        c_ent = ctk.CTkEntry(code_row, width=175, fg_color="transparent", border_width=0,
                             text_color=T.BLUE, justify="center",
                             font=ctk.CTkFont(family=MONO, size=32, weight="bold"))
        c_ent.insert(0, code)
        c_ent.configure(state="readonly")
        c_ent.pack(side="left")

        copy_btn = None

        def copy_code():
            root.clipboard_clear()
            root.clipboard_append(code)
            copy_btn.configure(text="✓")
            root.after(1200, lambda: copy_btn.winfo_exists() and copy_btn.configure(text="🗎"))

        copy_btn = mkbtn(code_row, "🗎", copy_code, kind="ghost", width=32,
                          height=32, font=font(16), corner_radius=8)
        copy_btn.pack(side="left", padx=(2, 0))

    # ==================================================================
    # Versions
    # ==================================================================
    def refresh_versions():
        beta = load_settings().get("show_betas", False)
        try:
            ui["versions"] = list_mc_versions(beta)
        except Exception as e:
            log._LOG_SINK(f"xx versions: {e}")
            return
        from .util import format_display_version
        labels = [format_display_version(v["tag"], v["beta"]) + ("  ·  BETA" if v["beta"] else "")
                  for v in ui["versions"]]

        def ap():
            ui["labels"] = labels
            cur = (load_settings().get("mc_version") or "")
            pick = next((x for x in labels
                         if x.split("  ")[0] == cur
                         or x.split("  ")[0].startswith(cur + ".")),
                        labels[0] if labels else "")
            if labels:
                mc_var.set(pick)
                _update_selected_chip()
        root.after(0, ap)

    def selected_version():
        if not ui["versions"] or not mc_var.get():
            return None
        from .util import format_display_version
        labels = [format_display_version(v["tag"], v["beta"]) + ("  ·  BETA" if v["beta"] else "")
                  for v in ui["versions"]]
        try:
            return ui["versions"][labels.index(mc_var.get())]
        except ValueError:
            return None

    # ==================================================================
    # Play & Kill
    # ==================================================================
    def busy(on):
        ui["busy"] = on
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if on:
                play_btn._img_norm = _create_kill_icon(16, T.FG, T.RED)
                play_btn.configure(
                    text="  KILL",
                    image=play_btn._img_norm,
                    fg_color=T.RED,
                    hover_color=T.RED,
                    text_color=T.FG,
                    command=lambda: kill_wine()
                )
                if hasattr(play_btn, "_tooltip"):
                    cur_ver = mc_var.get().split('  ')[0].strip() if mc_var.get() else "Game"
                    play_btn._tooltip.text = f"Kill {cur_ver}"
            else:
                play_btn._img_norm = _create_play_icon(16, T.FG, T.THEME_ACCENT)
                play_btn.configure(
                    text="  PLAY",
                    image=play_btn._img_norm,
                    fg_color=T.THEME_ACCENT,
                    hover_color=T.THEME_ACCENT,
                    text_color=T.FG,
                    command=lambda: do_play()
                )
                if hasattr(play_btn, "_tooltip"):
                    cur_ver = mc_var.get().split('  ')[0].strip() if mc_var.get() else "Game"
                    play_btn._tooltip.text = f"Play {cur_ver}"

    def do_play():
        if ui["busy"]:
            return
        busy(True)
        set_status("Preparing…", T.FG)
        bar_busy()

        def work():
            try:
                ver = selected_version()
                do_setup(mc_ver=ver, progress=set_progress)
                set_status("Starting Minecraft…", T.FG)
                ui["launch_active"] = True
                launch()
                set_status("Minecraft closed.", T.SUB)
            except Exception as e:
                message = str(e) or type(e).__name__
                log._LOG_SINK(f"xx {message}")
                set_status("Minecraft could not start.", T.RED)
                root.after(0, lambda text=message: messagebox.showerror(
                    "Minecraft could not start", text[:2000], parent=root))
            finally:
                ui["launch_active"] = False
                end_progress()
                root.after(0, lambda: busy(False))
        threading.Thread(target=work, daemon=False).start()

    # ==================================================================
    # Settings (tabbed) — built in, lives in view_area next to the hero
    # ==================================================================
    settings_view = ctk.CTkFrame(view_area, fg_color=T.CARD, corner_radius=18,
                                  border_width=1, border_color=T.BORDER)

    def toggle_settings():
        if settings_view.winfo_ismapped():
            settings_view.pack_forget()
            hero.pack(fill="both", expand=True)
            settings_btn.configure(fg_color=T.CARD_2)
        else:
            hero.pack_forget()
            changelog_view.pack_forget()
            settings_view.pack(fill="both", expand=True)
            settings_btn.configure(fg_color=T.CARD_3)

    def toggle_changelog():
        if changelog_view.winfo_ismapped():
            changelog_view.pack_forget()
            hero.pack(fill="both", expand=True)
        else:
            hero.pack_forget()
            settings_view.pack_forget()
            changelog_view.pack(fill="both", expand=True)
            settings_btn.configure(fg_color=T.CARD_2)
            load_changelogs()

    def _build_settings():
        d = root
        outer = ctk.CTkFrame(settings_view, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=20, pady=18)

        ui["settings_head"] = ctk.CTkFrame(outer, fg_color="transparent")
        ui["settings_head"].pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(ui["settings_head"], text="Settings", font=font(16, "bold"),
                     text_color=T.FG).pack(side="left")
        mkbtn(ui["settings_head"], "← Back", toggle_settings, kind="flat", width=76,
              height=28, font=font(12)).pack(side="right")

        tabs = ctk.CTkTabview(
            outer, fg_color=T.CARD_2, segmented_button_fg_color=T.CARD_2,
            segmented_button_selected_color=T.THEME_ACCENT,
            segmented_button_selected_hover_color=T.THEME_HOV,
            segmented_button_unselected_color=T.CARD_2,
            text_color=T.FG, corner_radius=12)
        tabs.pack(fill="both", expand=True)
        
        def _mk_sf(parent):
            sf = ctk.CTkScrollableFrame(parent, fg_color=T.CARD_2)
            def _check(*_):
                try:
                    c = sf._parent_canvas
                    if sf.winfo_reqheight() > c.winfo_height() and c.winfo_height() > 10:
                        sf._scrollbar.grid()
                    else:
                        sf._scrollbar.grid_remove()
                except Exception: pass
            sf.bind("<Configure>", _check, add="+")
            try: sf._parent_canvas.bind("<Configure>", _check, add="+")
            except Exception: pass
            sf.pack(fill="both", expand=True)
            return sf

        tab_general = _mk_sf(tabs.add("General"))
        tab_advanced = _mk_sf(tabs.add("Advanced"))
        tab_tools = _mk_sf(tabs.add("Tools"))

        # ---- General --------------------------------------------------
        theme_v = tk.BooleanVar(value=load_settings().get("light_theme", False))
        
        def save_theme():
            s2 = load_settings()
            s2["light_theme"] = theme_v.get()
            save_settings(s2)
            ctk.set_appearance_mode("Light" if theme_v.get() else "Dark")
            _sync_theme(root)
            
        ctk.CTkSwitch(tab_general, text="Use light theme",
                      variable=theme_v, command=save_theme,
                      progress_color=T.THEME_ACCENT, font=font(13)
                      ).pack(anchor="w", pady=8, padx=4)

        changelog_startup_v = tk.BooleanVar(value=load_settings().get("show_changelog_on_startup", False))
        
        def save_changelog_startup():
            s2 = load_settings()
            s2["show_changelog_on_startup"] = changelog_startup_v.get()
            save_settings(s2)
            
        ctk.CTkSwitch(tab_general, text="Show changelog on startup",
                      variable=changelog_startup_v, command=save_changelog_startup,
                      progress_color=T.THEME_ACCENT, font=font(13)
                      ).pack(anchor="w", pady=8, padx=4)

        beta_v = tk.BooleanVar(value=load_settings().get("show_betas", False))

        def save_beta():
            s2 = load_settings()
            s2["show_betas"] = beta_v.get()
            save_settings(s2)
            threading.Thread(target=refresh_versions, daemon=True).start()
            load_changelogs(force=True)
        ctk.CTkSwitch(tab_general, text="Show beta / preview versions",
                      variable=beta_v, command=save_beta,
                      progress_color=T.THEME_ACCENT, font=font(13)
                      ).pack(anchor="w", pady=8, padx=4)

        confine_v = tk.BooleanVar(
            value=load_settings().get("confine_cursor", False))

        def save_confine():
            s2 = load_settings()
            s2["confine_cursor"] = confine_v.get()
            save_settings(s2)
        ctk.CTkSwitch(tab_general, text="Keep the mouse inside the window\n"
                      "(fixes the cursor escaping in windowed mode)",
                      variable=confine_v, command=save_confine,
                      progress_color=T.THEME_ACCENT, font=font(13)
                      ).pack(anchor="w", pady=8, padx=4)

        # ---- Advanced ---------------------------------------------------
        diag_v = tk.BooleanVar(value=load_settings().get("diagnostics", False))

        def save_diag():
            s2 = load_settings()
            s2["diagnostics"] = diag_v.get()
            save_settings(s2)
        ctk.CTkSwitch(tab_advanced, text="Advanced diagnostics\n"
                      "(verbose logs — for bug reports)",
                      variable=diag_v, command=save_diag,
                      progress_color=T.THEME_ACCENT, font=font(13)
                      ).pack(anchor="w", pady=(4, 12), padx=4)

        ctk.CTkLabel(tab_advanced, text="Custom environment variables",
                     text_color=T.SUB, font=font(11, "bold"),
                     anchor="w").pack(anchor="w", pady=(0, 4), padx=4)

        def save_custom_env(_event=None):
            s2 = load_settings()
            s2["custom_env"] = env_entry.get()
            save_settings(s2)

        env_entry = ctk.CTkEntry(
            tab_advanced,
            placeholder_text="e.g., PROTON_USE_WINED3D=1 KEY=VALUE",
            fg_color=T.CARD_3, border_width=0, text_color=T.FG,
            placeholder_text_color=T.MUTED, corner_radius=10, height=36,
            font=font(13))
        env_entry.pack(fill="x", pady=(0, 12), padx=4)
        saved_env = load_settings().get("custom_env") or ""
        if saved_env:
            env_entry.insert(0, saved_env)
        env_entry.bind("<KeyRelease>", save_custom_env)
        env_entry.bind("<FocusOut>", save_custom_env)
        env_entry.bind("<Return>", lambda e: "break")
        
        ctk.CTkLabel(tab_advanced, text="Gamescope arguments",
                     text_color=T.SUB, font=font(11, "bold"),
                     anchor="w").pack(anchor="w", pady=(0, 4), padx=4)

        def save_gamescope(_event=None):
            s2 = load_settings()
            s2["gamescope"] = gamescope_entry.get()
            save_settings(s2)

        gamescope_entry = ctk.CTkEntry(
            tab_advanced,
            placeholder_text="1 for auto, or e.g. -w 1920 -h 1080 -f",
            fg_color=T.CARD_3, border_width=0, text_color=T.FG,
            placeholder_text_color=T.MUTED, corner_radius=10, height=36,
            font=font(13))
        gamescope_entry.pack(fill="x", pady=(0, 4), padx=4)
        saved_gamescope = load_settings().get("gamescope") or ""
        if saved_gamescope:
            gamescope_entry.insert(0, saved_gamescope)
        gamescope_entry.bind("<KeyRelease>", save_gamescope)
        gamescope_entry.bind("<FocusOut>", save_gamescope)
        gamescope_entry.bind("<Return>", lambda e: "break")

        # ===== Game files location =====
        ctk.CTkLabel(tab_advanced, text="Game files location",
                     text_color=T.SUB, font=font(11, "bold"),
                     anchor="w").pack(anchor="w", pady=(12, 4), padx=4)

        loc_row = ctk.CTkFrame(tab_advanced, fg_color="transparent")
        loc_row.pack(fill="x", pady=(0, 2), padx=4)

        loc_var = tk.StringVar(value=get_install_location())
        loc_field = ctk.CTkLabel(
            loc_row, textvariable=loc_var, text_color=T.FG, font=font(12),
            fg_color=T.CARD_3, corner_radius=8, anchor="w", height=36)
        loc_field.pack(side="left", fill="x", expand=True, padx=(0, 8))

        loc_status = tk.StringVar(value="")
        loc_move_v = tk.BooleanVar(value=True)

        def _relocate_blocked():
            if ui.get("launch_active"):
                messagebox.showwarning(
                    "Minecraft is running",
                    "Close Minecraft first before changing the game files "
                    "location.", parent=d)
                return True
            if ui.get("busy"):
                messagebox.showwarning(
                    "Operation in progress",
                    "Wait for the current preparation task to finish before "
                    "changing the game files location.", parent=d)
                return True
            if _mc_running():
                messagebox.showwarning(
                    "Minecraft is running",
                    "Close Minecraft first before changing the game files "
                    "location.", parent=d)
                return True
            return False

        def do_browse():
            if _relocate_blocked():
                return
            from tkinter import filedialog, messagebox as mb
            chosen = filedialog.askdirectory(
                parent=d, title="Choose a folder for BedrockOnLinux's files",
                mustexist=False)
            if not chosen:
                return
            old_dir = Path(get_install_location())
            new_dir = Path(chosen).expanduser()
            if new_dir == old_dir:
                return
            if new_dir.exists() and any(new_dir.iterdir()):
                mb.showerror(
                    "Folder not empty",
                    f"'{new_dir}' already has files in it. Choose an empty "
                    "or new folder.", parent=d)
                return

            move_existing = mb.askyesnocancel(
                "Move existing files?",
                f"Move everything currently in\n{old_dir}\nto\n{new_dir}\n\n"
                "Yes: move the existing install (engine, downloaded "
                "versions, saves, settings) to the new location.\n"
                "No: leave the old files where they are and start fresh at "
                "the new location (re-downloads the engine and game).\n"
                "Cancel: don't change anything.",
                parent=d)
            if move_existing is None:
                return

            loc_status.set("Moving…" if move_existing else "Applying…")

            def work():
                try:
                    if move_existing:
                        new_dir.parent.mkdir(parents=True, exist_ok=True)
                        if new_dir.exists():
                            new_dir.rmdir()  # only succeeds if empty
                        shutil.move(str(old_dir), str(new_dir))
                    set_install_location(new_dir)
                    msg = ("Location updated.\n\nRestart BedrockOnLinux for "
                           "this to take effect.")
                    ok_flag = True
                except Exception as e:
                    msg = f"Couldn't change the location:\n{e}"
                    ok_flag = False

                def finish():
                    loc_status.set("")
                    if ok_flag:
                        loc_var.set(str(new_dir))
                        if mb.askyesno("Restart now?",
                                       msg + "\n\nRestart now?", parent=d):
                            relaunch_app()
                    else:
                        mb.showerror("Game files location", msg, parent=d)
                d.after(0, finish)
            threading.Thread(target=work, daemon=True).start()

        def do_reset():
            if _relocate_blocked():
                return
            from tkinter import messagebox as mb
            if get_install_location() == default_install_location():
                return
            if not mb.askyesno(
                    "Reset location",
                    "Reset to the default location "
                    f"({default_install_location()})?\n\nThis only clears "
                    "the saved preference — it does not move or delete any "
                    "files. Restart required.", parent=d):
                return
            clear_install_location()
            loc_var.set(default_install_location())
            if mb.askyesno("Restart now?", "Restart BedrockOnLinux now?",
                           parent=d):
                relaunch_app()

        loc_btns = ctk.CTkFrame(loc_row, fg_color="transparent")
        loc_btns.pack(side="right")
        mkbtn(loc_btns, "Browse…", do_browse, kind="ghost", width=84,
              height=36, font=font(12)).pack(side="left", padx=(0, 4))
        mkbtn(loc_btns, "Reset", do_reset, kind="flat", width=64,
              height=36, font=font(12)).pack(side="left")

        ctk.CTkLabel(tab_advanced, textvariable=loc_status,
                     text_color=T.GOLD, font=font(11)
                     ).pack(anchor="w", pady=(4, 0), padx=4)
        ctk.CTkLabel(tab_advanced,
                     text="Moves where the engine, downloaded Minecraft "
                          "versions, saves and settings are stored — handy "
                          "if your home drive is low on space. Changing this "
                          "requires a restart.",
                     text_color=T.MUTED, font=font(10), anchor="w",
                     justify="left", wraplength=340
                     ).pack(anchor="w", pady=(2, 8), padx=4)
        
        # ---- Tools --------------------------------------------------
        imp_status = tk.StringVar(value="")

        def do_import():
            from tkinter import filedialog, messagebox as mb
            files = filedialog.askopenfilenames(
                parent=d, title="Import Minecraft content",
                filetypes=[("Minecraft content",
                            "*.mcpack *.mcaddon *.mcworld *.mctemplate *.mcskin"),
                           ("All files", "*.*")])
            if not files:
                return
            imp_status.set("Importing…")

            def work():
                done, errs = [], []
                for f in files:
                    try:
                        done += import_content(f)
                    except BolError as e:
                        errs.append(str(e))
                    except Exception as e:
                        errs.append(f"{Path(f).name}: {e}")
                msg = (f"Imported {len(done)} item(s)."
                       if done else "Nothing imported.")
                if errs:
                    msg += "\n\nProblems:\n• " + "\n• ".join(errs)
                if _mc_running():
                    msg += ("\n\nMinecraft is running — restart it to see the "
                            "new content.")
                d.after(0, lambda: (imp_status.set(""),
                                    mb.showinfo("Import", msg, parent=d)))
            threading.Thread(target=work, daemon=True).start()

        def do_inject():
            from tkinter import filedialog, messagebox as mb
            if not _mc_running():
                mb.showwarning(
                    "DLL injector",
                    "Start Minecraft first and wait for the main menu, then "
                    "inject.", parent=d)
                return
            last = load_settings().get("injector_dll") or ""
            dll = filedialog.askopenfilename(
                parent=d, title="Choose a client .dll to inject",
                initialdir=(str(Path(last).parent) if last else None),
                initialfile=(Path(last).name if last else None),
                filetypes=[("Client DLL", "*.dll"), ("All files", "*.*")])
            if not dll:
                return
            imp_status.set("Injecting…")

            def work():
                try:
                    name = run_injector(dll)
                    s2 = load_settings()
                    s2["injector_dll"] = dll
                    save_settings(s2)
                    msg = (f"Injected {name} into Minecraft. ✓\n\n"
                           "(Native / AppImage only — not inside the Flatpak "
                           "sandbox.)")
                except Exception as e:
                    msg = f"Couldn't inject:\n{e}"
                d.after(0, lambda: (imp_status.set(""),
                                    mb.showinfo("DLL injector", msg,
                                                parent=d)))
            threading.Thread(target=work, daemon=True).start()

        for label, fn, kind in (
            ("Import content (.mcpack / .mcworld / .mcaddon)…",
             do_import, "ghost"),
            ("Inject a client DLL…", do_inject, "ghost"),
            ("Open Minecraft folder", lambda: subprocess.Popen(
                ["xdg-open", str(_mojang_dir())], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL), "ghost"),
            ("Open logs folder", lambda: subprocess.Popen(
                ["xdg-open", str(LOGS)], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL), "ghost"),
            ("Repair (reset Wine prefix)", lambda: threading.Thread(
                target=reset_prefix, daemon=True).start(), "ghost"),
            ("Force stop Minecraft", kill_wine, "danger"),
        ):
            mkbtn(tab_tools, label, fn, kind=kind, anchor="w",
                  height=38).pack(fill="x", pady=3, padx=4)

        ctk.CTkLabel(tab_tools, textvariable=imp_status, text_color=T.GOLD,
                     font=font(11)).pack(anchor="w", pady=(6, 0), padx=4)

    _build_settings()

    # ==================================================================
    # Changelog
    # ==================================================================
    changelog_view = ctk.CTkFrame(view_area, fg_color=T.CARD, corner_radius=18,
                                   border_width=1, border_color=T.BORDER)

    def _insert_inline_formatted(widget, text, base_tag, extra_tags=None):
        tokens = RE_MD_TOKENS.split(text)
        is_bold = False
        is_code = False
        for i, token in enumerate(tokens):
            if not token:
                continue
            if token in ("**", "__"):
                is_bold = not is_bold
                continue
            elif token == "`":
                is_code = not is_code
                continue

            link_match = RE_MD_LINK.match(token)
            if link_match:
                link_text = link_match.group(1)
                url = link_match.group(2)
                tags = ["link", f"url:{url}", base_tag] + list(extra_tags or [])
                if is_bold:
                    tags.append("bold")
                if is_code:
                    tags.append("code")
                widget.insert("end", link_text, tuple(tags))
                next_tok = tokens[i + 1] if i + 1 < len(tokens) else ""
                if next_tok and not next_tok[0].isspace():
                    widget.insert("end", " ", (base_tag,))
            else:
                tags = [base_tag] + list(extra_tags or [])
                if is_bold:
                    tags.append("bold")
                if is_code:
                    tags.append("code")
                widget.insert("end", token, tuple(tags))

    def render_markdown_to_text(widget, md, wrap_width=75):
        lines = md.split("\n")
        in_code_block = False
        code_in_quote = False
        code_content = []

        for line in lines:
            stripped = line.strip()
            is_quote = stripped.startswith(">")

            if is_quote:
                content_line = stripped[1:].strip()
            else:
                content_line = line

            if content_line.startswith("```"):
                if in_code_block:
                    for cl in code_content:
                        widget.insert("end", cl + "\n", "code_block")
                    in_code_block = False
                    code_in_quote = False
                    code_content = []
                else:
                    in_code_block = True
                    code_in_quote = is_quote
                continue

            if in_code_block:
                code_content.append(content_line)
                continue

            # Headers
            if content_line.startswith("#"):
                hashes = len(content_line) - len(content_line.lstrip("#"))
                content = content_line.lstrip("#").strip()
                tag = f"h{min(hashes, 3)}"
                widget.insert("end", content + "\n", (tag, "quote") if is_quote else tag)
                continue

            # Blockquotes
            if is_quote:
                if content_line:
                    _insert_inline_formatted(widget, content_line + "\n", "quote")
                else:
                    widget.insert("end", "\n", "quote")
                continue

            if stripped.startswith(("* ", "- ", "+ ", "\u2022 ")):
                bullet_char = "\u2022 "
                content = line.replace(stripped[:2], "", 1).strip()
                widget.insert("end", bullet_char, "bullet")
                _insert_inline_formatted(widget, content + "\n", "bullet")
                continue

            # Normal lines
            if stripped == "":
                widget.insert("end", "\n", "normal")
            else:
                _insert_inline_formatted(widget, line + "\n", "normal")

    def render_launcher_changelog(widget, rels, dividers, wrap_width=75):
        for i, rel in enumerate(rels):
            tag_name = rel.get("tag_name", "Unknown")
            name = rel.get("name")
            date = (rel.get("published_at") or "").split("T")[0]
            body = (rel.get("body") or "").strip()

            title_text = tag_name
            if name and name != tag_name:
                title_text += f" \u2014 {name}"

            url = rel.get("html_url")
            tags = ("release_title", "link", f"url:{url}") if url else ("release_title",)
            widget.insert("end", title_text + "\n", tags)
            widget.insert("end", date + "\n", "release_date")

            if body:
                render_markdown_to_text(widget, body, wrap_width=wrap_width)

            if i < len(rels) - 1:
                div_frame = ctk.CTkFrame(widget, fg_color=T.MUTED, height=2, width=1, corner_radius=0)
                div_frame.pack_propagate(False)
                widget.insert("end", "\n", "divider_tag")
                widget.window_create("end", window=div_frame)
                widget.insert("end", "\n\n", "divider_tag")
                dividers.append(div_frame)

    def render_game_changelog(widget, data, dividers, wrap_width=75):
        from .util import format_display_version
        ui_sel = mc_var.get()
        ui_wants_beta = "BETA" in ui_sel if ui_sel else False
        target_version = ui_sel.split('  ')[0].strip() if ui_sel else None
        target_index = None
        
        filtered = []
        for art in data.get("articles", []):
            title = art.get("title", "Unknown Release")
            if not ("bedrock" in title.lower() or "beta" in title.lower() or "preview" in title.lower()):
                continue
            is_beta = "beta" in title.lower() or "preview" in title.lower()
            if is_beta == ui_wants_beta:
                filtered.append(art)
                
        articles = filtered[:40]
        for i, art in enumerate(articles):
            title = art.get("title", "Unknown Release")
            is_beta = "beta" in title.lower() or "preview" in title.lower()
            title = format_display_version(title, is_beta)
            date = (art.get("updated_at") or "").split("T")[0]
            body = art.get("body") or ""
            url = art.get("html_url")

            if target_version and target_version in title and target_index is None:
                target_index = widget.index("end-1c")

            tags = ("release_title", "link", f"url:{url}") if url else ("release_title",)
            widget.insert("end", title + "\n", tags)
            widget.insert("end", date + "\n", "release_date")

            if body:
                from html.parser import HTMLParser

                class HTMLToTkinterParser(HTMLParser):
                    def __init__(self, w):
                        super().__init__()
                        self.w = w
                        self.tags = []
                        self.current_href = None
                        self.in_blockquote = False

                    def _ensure_newlines(self, count):
                        if self.w.index("end-1c") == "1.0":
                            return
                        text = self.w.get(f"end-{count+1}c", "end-1c")
                        missing = count - text.count("\n")
                        if missing > 0:
                            self.w.insert("end", "\n" * missing)

                    def handle_starttag(self, tag, attrs):
                        attrs_dict = dict(attrs)
                        if tag in ("strong", "b"):
                            self.tags.append("bold")
                        elif tag in ("h1", "h2", "h3"):
                            self.tags.append(tag)
                            self._ensure_newlines(2)
                        elif tag == "a" and "href" in attrs_dict:
                            self.current_href = attrs_dict["href"]
                            self.tags.append("link")
                            self.tags.append(f"url:{self.current_href}")
                        elif tag == "li":
                            self._ensure_newlines(1)
                            self.w.insert("end", "\u2022 ", "bullet")
                            self.tags.append("bullet")
                            self.in_li = True
                            self.li_has_content = False
                        elif tag == "blockquote":
                            self.tags.append("quote")
                            self.in_blockquote = True
                            self._ensure_newlines(1)
                        elif tag == "p":
                            if not getattr(self, "in_li", False):
                                self._ensure_newlines(2)

                    def handle_endtag(self, tag):
                        if tag in ("strong", "b"):
                            if "bold" in self.tags:
                                self.tags.remove("bold")
                        elif tag in ("h1", "h2", "h3"):
                            if tag in self.tags:
                                self.tags.remove(tag)
                            self._ensure_newlines(1)
                        elif tag == "a":
                            if "link" in self.tags:
                                self.tags.remove("link")
                            if f"url:{self.current_href}" in self.tags:
                                self.tags.remove(f"url:{self.current_href}")
                            self.current_href = None
                        elif tag == "li":
                            if "bullet" in self.tags:
                                self.tags.remove("bullet")
                            self.in_li = False
                            self.li_has_content = False
                            self._ensure_newlines(1)
                        elif tag == "ul":
                            self._ensure_newlines(2)
                        elif tag == "blockquote":
                            if "quote" in self.tags:
                                self.tags.remove("quote")
                            self.in_blockquote = False
                            self._ensure_newlines(1)
                        elif tag == "p":
                            self._ensure_newlines(1)

                    def handle_data(self, d_text):
                        in_li = getattr(self, "in_li", False)
                        in_link = self.current_href is not None
                        
                        d_text = d_text.replace("\n", " ")
                        
                        if not d_text.strip():
                            if d_text and (in_li or in_link):
                                if in_li and not getattr(self, "li_has_content", False):
                                    return
                                self.w.insert("end", d_text,
                                              tuple(self.tags or ["normal"]))
                        else:
                            if in_li and not getattr(self,
                                                     "li_has_content", False):
                                d_text = d_text.lstrip()
                                self.li_has_content = True
                            self.w.insert("end", d_text,
                                          tuple(self.tags or ["normal"]))

                parser = HTMLToTkinterParser(widget)
                parser.feed(body)

            if i < len(articles) - 1:
                div_frame = ctk.CTkFrame(widget, fg_color=T.MUTED, height=2, width=1, corner_radius=0)
                div_frame.pack_propagate(False)
                widget.insert("end", "\n", "divider_tag")
                widget.window_create("end", window=div_frame)
                widget.insert("end", "\n\n", "divider_tag")
                dividers.append(div_frame)

        if target_index is not None:
            def _do_scroll():
                widget.yview(target_index)
            widget.after(300, _do_scroll)

    def load_tab_changelog(tab, fetch_func, render_func):
        if getattr(tab, "_is_loading", False):
            return
        tab._is_loading = True
        tab.unbind("<Configure>")
        for child in tab.winfo_children():
            child.destroy()

        bg_color = tab.cget("fg_color")
        if isinstance(bg_color, tuple):
            bg_color = tab._apply_appearance_mode(bg_color)
        elif bg_color == "transparent":
            bg_color = T.CARD_2

        lab = tk.Label(tab, bg=bg_color, bd=0)
        lab.place(relx=0.5, rely=0.5, anchor="center")

        if icon_img is not None:
            factors = [10, 9, 8, 7, 6, 7, 8, 9]
            step_idx = 0

            def animate():
                nonlocal step_idx
                if not lab.winfo_exists():
                    return
                try:
                    factor = factors[step_idx]
                    im = icon_img.subsample(factor)
                    lab.configure(image=im)
                    lab.image = im
                except Exception:
                    pass
                step_idx = (step_idx + 1) % len(factors)
                tab.after(125, animate)

            animate()

        def show_error(err_msg):
            for child in tab.winfo_children():
                child.destroy()
            err_frame = ctk.CTkFrame(tab, fg_color="transparent")
            err_frame.place(relx=0.5, rely=0.5, anchor="center")
            ctk.CTkLabel(err_frame, text="Could not load changelog.",
                         font=font(14, "bold"), text_color=T.RED).pack(pady=4)
            ctk.CTkLabel(err_frame, text=err_msg, font=font(11),
                         text_color=T.SUB).pack(pady=4)
            mkbtn(err_frame, "Retry",
                  lambda: load_tab_changelog(tab, fetch_func, render_func),
                  kind="ghost", width=100, height=32).pack(pady=8)

        def show_data(data):
            if not data:
                show_error("No releases found.")
                return

            tb = ctk.CTkTextbox(tab, fg_color=T.CARD_2, corner_radius=12,
                                text_color=T.FG, font=font(11), wrap="word")
            tb._x_scrollbar.grid = lambda *a, **k: None
            tb._x_scrollbar.grid_forget()
            
            def _tb_check(*_):
                try:
                    yv = tb._textbox.yview()
                    if yv[0] <= 0.0 and yv[1] >= 1.0:
                        tb._yscrollbar.grid_remove()
                    else:
                        tb._yscrollbar.grid()
                except Exception: pass
            
            tb._textbox.bind("<Configure>", _tb_check, add="+")
            def _poll_tb():
                if tb.winfo_exists():
                    _tb_check()
                    tb.after(500, _poll_tb)
            tb.after(500, _poll_tb)

            widget = tb._textbox
            widget.configure(wrap="word", spacing1=1, spacing2=2,
                             spacing3=1, padx=16, pady=16)

            dividers = []
            f_family = font().cget("family")
            widget.tag_configure("quote", font=(f_family, 11, "italic"),
                                 background=T.r(T.CARD_2), foreground=T.r(T.SUB),
                                 lmargin1=10, rmargin=10,
                                 spacing1=3, spacing3=3)
            widget.tag_configure("normal", font=(f_family, 11),
                                 spacing1=1, spacing3=1)
            widget.tag_configure("bold", font=(f_family, 11, "bold"))
            widget.tag_configure("h1", font=(f_family, 15, "bold"),
                                 spacing1=6, spacing3=2)
            widget.tag_configure("h2", font=(f_family, 13, "bold"),
                                 spacing1=5, spacing3=2)
            widget.tag_configure("h3", font=(f_family, 12, "bold"),
                                 spacing1=4, spacing3=2)
            widget.tag_configure("code", font=(MONO, 9),
                                 background=T.r(T.CARD_2),
                                 foreground=T.r(T.BROWN))
            widget.tag_configure("code_block", font=(MONO, 9),
                                 background=T.r(T.CARD_2), foreground=T.r(T.FG),
                                 lmargin1=10, rmargin=10,
                                 spacing1=3, spacing3=3)
            widget.tag_configure("bullet", font=(f_family, 11),
                                 lmargin1=14, lmargin2=24)
            widget.tag_configure("link", foreground=T.r(T.THEME_ACCENT))
            widget.tag_configure("link_hover", underline=True)

            def on_link_click(event):
                idx = widget.index(f"@{event.x},{event.y}")
                for tag in widget.tag_names(idx):
                    if tag.startswith("url:"):
                        url = tag[4:]
                        subprocess.Popen(
                            ["xdg-open", url],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
                        break

            def on_link_enter(event):
                widget.configure(cursor="hand2")
                idx = widget.index(f"@{event.x},{event.y}")
                for tag in widget.tag_names(idx):
                    if tag.startswith("url:"):
                        ranges = widget.tag_ranges(tag)
                        if ranges:
                            for i in range(0, len(ranges), 2):
                                widget.tag_add("link_hover", ranges[i], ranges[i+1])
                        break
                        
            def on_link_leave(event):
                widget.configure(cursor="arrow")
                widget.tag_remove("link_hover", "1.0", "end")

            widget.tag_bind("link", "<Enter>", on_link_enter)
            widget.tag_bind("link", "<Leave>", on_link_leave)
            widget.tag_bind("link", "<Button-1>", on_link_click)

            widget.tag_configure("release_title",
                                 font=(f_family, 15, "bold"),
                                 foreground=T.r(T.THEME_ACCENT),
                                 spacing1=8, spacing3=1)
            widget.tag_configure("release_date", font=(f_family, 11),
                                 foreground=T.r(T.SUB),
                                 spacing1=0, spacing3=4)
            widget.tag_configure("divider_tag", spacing1=4, spacing3=4)

            widget.rendering = False

            def do_render(container_width):
                if getattr(widget, "rendering", False):
                    return
                widget.rendering = True
                try:
                    tb_width = container_width
                    tb.configure(width=tb_width)
                    char_width = max(30, int((tb_width - 48) / 7.2))
                    w_width = tb_width - 48
                    dividers.clear()
                    widget.configure(state="normal")
                    widget.delete("1.0", "end")
                    render_func(widget, data, dividers,
                                wrap_width=char_width)
                    widget.configure(state="disabled")
                    for div in dividers:
                        try:
                            div.configure(width=w_width)
                        except Exception:
                            pass
                finally:
                    widget.rendering = False

            def on_resize(event):
                do_render(event.width)
            tab.bind("<Configure>", on_resize)

            for child in tab.winfo_children():
                if child != tb:
                    child.destroy()
            tb.pack(fill="both", expand=True, padx=4, pady=4)
            tab.update_idletasks()
            do_render(tab.winfo_width())

        def work():
            try:
                data = fetch_func()
                def _done():
                    tab._is_loading = False
                    show_data(data)
                tab.after(0, _done)
            except Exception as e:
                err_msg = str(e)
                if "403" in err_msg:
                    err_msg += ("\n(Rate limit reached. Try setting "
                                "GITHUB_TOKEN environment variable.)")
                def _err():
                    tab._is_loading = False
                    show_error(err_msg)
                tab.after(0, _err)

        threading.Thread(target=work, daemon=True).start()

    def load_changelogs(force=False):
        if ui["changelogs_loaded"] and not force:
            return
        ui["changelogs_loaded"] = True

        from .util import mc_releases, gh_releases
        from .config import SELF_REPO

        load_tab_changelog(tab_game, lambda: mc_releases(fetch_all=False), render_game_changelog)
        load_tab_changelog(tab_launcher,
                           lambda: gh_releases(SELF_REPO),
                           render_launcher_changelog)

    def _build_changelog_view():
        nonlocal tab_game, tab_launcher
        outer = ctk.CTkFrame(changelog_view, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=20, pady=18)

        ui["changelog_head"] = ctk.CTkFrame(outer, fg_color="transparent", height=28)
        ui["changelog_head"].pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(ui["changelog_head"], text="Changelog", font=font(16, "bold"),
                     text_color=T.FG).pack(side="left")
        mkbtn(ui["changelog_head"], "← Back", toggle_changelog, kind="flat", width=76,
              height=28, font=font(12)).pack(side="right")

        ui["last_tab"] = "Game"
        def on_tab_change():
            ui["last_tab"] = tabs.get()

        tabs = ctk.CTkTabview(
            outer, fg_color=T.CARD_2,
            segmented_button_fg_color=T.CARD_2,
            segmented_button_selected_color=T.THEME_ACCENT,
            segmented_button_selected_hover_color=T.THEME_HOV,
            segmented_button_unselected_color=T.CARD_2,
            text_color=T.FG, corner_radius=12, command=on_tab_change)
        tabs.pack(fill="both", expand=True)
        tab_game = tabs.add("Game")
        tab_launcher = tabs.add("Launcher")
        
        def _force_refresh(tab_name):
            if ui.get("last_tab") != tab_name:
                return
            from .util import mc_releases, gh_releases
            from .config import SELF_REPO
            if tab_name == "Game":
                load_tab_changelog(tab_game, lambda: mc_releases(fetch_all=False, ignore_cache=True), render_game_changelog)
            elif tab_name == "Launcher":
                load_tab_changelog(tab_launcher, lambda: gh_releases(SELF_REPO, ignore_cache=True), render_launcher_changelog)
                
        try:
            for btn in tabs._segmented_button._buttons_dict.values():
                btn.bind("<Button-1>", lambda e, b=btn: _force_refresh(b.cget("text")), add="+")
        except Exception:
            pass

    _build_changelog_view()

    # ==================================================================
    # Self-update
    # ==================================================================
    def relaunch_app():
        na.stop()
        try:
            if os.environ.get("APPIMAGE"):
                os.execv(os.environ["APPIMAGE"], [os.environ["APPIMAGE"], "gui"])
            tgt = os.path.realpath(sys.argv[0] or __file__)
            os.execv(sys.executable, [sys.executable, tgt, "gui"])
        except Exception:
            root.destroy()

    def update_progress(got, total):
        def ap():
            _show_bar()
            prog.stop()
            prog.configure(mode="determinate")
            prog.set(got / max(1, total))
            status_txt.set(f"Downloading update…  {int(100 * got / max(1, total))}%")
            status_lbl.configure(text_color=T.FG)
        root.after(0, ap)

    def restart_prompt():
        d = dialog("Update installed", 340, 150)
        ctk.CTkLabel(d, text="Update installed", font=font(14, "bold"),
                     text_color=T.FG).pack(anchor="w", padx=24, pady=(22, 0))
        ctk.CTkLabel(d, text="Restart now to run the new version?",
                     text_color=T.SUB).pack(anchor="w", padx=24, pady=(4, 16))
        row = ctk.CTkFrame(d, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(0, 22))
        mkbtn(row, "Restart now", relaunch_app, kind="primary", width=130,
              height=38, font=font(13, "bold")).pack(side="right")
        mkbtn(row, "Later", d.destroy, kind="ghost", width=90,
              height=38).pack(side="right", padx=(0, 8))

    def run_update(rel, banner):
        banner.destroy()
        set_status(f"Updating to v{rel['version']}…", T.FG)
        bar_busy()

        def done(state, msg):
            end_progress()
            set_status(msg, T.GREEN if state == "ok"
                       else (T.RED if state == "error" else T.SUB))
            if state == "ok":
                restart_prompt()

        def work():
            state, msg = self_update(rel, progress=update_progress)
            root.after(0, lambda: done(state, msg))
        threading.Thread(target=work, daemon=True).start()

    def show_update_banner(rel):
        bn = ctk.CTkFrame(root, fg_color=T.BLUE_DIM, corner_radius=12,
                           border_width=1, border_color=T.BLUE)
        ctk.CTkLabel(bn, text=f"⟳   Update available — v{rel['version']}   "
                     f"(you have {VERSION})", text_color=T.BLUE_LIGHT,
                     font=font(12, "bold")).pack(side="left", padx=18, pady=9)
        mkbtn(bn, "Later", bn.destroy, kind="flat", width=64, height=30,
              text_color=T.BLUE_MUTED, hover_color=T.BLUE_DARK).pack(
                  side="right", padx=(0, 14), pady=7)
        mkbtn(bn, "Update now", lambda: run_update(rel, bn), kind="primary",
              width=112, height=30, font=font(12, "bold")).pack(
                  side="right", padx=(0, 6), pady=7)
        bn.pack(fill="x", padx=22, pady=(0, 6), after=top)

    def update_check():
        rel = check_for_update()
        if rel:
            root.after(0, lambda: show_update_banner(rel))

    acct_state("in" if msa_signed_in() else "out")
    threading.Thread(target=refresh_versions, daemon=True).start()
    threading.Thread(target=update_check, daemon=True).start()
    
    hero.pack(fill="both", expand=True)

    root.update_idletasks()

    def on_close():
        if ui.get("launch_active"):
            messagebox.showwarning(
                "Minecraft is running",
                "Close Minecraft first and wait for the launcher to report "
                "that it closed. To abort it, use Settings → Tools → Force "
                "stop Minecraft.",
                parent=root,
            )
            return
        if ui.get("busy"):
            messagebox.showwarning(
                "Operation in progress",
                "Wait for the current preparation task to finish before "
                "closing the launcher.",
                parent=root,
            )
            return
        na.stop()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    def on_enter_pressed(e):
        focus = root.focus_get()
        if isinstance(focus, (tk.Entry, tk.Text)):
            return "break"
        do_play()

    root.bind("<Return>", on_enter_pressed)
    
    if load_settings().get("show_changelog_on_startup", False):
        toggle_changelog()
        
    root.mainloop()
