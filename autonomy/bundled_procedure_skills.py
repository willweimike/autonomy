from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


BUNDLED_SKILLS_DIR = Path(__file__).with_name("bundled_skills")


def _frontmatter(content: str, path: Path) -> dict[str, Any]:
    if not content.startswith("---\n"):
        raise ValueError(f"bundled procedure skill is missing YAML frontmatter: {path}")
    try:
        _, frontmatter, _ = content.split("---", 2)
    except ValueError as exc:
        raise ValueError(f"invalid bundled procedure skill frontmatter: {path}") from exc
    metadata = yaml.safe_load(frontmatter) or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"invalid bundled procedure skill metadata: {path}")
    return metadata


def _load_bundled_procedure_skills(skills_dir: Path = BUNDLED_SKILLS_DIR) -> dict[str, str]:
    skills: dict[str, str] = {}
    if not skills_dir.is_dir():
        return skills
    for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
        content = skill_file.read_text(encoding="utf-8")
        metadata = _frontmatter(content, skill_file)
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"bundled procedure skill metadata missing name: {skill_file}")
        directory_name = skill_file.parent.name
        if name != directory_name:
            raise ValueError(
                "bundled procedure skill directory name must match frontmatter name: "
                f"{skill_file} has name {name!r}"
            )
        if name in skills:
            raise ValueError(f"duplicate bundled procedure skill: {name}")
        skills[name] = content
    return skills


BUNDLED_PROCEDURE_SKILLS: dict[str, str] = _load_bundled_procedure_skills()


def bundled_skill_names() -> tuple[str, ...]:
    return tuple(sorted(BUNDLED_PROCEDURE_SKILLS))
