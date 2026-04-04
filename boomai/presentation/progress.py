from __future__ import annotations

import re
import sys
import time

from ..core.models import ReviewSummary
from ..core.policies import build_progress_policy
from ..review.progress_history import ChunkProgressFeatures, predict_chunk_elapsed_seconds


class ScanProgressDisplay:
    """Verbose debug logs or a compact progress bar, depending on scan_debug."""

    def __init__(self, *, debug: bool, total_files: int = 0, scan_model: str = "", profile: str = "default"):
        self.debug = debug
        self.total_files = max(0, int(total_files))
        self.scan_model = scan_model
        self.profile = profile
        self.total_chunks = 0
        self.total_weight = 0.0
        self.completed_weight = 0.0
        self.completed_files = 0
        self.completed_labels: set[str] = set()
        self.active_labels: set[str] = set()
        self.label_file_counts: dict[str, int] = {}
        self.label_char_counts: dict[str, int] = {}
        self.label_started_at: dict[str, float] = {}
        self.label_expected_seconds: dict[str, float] = {}
        self._bar_visible = False
        self._spinner_index = 0

    @staticmethod
    def normalize_label(raw_label: str) -> str:
        return raw_label.strip().replace("[", "").replace("]", "")

    def label_weight(self, label: str) -> float:
        parts = label.split("/")
        depth = max(0, len(parts) - 2)
        return 1.0 / (2 ** depth)

    def clear_bar(self) -> None:
        if self.debug:
            return
        if self._bar_visible:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._bar_visible = False

    def render_bar(self) -> None:
        if self.debug:
            return
        progress_policy = build_progress_policy()
        if self.total_files > 0:
            total_units = float(self.total_files)
            estimated_units = float(self.completed_files)
            now = time.monotonic()
            for label in self.active_labels:
                file_count = self.label_file_counts.get(label, 0)
                started_at = self.label_started_at.get(label, now)
                expected = self.label_expected_seconds.get(label, 0.0)
                if file_count <= 0 or expected <= 0:
                    continue
                active_ratio = min(0.95, max(0.0, (now - started_at) / expected))
                estimated_units += file_count * active_ratio
            completed_units = min(total_units, estimated_units)
            suffix = f" (est. {completed_units:.0f}/{self.total_files} files)"
        else:
            if self.total_weight <= 0:
                return
            total_units = self.total_weight
            completed_units = self.completed_weight
            suffix = ""
        ratio = min(1.0, completed_units / total_units)
        filled = int(round(progress_policy.bar_width * ratio))
        bar = "#" * filled + "." * (progress_policy.bar_width - filled)
        percent = int(round(ratio * 100))
        spinner = ""
        if self.active_labels and completed_units < total_units:
            frame_index = self._spinner_index % len(progress_policy.spinner_frames)
            spinner = f" {progress_policy.spinner_frames[frame_index]}"
            self._spinner_index += 1
        sys.stdout.write(f"\r  Scan progress: [{bar}] {percent:3d}%{suffix}{spinner}")
        sys.stdout.flush()
        self._bar_visible = True

    def emit(self, msg: str) -> None:
        plain = msg.strip()
        if self.debug:
            print(f"  {plain}")
            return

        if plain in {"Planning review chunks...", "Using greedy chunking"}:
            self.clear_bar()
            print(f"  {plain}")
            return

        planned = re.match(r"(\d+) chunk\(s\) planned, model: (.+)", plain)
        if planned:
            self.clear_bar()
            self.total_chunks = int(planned.group(1))
            self.total_weight = float(self.total_chunks)
            self.completed_weight = 0.0
            self.completed_files = 0
            self.completed_labels.clear()
            self.active_labels.clear()
            self.label_file_counts.clear()
            print(f"  Planned {self.total_chunks} review chunks")
            return

        if plain.startswith("Reviewing code"):
            self.clear_bar()
            print("  Reviewing code...")
            return

        chunk_start = re.match(r"(\[\d+/\d+\](?:/[A-Za-z0-9_-]+)*)\s+(\d+)\s+files,\s+([\d,]+)\s+chars\.\.\.", plain)
        if chunk_start:
            label = self.normalize_label(chunk_start.group(1))
            file_count = int(chunk_start.group(2))
            char_count = int(chunk_start.group(3).replace(",", ""))
            self.active_labels.add(label)
            self.label_file_counts[label] = file_count
            self.label_char_counts[label] = char_count
            self.label_started_at[label] = time.monotonic()
            split_depth = max(0, len(label.split("/")) - 2)
            predicted_seconds, _ = predict_chunk_elapsed_seconds(
                ChunkProgressFeatures(
                    chunk_chars=char_count,
                    file_count=file_count,
                    split_depth=split_depth,
                    scan_model_flash=int("flash" in self.scan_model.lower()),
                    profile_deep=int(self.profile == "deep"),
                )
            )
            self.label_expected_seconds[label] = predicted_seconds
            self.render_bar()
            return

        split = re.match(r"(\[\d+/\d+\](?:/[A-Za-z0-9_-]+)*)\s+failed .*splitting", plain)
        if split:
            label = self.normalize_label(split.group(1))
            self.active_labels.discard(label)
            self.label_started_at.pop(label, None)
            self.label_expected_seconds.pop(label, None)
            self.render_bar()
            return

        completed = re.match(r"(\[\d+/\d+\](?:/[A-Za-z0-9_-]+)*)\s+completed$", plain)
        if completed:
            label = self.normalize_label(completed.group(1))
            if label not in self.completed_labels:
                self.completed_labels.add(label)
                self.completed_weight = min(self.total_weight, self.completed_weight + self.label_weight(label))
                if self.total_files > 0:
                    self.completed_files = min(
                        self.total_files,
                        self.completed_files + self.label_file_counts.get(label, 0),
                    )
            self.active_labels.discard(label)
            self.label_started_at.pop(label, None)
            self.label_expected_seconds.pop(label, None)
            self.render_bar()
            if (
                (self.total_files > 0 and self.completed_files >= self.total_files)
                or (self.total_files <= 0 and self.completed_weight >= self.total_weight)
            ):
                self.finish()
            return

        heartbeat = re.match(r"\[(\d+)/(\d+)\](?:/[A-Za-z0-9_-]+)?\.\.\. \(\d+s\)", plain)
        if heartbeat or "patching " in plain:
            self.render_bar()
            return

        if plain.startswith("Done") or "rate limited" in plain.lower():
            self.clear_bar()
            print(f"  {plain}")

    def chunk_done(self, _chunk_review: ReviewSummary) -> None:
        return

    def finish(self) -> None:
        self.clear_bar()
