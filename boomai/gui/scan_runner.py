"""Threaded scan execution with progress tracking for the GUI."""

from __future__ import annotations

import asyncio
import io
import os
import re
import sys
import threading
import time
import traceback

from ..analysis.languages import detect_languages, filter_reviewable_files
from ..app.services.file_selection_service import read_file_contents
from ..app.services.profile_service import apply_scan_profile
from ..context.indexer import build_code_index
from ..core.config import settings
from ..core.models import ReviewSummary
from ..integrations.google.models_catalog_service import ModelCatalogService
from ..review.estimation_history import record_run
from ..review.estimator import get_pricing
from ..review.progress_history import ChunkProgressFeatures, predict_chunk_elapsed_seconds
from ..review.services.review_workflow import ReviewWorkflow

# ── Regex patterns (same as CLI's ScanProgressDisplay) ────────

_RE_PLANNED = re.compile(r"(\d+) chunk\(s\) planned, model: (.+)")
_RE_CHUNK_START = re.compile(
    r"(\[\d+/\d+\](?:/[A-Za-z0-9_-]+)*)\s+(\d+)\s+files,\s+([\d,]+)\s+chars\.\.\."
)
_RE_CHUNK_COMPLETED = re.compile(
    r"(\[\d+/\d+\](?:/[A-Za-z0-9_-]+)*)\s+completed$"
)
_RE_CHUNK_DONE = re.compile(
    r"(\[\d+/\d+\](?:/[A-Za-z0-9_-]+)*)\s+done"
)
_RE_CHUNK_SPLIT = re.compile(
    r"(\[\d+/\d+\](?:/[A-Za-z0-9_-]+)*)\s+failed .*splitting"
)
_RE_HEARTBEAT = re.compile(
    r"\[(\d+)/(\d+)\](?:/[A-Za-z0-9_-]+)?\.\.\. \(\d+s\)"
)
_RE_SINGLE_CHUNK = re.compile(r"Reviewing (\d+) chunk")


def _normalize_label(raw: str) -> str:
    return raw.strip().replace("[", "").replace("]", "")


def _label_weight(label: str) -> float:
    parts = label.split("/")
    depth = max(0, len(parts) - 2)
    return 1.0 / (2 ** depth)


