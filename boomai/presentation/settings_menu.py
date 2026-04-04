from __future__ import annotations


def print_settings_menu(
    *,
    masked_api_key: str,
    runtime_models,
    inline_comments_enabled: bool,
    debug_logs_enabled: bool,
    cost_reporting_enabled: bool,
) -> None:
    comments_str = "ON" if inline_comments_enabled else "OFF"
    debug_str = "ON" if debug_logs_enabled else "OFF"
    reporting_str = "ON" if cost_reporting_enabled else "OFF"

    print(f"\n  BoomAI Settings")
    print(f"  {'=' * 54}")
    print(f"  Catalog source: {runtime_models.source.upper()}")
    if runtime_models.catalog_error:
        print(f"  Note: using {runtime_models.source} catalog after refresh error.")
    print(f"  [1] Gemini API Key                    {masked_api_key}")
    print(
        f"  [2] Strong model ({runtime_models.strong_mode.upper()})"
        f"          {runtime_models.strong_display_name} [{runtime_models.strong_model_id}]"
    )
    print(
        f"  [3] Weak model ({runtime_models.weak_mode.upper()})"
        f"            {runtime_models.weak_display_name} [{runtime_models.weak_model_id}]"
    )
    print(f"  [4] Inline comments                  {comments_str}")
    print(f"  [5] Debug logs                       {debug_str}")
    print(f"  [6] Generate detailed cost report    {reporting_str}")
    print(f"  [7] Refresh model catalog now")
    print()
