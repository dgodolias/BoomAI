from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def walk_files(repo_path: str) -> list[str]:
    skip_dirs = {
        ".git", "node_modules", ".venv", "venv", "__pycache__",
        "dist", "build", ".next", ".nuxt", "target", "bin", "obj",
    }
    result = []
    for root, dirs, filenames in os.walk(repo_path):
        dirs[:] = [directory for directory in dirs if directory not in skip_dirs]
        for name in filenames:
            relative = os.path.relpath(os.path.join(root, name), repo_path)
            result.append(relative.replace("\\", "/"))
    return result


def collect_files(
    repo_path: str = ".",
    exclude: list[str] | None = None,
    include: list[str] | None = None,
) -> list[str]:
    cmd = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
    try:
        result = subprocess.run(cmd, capture_output=True, check=True, cwd=repo_path)
        files = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = walk_files(repo_path)

    files = [path.strip() for path in files if path.strip()]
    if include:
        files = [path for path in files if any(path.startswith(prefix) for prefix in include)]
    if exclude:
        files = [path for path in files if not any(path.startswith(prefix) or path == prefix for prefix in exclude)]
    return files


def normalize_repo_target(target: str, repo_path: str) -> str | None:
    repo_root = Path(repo_path).resolve()
    target_path = Path(target)

    try:
        resolved = target_path.resolve() if target_path.is_absolute() else (repo_root / target_path).resolve()
    except OSError:
        return None

    try:
        relative = resolved.relative_to(repo_root)
    except ValueError:
        return None

    return relative.as_posix().strip("/")


def select_target_files(
    all_files: list[str],
    repo_path: str,
    targets: list[str],
) -> tuple[list[str], list[str]]:
    if not targets:
        return all_files, []

    selected: list[str] = []
    unmatched: list[str] = []
    known_files = set(all_files)

    for raw_target in targets:
        normalized = normalize_repo_target(raw_target, repo_path)
        if normalized is None:
            unmatched.append(raw_target)
            continue

        if normalized == "":
            for path in all_files:
                if path not in selected:
                    selected.append(path)
            continue

        if normalized in known_files:
            if normalized not in selected:
                selected.append(normalized)
            continue

        prefix = normalized.rstrip("/") + "/"
        matches = [path for path in all_files if path.startswith(prefix)]
        if matches:
            for path in matches:
                if path not in selected:
                    selected.append(path)
            continue

        unmatched.append(raw_target)

    return selected, unmatched


def read_file_contents(files: list[str], repo_path: str = ".") -> list[tuple[str, str]]:
    def read_one(filepath: str) -> tuple[str, str] | None:
        full_path = os.path.join(repo_path, filepath)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            if "\x00" in text[:8192]:
                return None
            return filepath, text
        except OSError:
            return None

    max_workers = min(32, max(4, (os.cpu_count() or 8) * 2))
    contents: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for result in executor.map(read_one, files):
            if result is not None:
                contents.append(result)
    return contents


def report_unreadable_files(
    requested_files: list[str],
    file_contents: list[tuple[str, str]],
    *,
    debug: bool = False,
) -> None:
    loaded = {path for path, _ in file_contents}
    missing = [path for path in requested_files if path not in loaded]
    if not missing:
        return

    print(f"    Skipped {len(missing)} unreadable/binary file(s).")
    if debug:
        for path in missing[:10]:
            print(f"      - {path}")
        if len(missing) > 10:
            print(f"      ... and {len(missing) - 10} more")
