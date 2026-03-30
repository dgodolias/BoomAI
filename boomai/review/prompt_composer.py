"""Prompt composition helpers for BoomAI prompt packs."""

from __future__ import annotations

from .pack_registry import resolve_prompt_packs


def _render_pack_section(pack_ids: list[str] | None, stage: str) -> str:
    packs = resolve_prompt_packs(pack_ids, stage)
    if not packs:
        return ""

    lines = ["", "## Domain Guidance"]
    for pack in packs:
        lines.append("")
        lines.append(f"### Pack: {pack.id}@{pack.version} - {pack.title}")
        lines.append(f"Summary: {pack.summary}")
        if stage == "scan" and pack.review_focus:
            lines.append("Review focus:")
            for bullet in pack.review_focus:
                lines.append(f"- {bullet}")
        if stage == "fix" and pack.fix_focus:
            lines.append("Fix focus:")
            for bullet in pack.fix_focus:
                lines.append(f"- {bullet}")
        if pack.avoid:
            lines.append("Avoid:")
            for bullet in pack.avoid:
                lines.append(f"- {bullet}")
    return "\n".join(lines)


def append_scan_pack_guidance(base_prompt: str, pack_ids: list[str] | None) -> str:
    """Append selected scan packs to the base system prompt."""
    return f"{base_prompt}{_render_pack_section(pack_ids, 'scan')}"


def append_fix_pack_guidance(base_prompt: str, pack_ids: list[str] | None) -> str:
    """Append selected fix packs to the base system prompt."""
    return f"{base_prompt}{_render_pack_section(pack_ids, 'fix')}"
