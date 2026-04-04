from __future__ import annotations

from ...core.config import settings


def apply_scan_profile(profile: str) -> None:
    base = {
        "max_scan_chars": settings.max_scan_chars,
        "scan_max_files_per_chunk": settings.scan_max_files_per_chunk,
        "patch_max_findings_per_chunk": settings.patch_max_findings_per_chunk,
        "prompt_pack_scan_max_extras": settings.prompt_pack_scan_max_extras,
        "prompt_pack_fix_max_extras": settings.prompt_pack_fix_max_extras,
    }
    deep = {
        "max_scan_chars": settings.deep_max_scan_chars,
        "scan_max_files_per_chunk": settings.deep_scan_max_files_per_chunk,
        "patch_max_findings_per_chunk": settings.deep_patch_max_findings_per_chunk,
        "prompt_pack_scan_max_extras": settings.deep_prompt_pack_scan_max_extras,
        "prompt_pack_fix_max_extras": settings.deep_prompt_pack_fix_max_extras,
    }
    selected = deep if profile == "deep" else base
    for key, value in selected.items():
        setattr(settings, key, value)
    settings.scan_profile = profile
