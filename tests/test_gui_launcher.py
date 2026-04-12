from __future__ import annotations

from types import SimpleNamespace

from boomai.gui import launcher


class _EventHook:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


def test_launch_gui_swallows_keyboard_interrupt(monkeypatch) -> None:
    fake_window = SimpleNamespace(events=SimpleNamespace(shown=_EventHook()))
    create_calls: list[dict[str, object]] = []
    bridge_instances: list[object] = []

    class FakeBridge:
        def __init__(self) -> None:
            self.window = None
            bridge_instances.append(self)

        def set_window(self, window) -> None:
            self.window = window

    def fake_create_window(**kwargs):
        create_calls.append(kwargs)
        return fake_window

    def fake_start(debug: bool = False) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(launcher, "BoomAIBridge", FakeBridge)
    monkeypatch.setattr(launcher.webview, "create_window", fake_create_window)
    monkeypatch.setattr(launcher.webview, "start", fake_start)
    monkeypatch.setattr(launcher, "_set_windows_icon", lambda: None)

    launcher.launch_gui()

    assert len(create_calls) == 1
    assert create_calls[0]["title"] == "BoomAI"
    assert len(fake_window.events.shown.handlers) == 1
    assert len(bridge_instances) == 1
    assert bridge_instances[0].window is fake_window
