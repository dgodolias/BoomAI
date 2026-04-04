"""BoomAI GUI launcher — opens a native desktop window via pywebview."""

from __future__ import annotations

from pathlib import Path

import webview

from .bridge import BoomAIBridge


_FRONTEND = Path(__file__).parent / "frontend" / "index.html"


def launch_gui() -> None:
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
    webview.start(debug=False)
