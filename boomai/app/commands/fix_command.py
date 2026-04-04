from __future__ import annotations

from ..services.fix_workflow import FixWorkflow


def cmd_fix(args) -> None:
    FixWorkflow().run(args)
