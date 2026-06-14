from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MIGRATION_MARKER = "storage-migration.json"
LEGACY_STORAGE_ITEMS = (
    "config.yaml",
    ".env",
    "tools.yaml",
    "autonomy.db",
    "skills",
    "skill-candidates",
)


def workspace_autonomy_home(workspace: str | Path | None = None) -> Path:
    return (Path(workspace) if workspace is not None else Path.cwd()).expanduser().resolve() / ".autonomy"


def legacy_autonomy_home() -> Path:
    return Path.home() / ".autonomy"


def workspace_db_path(workspace: str | Path | None = None) -> Path:
    return workspace_autonomy_home(workspace) / "autonomy.db"



def _next_available_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.name}-{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate conflict path for {path}")


def _trash_if_empty(path: Path, result: dict[str, Any], trash_binary: str) -> None:
    try:
        if not path.is_dir() or any(path.iterdir()):
            return
    except OSError:
        return
    trash = shutil.which(trash_binary)
    if not trash:
        result["trash_unavailable"] = True
        return
    completed = subprocess.run(
        [trash, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        result["trashed"].append(str(path))
    else:
        result["trash_unavailable"] = True
