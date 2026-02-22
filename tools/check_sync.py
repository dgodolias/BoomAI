"""Verify shared Python files haven't drifted back into data/scripts/.

Shared files live ONLY in boomai/ (canonical source). The setup script
copies them to target repos at deploy time.  If someone accidentally
recreates a copy in data/scripts/, this check catches it.

Usage:
    python tools/check_sync.py
"""

import sys
from pathlib import Path

SHARED = [
    "models.py",
    "languages.py",
    "prompts.py",
    "github_client.py",
    "slack_notifier.py",
    "apply_fixes.py",
]

root = Path(__file__).resolve().parent.parent
data_scripts = root / "boomai" / "data" / "scripts"

errors = []
for f in SHARED:
    if (data_scripts / f).exists():
        errors.append(
            f"  {f} exists in boomai/data/scripts/ — "
            f"should be deleted (canonical copy is boomai/{f})"
        )

if errors:
    print("Sync check FAILED — stale copies found:", file=sys.stderr)
    for e in errors:
        print(e, file=sys.stderr)
    sys.exit(1)

print("OK: no stale copies in data/scripts/")
