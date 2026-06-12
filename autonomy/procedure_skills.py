from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .bundled_procedure_skills import BUNDLED_PROCEDURE_SKILLS
from .models import ProcedureSkill, ProcedureSkillDraft, ProcedureSkillSummary
from .store import AutonomyStore


class ProcedureSkillError(ValueError):
    pass


class ProcedureSkillLibrary:
    """Discover governed SKILL.md procedure knowledge with progressive disclosure."""

    NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
    MAX_CONTENT_CHARS = 50_000

    def __init__(
        self,
        workspace: str | Path,
        store: AutonomyStore,
        skills_dir: str | Path | None = None,
        candidates_dir: str | Path | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        autonomy_home = Path.home() / ".autonomy"
        self.skills_dir = Path(skills_dir or autonomy_home / "skills").expanduser().resolve()
        self.candidates_dir = Path(
            candidates_dir or autonomy_home / "skill-candidates"
        ).expanduser().resolve()
        self.store = store

    def index(
        self,
        available_tools: set[str],
        *,
        include_disabled: bool = False,
    ) -> list[ProcedureSkillSummary]:
        summaries: list[ProcedureSkillSummary] = []
        current_platform = self._current_platform()
        if not self.skills_dir.is_dir():
            return []
        for skill_file in sorted(self.skills_dir.rglob("SKILL.md")):
            skill = self._read_skill(skill_file, self.skills_dir, "global")
            summary = self.store.sync_procedure_skill(skill.summary)
            if not include_disabled and not summary.enabled:
                continue
            if summary.platforms and current_platform not in summary.platforms:
                continue
            if not set(summary.requires_tools).issubset(available_tools):
                continue
            summaries.append(summary)
        return sorted(summaries, key=lambda item: item.name)

    def load_selected(
        self,
        names: list[str],
        available_tools: set[str],
    ) -> list[ProcedureSkill]:
        allowed = {item.name: item for item in self.index(available_tools)}
        selected: list[ProcedureSkill] = []
        for name in names[:3]:
            if name not in allowed or any(item.summary.name == name for item in selected):
                continue
            summary = allowed[name]
            skill = self._read_skill(Path(summary.path), self.skills_dir, "global")
            selected.append(skill)
            self.store.record_procedure_skill_loaded(name)
        return selected

    def view(self, name: str, available_tools: set[str]) -> ProcedureSkill:
        loaded = self.load_selected([name], available_tools)
        if not loaded:
            raise KeyError(f"unknown or unavailable procedure skill: {name}")
        return loaded[0]

    def list_all(self, *, include_disabled: bool = False) -> list[ProcedureSkill]:
        if not self.skills_dir.is_dir():
            return []
        skills: list[ProcedureSkill] = []
        for skill_file in sorted(self.skills_dir.rglob("SKILL.md")):
            skill = self._read_skill(skill_file, self.skills_dir, "global")
            summary = self.store.sync_procedure_skill(skill.summary)
            if include_disabled or summary.enabled:
                skills.append(
                    ProcedureSkill(
                        summary=summary,
                        body=skill.body,
                        raw_content=skill.raw_content,
                    )
                )
        return sorted(skills, key=lambda item: item.summary.name)

    def install_bundled(
        self,
        names: list[str] | None = None,
    ) -> list[ProcedureSkillSummary]:
        selected_names = names or sorted(BUNDLED_PROCEDURE_SKILLS)
        installed: list[ProcedureSkillSummary] = []
        unknown = sorted(set(selected_names) - set(BUNDLED_PROCEDURE_SKILLS))
        if unknown:
            raise ProcedureSkillError("unknown bundled procedure skill: " + ", ".join(unknown))
        for name in selected_names:
            content = BUNDLED_PROCEDURE_SKILLS[name]
            skill = self._parse_content(
                content,
                source="global",
                path=self.skills_dir / name / "SKILL.md",
            )
            destination = self.skills_dir / skill.summary.name / "SKILL.md"
            if destination.exists():
                raise FileExistsError(f"procedure skill already exists: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=False)
            destination.write_text(content, encoding="utf-8")
            approved = self._read_skill(destination, self.skills_dir, "global")
            self.store.sync_procedure_skill(approved.summary)
            installed.append(approved.summary)
        return installed

    def write_candidate(
        self,
        draft: ProcedureSkillDraft,
        *,
        source_run_id: str = "",
        source_workspace: str | Path | None = None,
        proposal_type: str = "new_skill",
        reason: str = "",
        confidence: float = 1.0,
    ) -> dict[str, str]:
        content = self.render_draft(draft)
        self._parse_content(content, source="candidate", path=Path("SKILL.md"))
        candidate_id = uuid.uuid4().hex
        target_dir = self.candidates_dir / candidate_id
        target_dir.mkdir(parents=True, exist_ok=False)
        target = target_dir / "SKILL.md"
        target.write_text(content, encoding="utf-8")
        metadata = {
            "candidate_id": candidate_id,
            "name": draft.name,
            "source_run_id": source_run_id,
            "source_workspace": str(
                Path(source_workspace).resolve() if source_workspace else self.workspace
            ),
            "proposal_type": proposal_type,
            "reason": reason,
            "confidence": str(max(0.0, min(float(confidence), 1.0))),
            "status": "candidate",
            "created_at": self._utc_now(),
            "path": str(target),
        }
        self._write_candidate_metadata(candidate_id, metadata)
        return metadata

    def list_candidates(self) -> list[dict[str, str]]:
        if not self.candidates_dir.is_dir():
            return []
        candidates: list[dict[str, str]] = []
        for skill_file in sorted(self.candidates_dir.glob("*/SKILL.md")):
            try:
                metadata = self._candidate_metadata(skill_file.parent.name)
                if metadata.get("status", "candidate") != "candidate":
                    continue
                skill = self._parse_content(
                    skill_file.read_text(encoding="utf-8"),
                    source="candidate",
                    path=skill_file,
                )
                candidates.append(
                    metadata
                    | {
                        "candidate_id": skill_file.parent.name,
                        "name": skill.summary.name,
                        "description": skill.summary.description,
                        "path": str(skill_file),
                    }
                )
            except ProcedureSkillError:
                continue
        return candidates

    def view_candidate(self, candidate_id: str) -> ProcedureSkill:
        source = self._candidate_skill_path(candidate_id)
        skill = self._parse_content(
            source.read_text(encoding="utf-8"),
            source="candidate",
            path=source,
        )
        return skill

    def approve_candidate(self, candidate_id: str) -> ProcedureSkill:
        source = self._candidate_skill_path(candidate_id)
        skill = self.view_candidate(candidate_id)
        destination = self.skills_dir / skill.summary.name / "SKILL.md"
        if destination.exists():
            raise FileExistsError(f"procedure skill already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(source, destination)
        approved = self._read_skill(destination, self.skills_dir, "global")
        self.store.sync_procedure_skill(approved.summary)
        metadata = self._candidate_metadata(candidate_id)
        metadata.update(
            {
                "status": "approved",
                "approved_at": self._utc_now(),
                "approved_path": str(destination),
            }
        )
        self._write_candidate_metadata(candidate_id, metadata)
        self._record_candidate_status_event(
            metadata,
            "procedure_skill_candidate_approved",
        )
        return approved

    def reject_candidate(self, candidate_id: str) -> dict[str, str]:
        self._candidate_skill_path(candidate_id)
        metadata = self._candidate_metadata(candidate_id)
        metadata.update({"status": "rejected", "rejected_at": self._utc_now()})
        self._write_candidate_metadata(candidate_id, metadata)
        self._record_candidate_status_event(
            metadata,
            "procedure_skill_candidate_rejected",
        )
        return metadata

    def disable(self, name: str, available_tools: set[str]) -> None:
        known = {item.name for item in self.index(available_tools, include_disabled=True)}
        if name not in known:
            raise KeyError(f"unknown procedure skill: {name}")
        self.store.set_procedure_skill_enabled(name, False)

    def delete_skill(self, name: str) -> None:
        skill_dir = self._skill_dir(name)
        skill_path = skill_dir / "SKILL.md"
        if not skill_path.is_file():
            raise KeyError(f"unknown procedure skill: {name}")
        self._read_skill(skill_path, self.skills_dir, "global")
        shutil.rmtree(skill_dir)
        self.store.delete_procedure_skill_record(name)

    def merge_skill(
        self,
        source_name: str,
        target_name: str,
        merged_content: str,
    ) -> ProcedureSkill:
        if source_name == target_name:
            raise ProcedureSkillError("source and target skill must differ")
        source_path = self._skill_dir(source_name) / "SKILL.md"
        target_path = self._skill_dir(target_name) / "SKILL.md"
        if not source_path.is_file():
            raise KeyError(f"unknown source procedure skill: {source_name}")
        if not target_path.is_file():
            raise KeyError(f"unknown target procedure skill: {target_name}")
        source = self._read_skill(source_path, self.skills_dir, "global")
        target = self._read_skill(target_path, self.skills_dir, "global")
        merged = self._parse_content(
            merged_content,
            source="global",
            path=target_path,
        )
        if merged.summary.name != target.summary.name:
            raise ProcedureSkillError("merged skill name must match target skill")
        if not set(merged.summary.requires_tools).issubset(set(target.summary.requires_tools)):
            raise ProcedureSkillError("merged skill must not increase required tools")
        if target.summary.platforms and not set(merged.summary.platforms).issubset(
            set(target.summary.platforms)
        ):
            raise ProcedureSkillError("merged skill must not expand platforms")
        del source
        target_path.write_text(merged.raw_content, encoding="utf-8")
        approved = self._read_skill(target_path, self.skills_dir, "global")
        self.store.sync_procedure_skill(approved.summary)
        self.delete_skill(source_name)
        return approved

    def merge_skill_preview(self, merged_content: str, target_name: str) -> ProcedureSkill:
        target_path = self._skill_dir(target_name) / "SKILL.md"
        merged = self._parse_content(
            merged_content,
            source="global",
            path=target_path,
        )
        if merged.summary.name != target_name:
            raise ProcedureSkillError("merged skill name must match target skill")
        return merged

    @classmethod
    def render_draft(cls, draft: ProcedureSkillDraft) -> str:
        frontmatter = {
            "name": draft.name,
            "description": draft.description,
            "version": draft.version,
            "tags": list(draft.tags),
            "platforms": list(draft.platforms),
            "requires_tools": list(draft.requires_tools),
        }
        return (
            "---\n"
            + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=False).strip()
            + "\n---\n\n"
            + draft.body.strip()
            + "\n"
        )

    def _read_skill(self, path: Path, root: Path, source: str) -> ProcedureSkill:
        resolved = path.resolve()
        if root.resolve() not in resolved.parents:
            raise ProcedureSkillError(f"skill path escapes source root: {path}")
        return self._parse_content(
            resolved.read_text(encoding="utf-8"),
            source=source,
            path=resolved,
        )

    def _candidate_skill_path(self, candidate_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", candidate_id):
            raise ProcedureSkillError("invalid candidate id")
        candidate_dir = (self.candidates_dir / candidate_id).resolve()
        if self.candidates_dir.resolve() not in candidate_dir.parents:
            raise ProcedureSkillError("candidate path escapes candidate directory")
        source = candidate_dir / "SKILL.md"
        if not source.is_file():
            raise KeyError(f"unknown procedure skill candidate: {candidate_id}")
        return source

    def _candidate_metadata(self, candidate_id: str) -> dict[str, str]:
        metadata_path = self.candidates_dir / candidate_id / "candidate.json"
        if metadata_path.is_file():
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(key): str(value) for key, value in raw.items()}
        source = self._candidate_skill_path(candidate_id)
        skill = self._parse_content(
            source.read_text(encoding="utf-8"),
            source="candidate",
            path=source,
        )
        return {
            "candidate_id": candidate_id,
            "name": skill.summary.name,
            "source_run_id": "",
            "source_workspace": str(self.workspace),
            "proposal_type": "new_skill",
            "reason": "",
            "confidence": "1.0",
            "status": "candidate",
            "created_at": "",
            "path": str(source),
        }

    def _write_candidate_metadata(self, candidate_id: str, metadata: dict[str, str]) -> None:
        metadata_path = self.candidates_dir / candidate_id / "candidate.json"
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _record_candidate_status_event(self, metadata: dict[str, str], event_type: str) -> None:
        source_run_id = metadata.get("source_run_id", "")
        if source_run_id:
            try:
                self.store.record_event(source_run_id, 0, event_type, metadata)
            except Exception:
                pass

    def _skill_dir(self, name: str) -> Path:
        if not self.NAME_RE.fullmatch(name):
            raise ProcedureSkillError(f"invalid procedure skill name: {name}")
        skill_dir = (self.skills_dir / name).resolve()
        if self.skills_dir.resolve() not in skill_dir.parents:
            raise ProcedureSkillError("skill path escapes skill directory")
        return skill_dir

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _parse_content(self, content: str, source: str, path: Path) -> ProcedureSkill:
        if len(content) > self.MAX_CONTENT_CHARS:
            raise ProcedureSkillError(f"SKILL.md exceeds {self.MAX_CONTENT_CHARS} characters")
        if not content.startswith("---\n"):
            raise ProcedureSkillError("SKILL.md must start with YAML frontmatter")
        try:
            _, frontmatter_text, body = content.split("---", 2)
            frontmatter = yaml.safe_load(frontmatter_text)
        except (ValueError, yaml.YAMLError) as exc:
            raise ProcedureSkillError(f"invalid SKILL.md frontmatter: {exc}") from exc
        if not isinstance(frontmatter, dict):
            raise ProcedureSkillError("SKILL.md frontmatter must be a mapping")
        name = self._required_string(frontmatter, "name")
        if not self.NAME_RE.fullmatch(name):
            raise ProcedureSkillError(f"invalid procedure skill name: {name}")
        description = self._required_string(frontmatter, "description")
        version = self._required_string(frontmatter, "version")
        tags = self._string_tuple(frontmatter, "tags")
        platforms = self._string_tuple(frontmatter, "platforms")
        requires_tools = self._string_tuple(frontmatter, "requires_tools")
        if not body.strip():
            raise ProcedureSkillError("SKILL.md body must not be empty")
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        summary = ProcedureSkillSummary(
            name=name,
            description=description,
            version=version,
            tags=tags,
            platforms=platforms,
            requires_tools=requires_tools,
            source=source,
            path=str(path),
            file_hash=file_hash,
        )
        return ProcedureSkill(summary=summary, body=body.strip(), raw_content=content)

    @staticmethod
    def _required_string(frontmatter: dict, name: str) -> str:
        value = frontmatter.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ProcedureSkillError(f"frontmatter field {name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _string_tuple(frontmatter: dict, name: str) -> tuple[str, ...]:
        value = frontmatter.get(name, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ProcedureSkillError(f"frontmatter field {name} must be an array of strings")
        return tuple(item.strip() for item in value if item.strip())

    @staticmethod
    def _current_platform() -> str:
        if sys.platform == "darwin":
            return "macos"
        if sys.platform.startswith("win"):
            return "windows"
        return "linux"
