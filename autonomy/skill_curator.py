from __future__ import annotations

import re
import threading
import uuid

from .models import (
    LearningProposal,
    LearningProposalStatus,
    LearningProposalType,
    ProcedureSkill,
)
from .procedure_skills import ProcedureSkillLibrary
from .store import AutonomyStore


class CuratorDaemon:
    """Best-effort background runner for SkillCurator maintenance."""

    def __init__(self, curator: "SkillCurator"):
        self.curator = curator

    def trigger_after_run(self, run_id: str) -> None:
        thread = threading.Thread(
            target=self._run_once,
            args=(run_id,),
            daemon=True,
            name="autonomy-skill-curator",
        )
        thread.start()

    def _run_once(self, run_id: str) -> None:
        try:
            results = self.curator.apply_auto_merges()
            self.curator.store.record_curator_event(
                "curator_daemon_run",
                reason="run_finished",
                payload={
                    "run_id": run_id,
                    "merge_count": len(results),
                    "merges": results,
                },
            )
        except Exception as exc:
            try:
                self.curator.store.record_curator_event(
                    "curator_daemon_error",
                    reason="run_finished",
                    payload={"run_id": run_id, "error": str(exc)},
                )
            except Exception:
                pass


class SkillCurator:
    """Consolidate clear duplicate and subcase skills without prompt-visible lineage."""

    def __init__(self, library: ProcedureSkillLibrary, store: AutonomyStore):
        self.library = library
        self.store = store

    def review_library(self) -> list[LearningProposal]:
        proposals: list[LearningProposal] = []
        for item in self.merge_candidates():
            proposal = LearningProposal(
                id=uuid.uuid4().hex,
                proposal_type=LearningProposalType.MERGE_SKILLS,
                source_run_id="",
                status=LearningProposalStatus.CANDIDATE,
                reason=item["reason"],
                confidence=float(item["confidence"]),
                payload=item,
            )
            self.store.record_learning_proposal(proposal)
            proposals.append(proposal)
        return proposals

    def apply_auto_merges(self) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        used_sources: set[str] = set()
        for item in self.merge_candidates():
            source_name = item["source_skill"]
            target_name = item["target_skill"]
            if source_name in used_sources:
                continue
            try:
                source, target = self._skill_pair(source_name, target_name)
            except KeyError:
                continue
            if not self._can_auto_merge(source, target):
                continue
            merged = self.library.merge_skill(
                source_name,
                target_name,
                target.raw_content,
            )
            result = {
                "event_type": "curator_merge",
                "source_skill": source_name,
                "target_skill": target_name,
                "reason": item["reason"],
                "merged_skill": merged.summary.name,
            }
            self.store.record_curator_event(
                "curator_merge",
                source_skill=source_name,
                target_skill=target_name,
                reason=item["reason"],
                payload=result,
            )
            self.store.record_learning_proposal(
                LearningProposal(
                    id=uuid.uuid4().hex,
                    proposal_type=LearningProposalType.MERGE_SKILLS,
                    source_run_id="",
                    status=LearningProposalStatus.APPLIED,
                    reason=item["reason"],
                    confidence=float(item["confidence"]),
                    payload=result,
                )
            )
            used_sources.add(source_name)
            results.append(result)
        return results

    def status(self) -> dict[str, object]:
        return {
            "merge_candidates": self.merge_candidates(),
            "recent_events": self.store.list_curator_events(limit=10),
        }

    def merge_candidates(self) -> list[dict[str, str]]:
        skills = self.library.list_all(include_disabled=True)
        candidates: list[dict[str, str]] = []
        for source in skills:
            if not source.summary.enabled:
                continue
            best = self._best_target(source, skills)
            if best is None:
                continue
            target, reason, confidence = best
            candidates.append(
                {
                    "source_skill": source.summary.name,
                    "target_skill": target.summary.name,
                    "reason": reason,
                    "confidence": f"{confidence:.2f}",
                }
            )
        return candidates

    def _best_target(
        self,
        source: ProcedureSkill,
        skills: list[ProcedureSkill],
    ) -> tuple[ProcedureSkill, str, float] | None:
        matches: list[tuple[ProcedureSkill, str, float]] = []
        for target in skills:
            if source.summary.name == target.summary.name or not target.summary.enabled:
                continue
            if not self._can_auto_merge(source, target):
                continue
            source_body = self._normalized(source.body)
            target_body = self._normalized(target.body)
            if (
                source_body == target_body
                and target.summary.name < source.summary.name
            ):
                matches.append((target, "duplicate skill content", 0.95))
            elif source_body != target_body and source_body in target_body:
                matches.append((target, "source skill is a subcase already covered by target", 0.9))
        if not matches:
            return None
        return sorted(
            matches,
            key=lambda item: (-item[2], -len(item[0].body), item[0].summary.name),
        )[0]

    def _can_auto_merge(self, source: ProcedureSkill, target: ProcedureSkill) -> bool:
        source_tools = set(source.summary.requires_tools)
        target_tools = set(target.summary.requires_tools)
        if not source_tools.issubset(target_tools):
            return False
        source_platforms = set(source.summary.platforms)
        target_platforms = set(target.summary.platforms)
        if source_platforms and target_platforms and not source_platforms.issubset(target_platforms):
            return False
        if bool(source_platforms) != bool(target_platforms):
            return False
        try:
            merged = self.library.merge_skill_preview(target.raw_content, target.summary.name)
        except Exception:
            return False
        return set(merged.summary.requires_tools).issubset(target_tools)

    def _skill_pair(self, source_name: str, target_name: str) -> tuple[ProcedureSkill, ProcedureSkill]:
        skills = {skill.summary.name: skill for skill in self.library.list_all(include_disabled=True)}
        try:
            return skills[source_name], skills[target_name]
        except KeyError as exc:
            raise KeyError(f"unknown curator skill pair: {source_name}, {target_name}") from exc

    @staticmethod
    def _normalized(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().lower())
