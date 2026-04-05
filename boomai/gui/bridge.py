"""Python API exposed to the JS frontend via pywebview js_api."""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

import webview

from ..analysis.languages import detect_languages, filter_reviewable_files
from ..app.services.file_selection_service import collect_files, read_file_contents
from ..app.services.settings_service import (
    mask_api_key,
    save_setting,
    refresh_runtime_model_catalog,
)
from ..core.config import settings
from .scan_runner import ScanRunner

_RECENT_FILE = Path.home() / ".boomai" / "gui_recent.json"


class BoomAIBridge:
    """Every public method is callable from JS as window.pywebview.api.methodName()."""

    def __init__(self, cwd: str | None = None) -> None:
        self._window: webview.Window | None = None
        self._scan_runner: ScanRunner | None = None
        self._cwd = cwd or os.path.abspath(".")

    def set_window(self, window: webview.Window) -> None:
        self._window = window

    # ── Project / Folder ─────────────────────────────────

    def select_folder(self) -> dict:
        if self._window is None:
            return {"path": None}
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG, directory=self._cwd,
        )
        if result and len(result) > 0:
            path = str(result[0])
            self._add_recent(path)
            return {"path": path}
        return {"path": None}

    def get_cwd(self) -> dict:
        """Return the directory from which the GUI was launched."""
        return {"path": self._cwd}

    def get_file_tree(self, repo_path: str) -> dict:
        try:
            all_files = collect_files(repo_path)
            reviewable = set(filter_reviewable_files(all_files))
            languages = detect_languages(all_files)

            tree: dict = {}
            for f in all_files:
                parts = f.split("/")
                node = tree
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = {"__file": True, "__reviewable": f in reviewable}

            return {
                "tree": tree,
                "total": len(all_files),
                "reviewable_count": len(reviewable),
                "languages": languages,
            }
        except Exception as exc:
            return {"error": str(exc)}

    def get_recent_projects(self) -> list:
        if not _RECENT_FILE.exists():
            return []
        try:
            return json.loads(_RECENT_FILE.read_text("utf-8"))[:8]
        except Exception:
            return []

    def _add_recent(self, path: str) -> None:
        recent = self.get_recent_projects()
        path_norm = path.replace("\\", "/")
        recent = [p for p in recent if p.replace("\\", "/") != path_norm]
        recent.insert(0, path)
        recent = recent[:8]
        _RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RECENT_FILE.write_text(json.dumps(recent), "utf-8")

    # ── Settings ─────────────────────────────────────────

    def get_settings(self) -> dict:
        return {
            "api_key_set": bool(settings.google_api_key),
            "api_key_masked": mask_api_key(settings.google_api_key or ""),
            "scan_profile": settings.scan_profile,
            "scan_comments": settings.scan_comments,
            "scan_debug": settings.scan_debug,
            "cost_reporting_enabled": settings.cost_reporting_enabled,
        }

    def set_api_key(self, key: str) -> dict:
        save_setting("BOOMAI_GOOGLE_API_KEY", key.strip())
        settings.google_api_key = key.strip()
        return {"ok": True}

    def save_setting(self, key: str, value: str) -> dict:
        env_key = f"BOOMAI_{key.upper()}"
        save_setting(env_key, value)
        # Update runtime setting
        attr = key.lower()
        if hasattr(settings, attr):
            current = getattr(settings, attr)
            if isinstance(current, bool):
                setattr(settings, attr, value.lower() in ("true", "1", "yes"))
            else:
                setattr(settings, attr, value)
        return {"ok": True}

    def get_api_key_status(self) -> dict:
        return {
            "configured": bool(settings.google_api_key),
            "masked": mask_api_key(settings.google_api_key or ""),
        }

    # ── Estimation ───────────────────────────────────────

    def estimate(self, repo_path: str, selected_files: list, profile: str, shallow: bool) -> dict:
        try:
            from ..app.services.profile_service import apply_scan_profile
            from ..integrations.google.models_catalog_service import ModelCatalogService
            from ..review.estimator import estimate_scan

            apply_scan_profile(profile)
            catalog = ModelCatalogService()
            runtime_models = catalog.get_runtime_models()
            catalog.apply_runtime_models(runtime_models)

            files = selected_files
            if shallow:
                files = [f for f in files if "/" not in f]

            reviewable = filter_reviewable_files(files)
            file_contents = read_file_contents(reviewable, repo_path)
            languages = detect_languages(files)

            est = estimate_scan(
                file_contents=file_contents,
                model=runtime_models.strong_model_id,
                patch_model=runtime_models.weak_model_id,
                model_label=runtime_models.strong_display_name,
                patch_model_label=runtime_models.weak_display_name,
                max_scan_chars=settings.max_scan_chars,
                scan_output_tokens=settings.scan_output_tokens,
                plan_output_tokens=settings.plan_output_tokens,
                profile=settings.scan_profile,
                patch_max_findings_per_chunk=settings.patch_max_findings_per_chunk,
                languages=languages,
            )

            return {
                "profile": est.profile,
                "model": est.model_label,
                "patch_model": est.patch_model_label,
                "file_count": est.file_count,
                "total_chars": est.total_chars,
                "chunk_count": est.chunk_count,
                "api_calls_low": est.total_api_calls_low,
                "api_calls_high": est.total_api_calls_high,
                "input_tokens_low": est.input_tokens_low,
                "input_tokens_high": est.input_tokens_high,
                "output_tokens_low": est.output_tokens_low,
                "output_tokens_high": est.output_tokens_high,
                "cost_min": round(est.cost_min, 4),
                "cost_max": round(est.cost_max, 4),
                "time_min": round(est.time_min),
                "time_max": round(est.time_max),
                "learned_samples": est.learned_samples,
            }
        except Exception as exc:
            traceback.print_exc()
            return {"error": str(exc)}

    # ── Scan ─────────────────────────────────────────────

    def start_scan(self, repo_path: str, selected_files: list,
                   profile: str, comments: bool, shallow: bool) -> dict:
        if self._scan_runner and self._scan_runner.state == "running":
            return {"error": "Scan already running"}
        try:
            self._scan_runner = ScanRunner(
                repo_path=repo_path,
                selected_files=selected_files,
                profile=profile,
                comments=comments,
                shallow=shallow,
            )
            self._scan_runner.start()
            return {"started": True}
        except Exception as exc:
            traceback.print_exc()
            return {"error": str(exc)}

    def get_scan_status(self) -> dict:
        if not self._scan_runner:
            return {"state": "idle"}
        return self._scan_runner.poll()

    def cancel_scan(self) -> dict:
        if self._scan_runner:
            self._scan_runner.cancel()
            return {"cancelled": True}
        return {"cancelled": False}

    def get_scan_results(self) -> dict:
        if not self._scan_runner or not self._scan_runner.review:
            return {"error": "No results available"}
        review = self._scan_runner.review
        findings = []
        for i, f in enumerate(review.findings):
            findings.append({
                "index": i,
                "file": f.file,
                "line": f.line,
                "end_line": f.end_line,
                "severity": f.severity.value if hasattr(f.severity, "value") else str(f.severity),
                "body": f.body,
                "category": f.category,
                "confidence": f.confidence,
                "fixable": bool(f.suggestion and f.old_code),
                "suggestion": f.suggestion,
                "old_code": f.old_code,
            })
        usage = None
        if review.usage:
            usage = {
                "prompt_tokens": review.usage.prompt_tokens,
                "completion_tokens": review.usage.completion_tokens,
                "api_calls": review.usage.api_calls,
            }
        return {
            "summary": review.summary,
            "findings": findings,
            "critical_count": review.critical_count,
            "has_critical": review.has_critical,
            "usage": usage,
            "elapsed": round(self._scan_runner.elapsed, 1),
            "applied_count": self._scan_runner.applied_count,
        }

    # ── Fix Application ──────────────────────────────────

    def apply_fixes(self, repo_path: str, finding_indices: list) -> dict:
        if not self._scan_runner or not self._scan_runner.review:
            return {"error": "No scan results"}
        try:
            from ..app.services.local_patch_service import apply_local
            selected = [self._scan_runner.review.findings[i] for i in finding_indices
                        if i < len(self._scan_runner.review.findings)]
            count = apply_local(selected, repo_path)
            return {"applied": count}
        except Exception as exc:
            return {"error": str(exc)}
