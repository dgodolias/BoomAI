"""Threaded scan execution with progress tracking for the GUI."""

from __future__ import annotations

import asyncio
import threading
import time
import traceback

from ..analysis.languages import detect_languages, filter_reviewable_files
from ..app.services.file_selection_service import read_file_contents
from ..app.services.local_patch_service import apply_local
from ..app.services.profile_service import apply_scan_profile
from ..context.indexer import build_code_index
from ..core.config import settings
from ..core.models import ReviewSummary
from ..integrations.google.models_catalog_service import ModelCatalogService
from ..review.services.review_workflow import ReviewWorkflow


class ScanRunner:
    """Runs the full scan in a background thread, exposing progress for JS polling."""

    def __init__(
        self,
        repo_path: str,
        selected_files: list[str],
        profile: str = "default",
        comments: bool = False,
        shallow: bool = False,
    ) -> None:
        self.repo_path = repo_path
        self.selected_files = selected_files
        self.profile = profile
        self.comments = comments
        self.shallow = shallow

        self.state = "idle"
        self.progress = 0.0
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

        # Parse chunk progress from on_progress messages
        self._total_chunks = 0
        self._completed_chunks = 0

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
        return {
            "state": self.state,
            "progress": round(self.progress, 3),
            "stage": self.stage,
            "messages": msgs,
            "total_files": self.total_files,
            "completed_files": self.completed_files,
            "total_chunks": self._total_chunks,
            "completed_chunks": self._completed_chunks,
            "error": self.error,
        }

    def _emit(self, msg: str) -> None:
        with self._lock:
            self._messages.append(msg)
        self._parse_progress(msg)

    def _parse_progress(self, msg: str) -> None:
        """Extract chunk progress from progress messages."""
        lower = msg.lower()

        # Detect total chunks
        if "chunks" in lower and "planning" not in lower:
            import re
            m = re.search(r"(\d+)\s*chunk", msg)
            if m:
                self._total_chunks = int(m.group(1))

        # Detect chunk completion
        if any(kw in lower for kw in ["done", "completed", "finished"]):
            self._completed_chunks += 1
            if self._total_chunks > 0:
                self.progress = min(0.95, self._completed_chunks / self._total_chunks)
                self.completed_files = int(self.progress * self.total_files)

        # Detect stage
        if "planning" in lower or "plan" in lower:
            self.stage = "Planning review chunks..."
        elif "review" in lower or "scan" in lower:
            if self._total_chunks > 0:
                self.stage = f"Reviewing chunk {self._completed_chunks + 1}/{self._total_chunks}..."
            else:
                self.stage = "Reviewing code..."
        elif "fix" in lower or "patch" in lower:
            self.stage = "Generating fixes..."

    def _run(self) -> None:
        started = time.monotonic()
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

            # Auto-apply fixes
            if self.review and self.review.findings:
                self.stage = "Applying fixes..."
                self._emit("Applying fixes to files...")
                self.applied_count = apply_local(self.review.findings, self.repo_path)

            self.progress = 1.0
            self.elapsed = time.monotonic() - started
            self.stage = "Complete"
            self.state = "done"
            self._emit(f"Scan complete — {len(self.review.findings)} issues, {self.applied_count} fixes applied")

        except Exception as exc:
            self.elapsed = time.monotonic() - started
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "error"
            self._emit(f"Error: {self.error}")
            traceback.print_exc()
