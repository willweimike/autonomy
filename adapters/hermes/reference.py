from __future__ import annotations

from pathlib import Path
from typing import List


class HermesReferenceAdapter:
    """Read-only adapter for a local Hermes Agent reference tree."""

    def __init__(self, hermes_root: str):
        self.hermes_root = Path(hermes_root).resolve()

    def is_available(self) -> bool:
        return (
            self.hermes_root.exists()
            and (self.hermes_root / "tools").is_dir()
            and (self.hermes_root / "skills").is_dir()
            and (self.hermes_root / "toolsets.py").is_file()
        )

    def list_skill_names(self) -> List[str]:
        names = []
        for skill_file in sorted((self.hermes_root / "skills").glob("**/SKILL.md")):
            names.append(skill_file.parent.name)
        return names

    def read_reference_file(self, relative_path: str) -> str:
        path = (self.hermes_root / relative_path).resolve()
        if self.hermes_root not in path.parents and path != self.hermes_root:
            raise ValueError("relative_path escapes hermes_root")
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return path.read_text(encoding="utf-8")
