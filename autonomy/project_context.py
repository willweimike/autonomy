from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MAX_PROJECT_CONTEXT_CHARS = 20_000
PROJECT_CONTEXT_CANDIDATES = (
    "AUTONOMY.md",
    ".autonomy.md",
    "AGENTS.md",
    "agents.md",
    ".cursorrules",
)


@dataclass(frozen=True)
class ProjectContext:
    source: str
    content: str
    original_chars: int
    truncated: bool = False


def _truncate_context(content: str, source: str, max_chars: int) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False
    marker = f"\n[...truncated {source}...]\n"
    content_budget = max(0, max_chars - len(marker))
    if content_budget <= 0:
        return marker[:max_chars], True
    head_chars = max(1, int(content_budget * 0.7))
    tail_chars = max(0, content_budget - head_chars)
    tail = content[-tail_chars:] if tail_chars else ""
    return content[:head_chars] + marker + tail, True


def load_project_context(
    workspace: str | Path,
    *,
    max_chars: int = MAX_PROJECT_CONTEXT_CHARS,
) -> ProjectContext | None:
    """Load the first workspace project guidance file, if present.

    This is planning context only. It does not grant tool permissions or bypass
    ActionGateway execution governance.
    """
    root = Path(workspace).resolve()
    for name in PROJECT_CONTEXT_CANDIDATES:
        path = root / name
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not raw:
            continue
        content = f"## {name}\n\n{raw}"
        truncated_content, truncated = _truncate_context(content, name, max_chars)
        return ProjectContext(
            source=name,
            content=truncated_content,
            original_chars=len(content),
            truncated=truncated,
        )
    return None
