from __future__ import annotations

import asyncio
import os
import time
import traceback

from ...analysis.languages import detect_languages, filter_reviewable_files
from ...analysis.services.static_analysis_service import StaticAnalysisService
from ...context.indexer import build_code_index
from ...core.config import settings
from ...integrations.google.models_catalog_service import ModelCatalogService
from ...presentation.estimate_output import format_estimate
from ...presentation.progress import ScanProgressDisplay
from ...presentation.review_output import print_review
from ...review.estimation_history import record_run
from ...review.estimator import estimate_scan, get_pricing
from ...review.run_cost_report import write_run_cost_report
from ...review.services.review_workflow import ReviewWorkflow
from .file_selection_service import collect_files, read_file_contents, report_unreadable_files, select_target_files
from .local_patch_service import apply_local
from .profile_service import apply_scan_profile
from .settings_service import require_api_key


class FixWorkflow:
    """Owns the full CLI fix lifecycle without embedding it in cli.py."""

    def __init__(self) -> None:
        self.model_catalog_service = ModelCatalogService()
        self.static_analysis_service = StaticAnalysisService()
        self.review_workflow = ReviewWorkflow()

    def run(self, args) -> None:
        require_api_key()
        profile = "deep" if getattr(args, "deep", False) else getattr(args, "profile", settings.scan_profile)
        apply_scan_profile(profile)
        runtime_models = self.model_catalog_service.get_runtime_models()
        self.model_catalog_service.apply_runtime_models(runtime_models)
        detailed_cost_report_enabled = settings.cost_reporting_enabled
        if getattr(args, "cost_report", False):
            detailed_cost_report_enabled = True
        if getattr(args, "clean_run", False):
            detailed_cost_report_enabled = False
        repo_path = os.path.abspath(".")

        print(f"\n  Collecting files...")
        all_files = collect_files(repo_path)
        if args.shallow:
            all_files = [path for path in all_files if "/" not in path]
        selected_files, unmatched_targets = select_target_files(all_files, repo_path, args.targets)
        if args.targets:
            print(f"    Targets: {', '.join(args.targets)}")
        if unmatched_targets:
            print(f"    Warning: no matches for {', '.join(unmatched_targets)}")
        all_files = selected_files
        reviewable = filter_reviewable_files(all_files)
        languages = detect_languages(all_files)

        lang_str = ", ".join(languages) if languages else "none detected"
        print(f"    {len(all_files):,} total, {len(reviewable)} reviewable ({lang_str})")
        print(f"    Profile: {settings.scan_profile}")

        if not reviewable:
            print("  No reviewable source files found.")
            return

        if len(reviewable) > settings.scan_max_files:
            print(f"    Warning: {len(reviewable)} files exceeds limit of {settings.scan_max_files}.")
            print("    Use --include to narrow scope.")
            return

        print("    Reading file contents...")
        file_contents = read_file_contents(reviewable, repo_path)
        report_unreadable_files(reviewable, file_contents, debug=settings.scan_debug)

        estimate = estimate_scan(
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
        format_estimate(estimate)

        while True:
            try:
                answer = input("  Proceed? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Aborted.")
                return
            if answer in ("", "y", "yes"):
                break
            if answer in ("n", "no"):
                print("  Aborted.")
                return
            print("  Please enter Y or n.")

        print("  Building code index...")
        code_index = build_code_index(file_contents, languages)
        started_at = time.monotonic()
        comments = settings.scan_comments
        progress_display = ScanProgressDisplay(
            debug=settings.scan_debug,
            total_files=len(file_contents),
            scan_model=runtime_models.strong_model_id,
            profile=settings.scan_profile,
        )
        analysis = self.static_analysis_service.run(
            repo_path=repo_path,
            reviewable_files=reviewable,
            detected_languages=languages,
            on_progress=lambda message: print(f"  {message}"),
        )

        try:
            review = asyncio.run(
                self.review_workflow.run_local_scan(
                    repo_path=repo_path,
                    comments=comments,
                    runtime_models=runtime_models,
                    on_progress=progress_display.emit,
                    file_contents=file_contents,
                    issue_seeds=analysis.prioritized_issue_seeds,
                    code_index=code_index,
                )
            )
        except BaseException as exc:
            progress_display.finish()
            print(f"\n  Fatal scan error: {type(exc).__name__}: {exc}")
            if settings.scan_debug:
                traceback.print_exc()
            return

        progress_display.finish()
        applied_total = 0
        if review.findings:
            print(f"\n  Applying fixes...")
            applied_total = apply_local(review.findings, repo_path)

        elapsed = time.monotonic() - started_at
        cost_report_path = None
        if review.usage and review.usage.api_calls > 0:
            record_run(
                features=estimate.features,
                elapsed_seconds=elapsed,
                usage=review.usage,
                findings_count=len(review.findings),
                applied_count=applied_total,
                get_pricing=get_pricing,
            )
        if detailed_cost_report_enabled and review.usage and review.usage.api_calls > 0:
            cost_report_path = write_run_cost_report(
                repo_path=repo_path,
                estimate=estimate,
                review=review,
                runtime_models=runtime_models,
                elapsed_seconds=elapsed,
                applied_count=applied_total,
                issue_seed_count=len(analysis.prioritized_issue_seeds),
                languages=languages,
            )

        print_review(review, applied=applied_total, elapsed=elapsed, show_usage=True)
        if cost_report_path is not None:
            print(f"  Cost report: {cost_report_path}")
        if applied_total:
            print("  Run `git diff` to see changes.")
