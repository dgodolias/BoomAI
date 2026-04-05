"""BoomAI GUI launcher — opens a native desktop window via pywebview."""

from __future__ import annotations

import sys
from pathlib import Path

import webview

from .bridge import BoomAIBridge


_ASSETS = Path(__file__).parent / "frontend" / "assets"
_FRONTEND = Path(__file__).parent / "frontend" / "index.html"
_ICON = _ASSETS / "icon.ico"


def _set_windows_icon() -> None:
    """Set window + taskbar icon on Windows using Win32 API."""
    if sys.platform != "win32" or not _ICON.exists():
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        icon_path = str(_ICON.resolve())

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        GCL_HICON = -14
        GCL_HICONSM = -34

        hicon_big = user32.LoadImageW(
            None, icon_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE | LR_DEFAULTSIZE,
        )
        hicon_small = user32.LoadImageW(
            None, icon_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE,
        )

        hwnd = user32.FindWindowW(None, "BoomAI")
        if not hwnd:
            return

        # Set class-level icons (taskbar + Alt+Tab)
        if hicon_big:
            user32.SetClassLongPtrW(hwnd, GCL_HICON, hicon_big)
        if hicon_small:
            user32.SetClassLongPtrW(hwnd, GCL_HICONSM, hicon_small)

        # Set window-level icons (title bar)
        if hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        if hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
    except Exception:
        pass


def launch_gui() -> None:
    # Set AppUserModelID before window creation (taskbar grouping)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BoomCorp.BoomAI.1")
        except Exception:
            pass

    bridge = BoomAIBridge()
    window = webview.create_window(
        title="BoomAI",
        url=str(_FRONTEND),
        js_api=bridge,
        width=1100,
        height=750,
        min_size=(900, 600),
        background_color="#1A0632",
    )
    bridge.set_window(window)

    def _on_shown():
        _set_windows_icon()

    window.events.shown += _on_shown
    webview.start(debug=False)
