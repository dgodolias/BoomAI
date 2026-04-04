from __future__ import annotations

from ...core.config import settings
from ...presentation.settings_menu import print_settings_menu
from ..services.settings_service import (
    format_model_choice,
    mask_api_key,
    refresh_runtime_model_catalog,
    save_setting,
    set_model_role,
)


def pick_role_model(role: str, runtime_models) -> None:
    role_title = role.title()
    candidates = list(runtime_models.strong_candidates if role == "strong" else runtime_models.weak_candidates)
    current_model_id = runtime_models.strong_model_id if role == "strong" else runtime_models.weak_model_id
    current_mode = runtime_models.strong_mode if role == "strong" else runtime_models.weak_mode

    while True:
        print(f"\n  {role_title} model")
        print(f"  {'-' * 36}")
        print(f"  Current: {current_mode.upper()} -> {current_model_id}")
        print("  [0] Reset to AUTO")
        for index, entry in enumerate(candidates, start=1):
            marker = " (current)" if entry.model_id == current_model_id else ""
            print(f"  [{index}] {format_model_choice(entry)}{marker}")
        print()
        choice = input("  Choose model (q to cancel): ").strip().lower()
        if choice in {"", "q"}:
            return
        if choice == "0":
            set_model_role(role, mode="auto")
            print(f"  {role_title} model: AUTO")
            return
        if not choice.isdigit():
            print("  Enter a valid number.")
            continue
        index = int(choice)
        if index < 1 or index > len(candidates):
            print("  Enter a valid number.")
            continue
        selected = candidates[index - 1]
        set_model_role(role, mode="manual", override=selected.model_id)
        print(f"  {role_title} model: {selected.display_name}")
        return


def cmd_settings(_args) -> None:
    while True:
        runtime_models = refresh_runtime_model_catalog()
        print_settings_menu(
            masked_api_key=mask_api_key(settings.google_api_key),
            runtime_models=runtime_models,
            inline_comments_enabled=settings.scan_comments,
            debug_logs_enabled=settings.scan_debug,
            cost_reporting_enabled=settings.cost_reporting_enabled,
        )

        try:
            choice = input("  Enter number to change (q to quit): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice in {"", "q"}:
            break
        if choice == "1":
            new_key = input("  Enter new Gemini API key: ").strip()
            if new_key:
                save_setting("BOOMAI_GOOGLE_API_KEY", new_key)
                settings.google_api_key = new_key
                print("  Key saved.")
                refresh_runtime_model_catalog(force_refresh=True)
        elif choice == "2":
            pick_role_model("strong", runtime_models)
        elif choice == "3":
            pick_role_model("weak", runtime_models)
        elif choice == "4":
            new_val = not settings.scan_comments
            save_setting("BOOMAI_SCAN_COMMENTS", str(new_val).lower())
            settings.scan_comments = new_val
            print(f"  Inline comments: {'ON' if new_val else 'OFF'}")
        elif choice == "5":
            new_val = not settings.scan_debug
            save_setting("BOOMAI_SCAN_DEBUG", str(new_val).lower())
            settings.scan_debug = new_val
            print(f"  Debug logs: {'ON' if new_val else 'OFF'}")
        elif choice == "6":
            new_val = not settings.cost_reporting_enabled
            save_setting("BOOMAI_COST_REPORTING_ENABLED", str(new_val).lower())
            settings.cost_reporting_enabled = new_val
            print(f"  Generate detailed cost report: {'ON' if new_val else 'OFF'}")
        elif choice == "7":
            refreshed = refresh_runtime_model_catalog(force_refresh=True)
            print(
                f"  Refreshed model catalog: "
                f"strong={refreshed.strong_model_id}, weak={refreshed.weak_model_id} "
                f"({refreshed.source.upper()})"
            )