class ScanRunner:
    """Runs the full scan in a background thread, exposing progress for JS polling.

    Progress tracking mirrors CLI's ScanProgressDisplay: time-based interpolation
    within active chunks so the bar moves smoothly between chunk completions.
    """

    def __init__(
        self,
        repo_path: str,
        selected_files: list[str],
        profile: str = "default",
        comments: bool = False,
        shallow: bool = False,
        estimate_features=None,
    ) -> None:
        self.repo_path = repo_path
        self.selected_files = selected_files
        self.profile = profile
        self.comments = comments
        self.shallow = shallow
        self._estimate_features = estimate_features

        self.state = "idle"
        self.stage = ""
        self.total_files = 0
        self.completed_files = 0
        self.review: ReviewSummary | None = None
        self.applied_count = 0
        self.elapsed = 0.0
        self.error: str | None = None

        self._messages: list[str] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancelled = False

        # Chunk tracking (mirrors CLI progress.py)
        self._scan_model = ""
        self._total_chunks = 0
        self._completed_chunks = 0
        self._completed_labels: set[str] = set()
        self._active_labels: set[str] = set()
        self._label_file_counts: dict[str, int] = {}
        self._label_char_counts: dict[str, int] = {}
        self._label_started_at: dict[str, float] = {}
        self._label_expected_seconds: dict[str, float] = {}

    def start(self) -> None:
        self.state = "running"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled = True

    def poll(self) -> dict:
        with self._lock:
            msgs = list(self._messages)
            self._messages.clear()

        # Compute smooth progress using time-based interpolation (same as CLI)
        progress, estimated_files = self._compute_progress()

        return {
            "state": self.state,
            "progress": round(progress, 3),
            "stage": self.stage,
            "messages": msgs,
            "total_files": self.total_files,
            "completed_files": self.completed_files,
            "estimated_files": estimated_files,
            "total_chunks": self._total_chunks,
            "completed_chunks": self._completed_chunks,
            "error": self.error,
        }

    def _compute_progress(self) -> tuple[float, int]:
        """Time-based interpolation, same algorithm as CLI's render_bar()."""
        if self.total_files <= 0:
            return 0.0, 0

        estimated_units = float(self.completed_files)
        now = time.monotonic()

        for label in self._active_labels:
            file_count = self._label_file_counts.get(label, 0)
            started_at = self._label_started_at.get(label, now)
            expected = self._label_expected_seconds.get(label, 0.0)
            if file_count <= 0 or expected <= 0:
                continue
            active_ratio = min(0.95, max(0.0, (now - started_at) / expected))
            estimated_units += file_count * active_ratio

        completed_units = min(float(self.total_files), estimated_units)
        progress = min(0.95, completed_units / self.total_files)
        return progress, int(completed_units)

    def _emit(self, msg: str) -> None:
        with self._lock:
            self._messages.append(msg)
        self._parse_progress(msg)

    def _parse_progress(self, msg: str) -> None:
        """Parse progress messages using the same regex as CLI's ScanProgressDisplay."""
        plain = msg.strip()

        # "7 chunk(s) planned, model: gemini-3.1-pro-preview"
        m = _RE_PLANNED.match(plain)
        if m:
            self._total_chunks = int(m.group(1))
            self._scan_model = m.group(2)
            self._completed_chunks = 0
            self._completed_labels.clear()
            self._active_labels.clear()
            self._label_file_counts.clear()
            self._label_char_counts.clear()
            self._label_started_at.clear()
            self._label_expected_seconds.clear()
            self.completed_files = 0
            self.stage = f"Reviewing files (0/{self.total_files})..."
            return

        # "Reviewing 1 chunk ..."
        m = _RE_SINGLE_CHUNK.match(plain)
        if m:
            self._total_chunks = 1
            self.stage = f"Reviewing files (0/{self.total_files})..."
            return

        # "[1/7] 3 files, 44,537 chars..."  — chunk started
        m = _RE_CHUNK_START.match(plain)
        if m:
            label = _normalize_label(m.group(1))
            file_count = int(m.group(2))
            char_count = int(m.group(3).replace(",", ""))
            self._active_labels.add(label)
            self._label_file_counts[label] = file_count
            self._label_char_counts[label] = char_count
            self._label_started_at[label] = time.monotonic()

            # Predict expected duration (same as CLI)
            split_depth = max(0, len(label.split("/")) - 2)
            predicted, _ = predict_chunk_elapsed_seconds(
                ChunkProgressFeatures(
                    chunk_chars=char_count,
                    file_count=file_count,
                    split_depth=split_depth,
                    scan_model_flash=int("flash" in self._scan_model.lower()),
                    profile_deep=int(self.profile == "deep"),
                )
            )
            self._label_expected_seconds[label] = predicted
            return

        # "[1/7] failed — splitting..."
        m = _RE_CHUNK_SPLIT.match(plain)
        if m:
            label = _normalize_label(m.group(1))
            self._active_labels.discard(label)
            self._label_started_at.pop(label, None)
            self._label_expected_seconds.pop(label, None)
            return

        # "[2/7] completed"
        m = _RE_CHUNK_COMPLETED.match(plain)
        if m:
            self._mark_done(_normalize_label(m.group(1)))
            return

        # "[2/7] done — 1 issues (1/7 complete)"
        m = _RE_CHUNK_DONE.match(plain)
        if m:
            self._mark_done(_normalize_label(m.group(1)))
            return

        # Planning phase
        if "planning" in plain.lower():
            self.stage = "Planning review chunks..."

        # Patching phase
        if "patching " in plain.lower():
            self.stage = "Generating fixes..."

    def _mark_done(self, label: str) -> None:
        if label in self._completed_labels:
            return
        self._completed_labels.add(label)
        self._completed_chunks += 1

        file_count = self._label_file_counts.get(label, 0)
        self.completed_files = min(self.total_files, self.completed_files + file_count)

        self._active_labels.discard(label)
        self._label_started_at.pop(label, None)
        self._label_expected_seconds.pop(label, None)

        self.stage = f"Reviewing files ({self.completed_files}/{self.total_files})..."

    def _run(self) -> None:
        started = time.monotonic()

        # Suppress stdout during scan so CLI print() calls don't leak
        original_stdout = sys.stdout
        sys.stdout = io.StringIO()

        try:
            apply_scan_profile(self.profile)

            catalog = ModelCatalogService()
            runtime_models = catalog.get_runtime_models()
            catalog.apply_runtime_models(runtime_models)

            files = list(self.selected_files)
            if self.shallow:
                files = [f for f in files if "/" not in f]

            reviewable = filter_reviewable_files(files)
            self.total_files = len(reviewable)
            languages = detect_languages(files)

            self._emit(f"Reading {len(reviewable)} files...")
            self.stage = "Reading files..."
            file_contents = read_file_contents(reviewable, self.repo_path)

            self._emit("Building code index...")
            self.stage = "Building code index..."
            code_index = build_code_index(file_contents, languages)

            self.stage = "Starting AI review..."
            workflow = ReviewWorkflow()
            self.review = asyncio.run(
                workflow.run_local_scan(
                    repo_path=self.repo_path,
                    comments=self.comments,
                    runtime_models=runtime_models,
                    on_progress=self._emit,
                    file_contents=file_contents,
                    code_index=code_index,
                )
            )

            # No auto-apply — user chooses which fixes to apply on results screen
            self.applied_count = 0
            self.elapsed = time.monotonic() - started

            # Record run for calibration (same as CLI)
            if self._estimate_features and self.review.usage and self.review.usage.api_calls > 0:
                record_run(
                    features=self._estimate_features,
                    elapsed_seconds=self.elapsed,
                    usage=self.review.usage,
                    findings_count=len(self.review.findings),
                    applied_count=0,
                    get_pricing=get_pricing,
                )

            self.progress = 1.0
            self.stage = "Complete"
            self.state = "done"
            self._emit(f"Scan complete — {len(self.review.findings)} issues found")

        except Exception as exc:
            self.elapsed = time.monotonic() - started
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "error"
            self._emit(f"Error: {self.error}")
            traceback.print_exc(file=original_stdout)
        finally:
            sys.stdout = original_stdout
