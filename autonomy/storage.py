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


def migrate_legacy_storage(
    workspace: str | Path,
    *,
    legacy_home: str | Path | None = None,
    trash_binary: str = "trash",
) -> dict[str, Any]:
    target_home = workspace_autonomy_home(workspace)
    source_home = Path(legacy_home).expanduser().resolve() if legacy_home else legacy_autonomy_home()
    target_home.mkdir(parents=True, exist_ok=True)
    marker_path = target_home / MIGRATION_MARKER
    if marker_path.exists():
        return json.loads(marker_path.read_text(encoding="utf-8"))

    result: dict[str, Any] = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source_home),
        "target": str(target_home),
        "migrated": [],
        "conflicts": [],
        "trash_unavailable": False,
        "trashed": [],
    }

    if source_home != target_home and source_home.exists():
        for name in LEGACY_STORAGE_ITEMS:
            source = source_home / name
            if not source.exists():
                continue
            destination = target_home / name
            if destination.exists():
                conflict_destination = _next_available_path(
                    target_home / "migration-conflicts" / name
                )
                conflict_destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(conflict_destination))
                result["conflicts"].append(
                    {
                        "source": str(source),
                        "destination": str(conflict_destination),
                    }
                )
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                result["migrated"].append(
                    {
                        "source": str(source),
                        "destination": str(destination),
                    }
                )
        _trash_if_empty(source_home, result, trash_binary)

    marker_path.write_text(
        json.dumps(result, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result


def migrate_legacy_storage_for_cli(workspace: str | Path) -> dict[str, Any] | None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return migrate_legacy_storage(workspace)


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
