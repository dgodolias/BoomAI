from __future__ import annotations

import os
import sys

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
BOOM_COLORS = [
    (104, 41, 196),
    (139, 48, 214),
    (180, 60, 221),
    (222, 82, 202),
    (255, 119, 171),
]
TITLE_LINES = [
    "BOOMAI",
]
ACCENT = "pixel-powered code fixer for C#/Unity"


def enable_windows_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        if handle in (0, -1):
            return False
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return False
        if kernel32.SetConsoleMode(handle, mode.value | 0x0004) == 0:
            return False
        return True
    except Exception:
        return False


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    term = os.environ.get("TERM", "")
    if term.lower() == "dumb":
        return False
    return enable_windows_vt()


def rgb(color: tuple[int, int, int], text: str, *, bold: bool = False, dim: bool = False) -> str:
    prefix = []
    if bold:
        prefix.append(BOLD)
    if dim:
        prefix.append(DIM)
    prefix.append(f"\033[38;2;{color[0]};{color[1]};{color[2]}m")
    return "".join(prefix) + text + RESET


def gradient_text(text: str, colors: list[tuple[int, int, int]]) -> str:
    if not text or not supports_color():
        return text

    colored: list[str] = []
    steps = max(1, len(text) - 1)
    segments = len(colors) - 1
    for index, char in enumerate(text):
        if char == " ":
            colored.append(char)
            continue
        position = index / steps
        segment = min(segments - 1, int(position * segments))
        local_start = segment / segments
        local_end = (segment + 1) / segments
        local_t = 0.0 if local_end == local_start else (position - local_start) / (local_end - local_start)
        start = colors[segment]
        end = colors[segment + 1]
        color = (
            int(start[0] + (end[0] - start[0]) * local_t),
            int(start[1] + (end[1] - start[1]) * local_t),
            int(start[2] + (end[2] - start[2]) * local_t),
        )
        colored.append(rgb(color, char, bold=True))
    return "".join(colored)


def print_banner() -> None:
    print()
    if supports_color():
        divider = rgb((186, 106, 220), "· " * 28, dim=True)
        print(f"  {divider}")
        print()
        for line in TITLE_LINES:
            print(f"    {gradient_text(line, BOOM_COLORS)}")
        print()
        print(f"    {rgb((214, 126, 220), ACCENT, dim=True)}")
        print(f"  {divider}")
    else:
        print("  BOOMAI")
        print(f"  {ACCENT}")
    print()
