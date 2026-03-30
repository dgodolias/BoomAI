"""Registry helpers for BoomAI prompt packs."""

from __future__ import annotations

from .prompt_packs import PROMPT_PACKS, PromptPack


_PACK_BY_ID = {pack.id: pack for pack in PROMPT_PACKS}


def get_prompt_pack(pack_id: str) -> PromptPack | None:
    """Return a pack by id, or None if it does not exist."""
    return _PACK_BY_ID.get(pack_id)


def resolve_prompt_packs(pack_ids: list[str] | None, stage: str) -> list[PromptPack]:
    """Resolve pack ids for a stage, filtering invalid or incompatible entries."""
    if not pack_ids:
        return []
    resolved: list[PromptPack] = []
    for pack_id in pack_ids:
        pack = get_prompt_pack(pack_id)
        if pack is None or stage not in pack.stages:
            continue
        resolved.append(pack)
    return resolved

