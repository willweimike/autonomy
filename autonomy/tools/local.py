from __future__ import annotations

import ast
import difflib
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..models import Observation, RiskLevel
from ..storage import workspace_db_path
from ..store import AutonomyStore
from ..toolsets import ToolsetConfiguration
from .registry import ToolRegistry
from .redaction import redact_sensitive_text
from .toolsets.browser import BrowserController, register_browser_tools
from .toolsets.database import register_database_tools
from .toolsets.delegate import DelegateRunner, register_delegate_tools
from .toolsets.mcp import register_mcp_tools
from .toolsets.process import ProcessManager, register_process_tools
from .toolsets.project import register_project_tools


_BINARY_LIKE_EXTENSIONS = {
    ".7z",
    ".avif",
    ".bin",
    ".bmp",
    ".class",
    ".dmg",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".tar",
    ".webp",
    ".zip",
}

_DEFAULT_READ_LIMIT = 500
_MAX_READ_LIMIT = 2000
_DEFAULT_READ_MANY_LIMIT = 200
_MAX_READ_MANY_FILES = 12
_DEFAULT_READ_MANY_CHARS = 50_000
_MAX_READ_MANY_CHARS = 100_000
_MAX_STAT_MANY_PATHS = 50
_MAX_RAW_READ_CHARS = 100_000
_MAX_READ_LINE_CHARS = 2000
_DEFAULT_LIST_LIMIT = 500
_MAX_LIST_LIMIT = 2000
_DEFAULT_TREE_DEPTH = 3
_MAX_TREE_DEPTH = 8
_DEFAULT_TREE_ENTRIES = 200
_MAX_TREE_ENTRIES = 1000
_DEFAULT_SEARCH_TEXT_LIMIT = 100
_DEFAULT_SEARCH_FILES_LIMIT = 50
_DEFAULT_OUTLINE_LIMIT = 200
_DEFAULT_SYNTAX_LIMIT = 200
_MAX_SEARCH_LIMIT = 500
_MAX_OUTLINE_LIMIT = 1000
_MAX_SYNTAX_LIMIT = 1000
_MAX_SEARCH_CONTEXT_LINES = 10
_DEFAULT_SHELL_OUTPUT_CHARS = 50_000
_MAX_SHELL_OUTPUT_CHARS = 200_000
_DEFAULT_DIFF_OUTPUT_CHARS = 50_000
_MAX_DIFF_OUTPUT_CHARS = 200_000
_MAX_DIFF_PATHS = 100
_MEMORY_SCOPES = {"user", "project", "workspace"}
_DEFAULT_MEMORY_LIMIT = 10
_MAX_MEMORY_LIMIT = 100
_SIMILAR_PATH_SCAN_LIMIT = 20_000
_SKIPPED_SUGGESTION_DIRS = {
    ".autonomy",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}
_SKIPPED_TREE_DIRS = set(_SKIPPED_SUGGESTION_DIRS)
_SECRET_ENV_FILENAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    ".env.staging",
    ".envrc",
}


def _resolve_inside(root: Path, raw_path: str) -> Path:
    if not str(raw_path).strip():
        raise ValueError("path must not be empty")
    path = Path(raw_path)
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path escapes workspace: {raw_path}")
    return resolved


def _relative_path_text(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _similar_workspace_paths(
    root: Path,
    raw_path: str,
    *,
    kind: str,
    limit: int = 5,
) -> list[str]:
    query_path = Path(raw_path)
    query_name = query_path.name.lower()
    query_stem = query_path.stem.lower()
    query_text = str(query_path).strip().lower()
    if not query_text:
        return []
    scored: list[tuple[float, str]] = []
    scanned = 0
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            dirname for dirname in dirnames if dirname not in _SKIPPED_SUGGESTION_DIRS
        )
        filenames = sorted(filenames)
        base = Path(current_root)
        candidates: list[Path] = []
        if kind in {"dir", "any"}:
            candidates.extend(base / dirname for dirname in dirnames)
        if kind in {"file", "any"}:
            candidates.extend(base / filename for filename in filenames)
        for candidate in candidates:
            scanned += 1
            if scanned > _SIMILAR_PATH_SCAN_LIMIT:
                break
            relative = _relative_path_text(root, candidate)
            lower_relative = relative.lower()
            lower_name = candidate.name.lower()
            score = 0.0
            if lower_relative == query_text or lower_name == query_name:
                score = 1.0
            elif query_stem and candidate.stem.lower() == query_stem:
                score = 0.92
            elif query_name and lower_name.startswith(query_name):
                score = 0.8
            elif query_name and query_name in lower_name:
                score = 0.72
            elif lower_name and lower_name in query_name and len(lower_name) > 2:
                score = 0.55
            else:
                score = max(
                    difflib.SequenceMatcher(None, query_text, lower_relative).ratio(),
                    difflib.SequenceMatcher(None, query_name, lower_name).ratio(),
                )
            if score >= 0.45:
                scored.append((score, relative))
        if scanned > _SIMILAR_PATH_SCAN_LIMIT:
            break
    return [
        relative
        for _, relative in sorted(scored, key=lambda item: (-item[0], len(item[1]), item[1]))[
            :limit
        ]
    ]


def _missing_path_error(root: Path, raw_path: str, message: str, *, kind: str) -> str:
    suggestions = _similar_workspace_paths(root, raw_path, kind=kind)
    if not suggestions:
        return message
    formatted = "\n".join(f"- {path}" for path in suggestions)
    return f"{message}\nSimilar paths:\n{formatted}"


def _is_binary_like_path(path: Path) -> bool:
    return path.suffix.lower() in _BINARY_LIKE_EXTENSIONS


def _validate_text_file_path(path: Path) -> None:
    if _is_binary_like_path(path):
        raise ValueError(f"binary-like file extension is not supported: {path.suffix}")


def _is_secret_env_path(path: Path) -> bool:
    return path.name.lower() in _SECRET_ENV_FILENAMES


def _file_revision(path: Path) -> str:
    if not path.exists():
        return "missing"
    stat_result = path.stat()
    return f"{stat_result.st_mtime_ns}:{stat_result.st_size}"


def _secret_env_error(path: Path) -> str:
    return (
        f"access denied: {path} is a secret-bearing environment file. "
        "Read .env.example instead when you need configuration structure."
    )


def _validate_readable_text_path(path: Path) -> None:
    _validate_text_file_path(path)
    if _is_secret_env_path(path):
        raise ValueError(_secret_env_error(path))


def _validate_writable_text_path(path: Path) -> None:
    _validate_text_file_path(path)
    if _is_secret_env_path(path):
        raise ValueError(_secret_env_error(path))


def _validate_trashable_path(root: Path, path: Path) -> None:
    if path == root:
        raise ValueError("cannot trash the workspace root")
    relative_parts = path.relative_to(root).parts
    protected = {".git", ".autonomy"}
    if any(part in protected for part in relative_parts):
        raise ValueError("cannot trash protected workspace metadata paths")
    if _is_secret_env_path(path):
        raise ValueError(_secret_env_error(path))
    if not path.exists():
        raise ValueError(f"path does not exist: {_relative_path_text(root, path)}")


def _validate_mutable_workspace_path(root: Path, path: Path, *, action: str) -> None:
    if path == root:
        raise ValueError(f"cannot {action} the workspace root")
    relative_parts = path.relative_to(root).parts
    protected = {".git", ".autonomy"}
    if any(part in protected for part in relative_parts):
        raise ValueError(f"cannot {action} protected workspace metadata paths")
    if _is_secret_env_path(path):
        raise ValueError(_secret_env_error(path))


def _short_unified_diff(path: Path, before: str, after: str, *, max_lines: int = 120) -> str:
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"{path.name}:before",
            tofile=f"{path.name}:after",
            lineterm="",
        )
    )
    truncated = len(diff_lines) > max_lines
    if truncated:
        diff_lines = diff_lines[:max_lines] + ["... diff truncated ..."]
    return "\n".join(diff_lines)


def _detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text[:4096] else "\n"


def _normalize_line_endings(text: str, target: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "\r\n") if target == "\r\n" else normalized


def _replace_by_stripped_lines(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool,
) -> tuple[str, int, str]:
    old_lines = old_string.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    if not old_lines:
        return content, 0, "old_string must not be empty"
    normalized_old = [line.strip() for line in old_lines]
    content_lines = content.splitlines(keepends=True)
    comparable_lines = [line.rstrip("\r\n").strip() for line in content_lines]
    span = len(normalized_old)
    matches: list[tuple[int, int]] = []
    for index in range(0, len(comparable_lines) - span + 1):
        if comparable_lines[index : index + span] == normalized_old:
            start = sum(len(line) for line in content_lines[:index])
            end = sum(len(line) for line in content_lines[: index + span])
            matches.append((start, end))
    if not matches:
        return content, 0, "old_string was not found with strip_lines matching"
    if len(matches) > 1 and not replace_all:
        return content, len(matches), (
            "old_string matched multiple stripped-line regions; "
            "set replace_all=true to replace all matches"
        )
    line_ending = _detect_line_ending(content)
    replacement = _normalize_line_endings(new_string, line_ending)
    selected = matches if replace_all else matches[:1]
    result = content
    for start, end in reversed(selected):
        result = result[:start] + replacement + result[end:]
    return result, len(selected), ""


def _relative_search_path(root: Path, path: Path) -> str:
    relative = str(path.relative_to(root))
    return "." if relative == "." else relative


def _run_rg(
    root: Path,
    command: list[str],
    *,
    offset: int = 0,
    limit: int,
) -> tuple[list[str], bool] | None:
    if not shutil.which("rg"):
        return None
    process = subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    selected: list[str] = []
    seen = 0
    truncated = False
    assert process.stdout is not None
    try:
        for raw_line in process.stdout:
            if seen < offset:
                seen += 1
                continue
            if len(selected) >= limit:
                truncated = True
                process.terminate()
                break
            selected.append(_normalize_rg_line(raw_line.rstrip("\n\r")))
            seen += 1
        returncode = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return None
    if not truncated and returncode not in {0, 1}:
        return None
    return selected, truncated


def _normalize_rg_line(line: str) -> str:
    return line[2:] if line.startswith("./") else line


def _coerce_read_window(arguments: dict) -> tuple[int, int, bool]:
    requested = "offset" in arguments or "limit" in arguments
    offset = int(arguments.get("offset", 1))
    limit = int(arguments.get("limit", _DEFAULT_READ_LIMIT))
    if offset < 1:
        raise ValueError("offset must be at least 1")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return offset, min(limit, _MAX_READ_LIMIT), requested


def _coerce_read_many_options(arguments: dict) -> tuple[list[str], int, int, int]:
    paths = arguments.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("paths must be a non-empty list of strings")
    if len(paths) > _MAX_READ_MANY_FILES:
        raise ValueError(f"paths must contain at most {_MAX_READ_MANY_FILES} entries")
    if not all(isinstance(path, str) and path.strip() for path in paths):
        raise ValueError("paths must contain non-empty strings")
    offset = int(arguments.get("offset", 1))
    limit = int(arguments.get("limit", _DEFAULT_READ_MANY_LIMIT))
    max_chars = int(arguments.get("max_chars", _DEFAULT_READ_MANY_CHARS))
    if offset < 1:
        raise ValueError("offset must be at least 1")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    return (
        paths,
        offset,
        min(limit, _MAX_READ_LIMIT),
        min(max_chars, _MAX_READ_MANY_CHARS),
    )


def _coerce_many_paths(arguments: dict, *, max_paths: int = _MAX_STAT_MANY_PATHS) -> list[str]:
    paths = arguments.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("paths must be a non-empty list of strings")
    if len(paths) > max_paths:
        raise ValueError(f"paths must contain at most {max_paths} entries")
    if not all(isinstance(path, str) and path.strip() for path in paths):
        raise ValueError("paths must contain non-empty strings")
    return paths


def _coerce_search_window(arguments: dict, default_limit: int) -> tuple[int, int]:
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", default_limit))
    if offset < 0:
        raise ValueError("offset must be at least 0")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return offset, min(limit, _MAX_SEARCH_LIMIT)


def _coerce_search_files_options(arguments: dict) -> tuple[int, int, str, int]:
    offset, limit = _coerce_search_window(arguments, _DEFAULT_SEARCH_FILES_LIMIT)
    output_mode = str(arguments.get("output_mode", "content")).strip().lower()
    if output_mode not in {"content", "files_only", "count"}:
        raise ValueError("output_mode must be content, files_only, or count")
    context = int(arguments.get("context", 0))
    if context < 0:
        raise ValueError("context must be at least 0")
    return offset, limit, output_mode, min(context, _MAX_SEARCH_CONTEXT_LINES)


def _coerce_list_window(arguments: dict) -> tuple[int, int]:
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", _DEFAULT_LIST_LIMIT))
    if offset < 0:
        raise ValueError("offset must be at least 0")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return offset, min(limit, _MAX_LIST_LIMIT)


def _coerce_outline_options(arguments: dict) -> tuple[int, int, str, bool]:
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", _DEFAULT_OUTLINE_LIMIT))
    file_glob = str(arguments.get("file_glob", "*.py") or "*.py")
    include_private = bool(arguments.get("include_private", False))
    if offset < 0:
        raise ValueError("offset must be at least 0")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return offset, min(limit, _MAX_OUTLINE_LIMIT), file_glob, include_private


def _coerce_import_options(arguments: dict) -> tuple[int, int, str, str]:
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", _DEFAULT_OUTLINE_LIMIT))
    file_glob = str(arguments.get("file_glob", "*.py") or "*.py")
    module_filter = str(arguments.get("module_filter", "") or "").strip()
    if offset < 0:
        raise ValueError("offset must be at least 0")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return offset, min(limit, _MAX_OUTLINE_LIMIT), file_glob, module_filter


def _coerce_symbol_search_options(arguments: dict) -> tuple[str, int, int, str, bool, str, str]:
    query = str(arguments["query"]).strip()
    if not query:
        raise ValueError("query must not be empty")
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", _DEFAULT_OUTLINE_LIMIT))
    file_glob = str(arguments.get("file_glob", "*.py") or "*.py")
    include_private = bool(arguments.get("include_private", False))
    match = str(arguments.get("match", "contains")).strip().lower()
    kind = str(arguments.get("kind", "any")).strip().lower()
    if offset < 0:
        raise ValueError("offset must be at least 0")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if match not in {"contains", "exact", "regex"}:
        raise ValueError("match must be contains, exact, or regex")
    if kind not in {"any", "class", "function", "async_function", "method", "async_method"}:
        raise ValueError("kind must be any, class, function, async_function, method, or async_method")
    if match == "regex":
        try:
            re.compile(query)
        except re.error as exc:
            raise ValueError(f"invalid regex query: {exc}") from exc
    return query, offset, min(limit, _MAX_OUTLINE_LIMIT), file_glob, include_private, match, kind


def _coerce_syntax_options(arguments: dict) -> tuple[int, int, str]:
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", _DEFAULT_SYNTAX_LIMIT))
    file_glob = str(arguments.get("file_glob", "*.py") or "*.py")
    if offset < 0:
        raise ValueError("offset must be at least 0")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return offset, min(limit, _MAX_SYNTAX_LIMIT), file_glob


def _coerce_tree_options(arguments: dict) -> tuple[int, int, bool, bool]:
    depth = int(arguments.get("depth", _DEFAULT_TREE_DEPTH))
    max_entries = int(arguments.get("max_entries", _DEFAULT_TREE_ENTRIES))
    if depth < 0:
        raise ValueError("depth must be at least 0")
    if max_entries < 1:
        raise ValueError("max_entries must be at least 1")
    return (
        min(depth, _MAX_TREE_DEPTH),
        min(max_entries, _MAX_TREE_ENTRIES),
        bool(arguments.get("include_files", True)),
        bool(arguments.get("include_hidden", False)),
    )


def _format_paged_results(lines: list[str], *, offset: int, limit: int, truncated: bool) -> str:
    output = "\n".join(lines)
    if truncated:
        hint = (
            f"[Hint: Results truncated. Use offset={offset + limit} to see more, "
            "or narrow the query.]"
        )
        return f"{output}\n\n{hint}" if output else hint
    if offset > 0:
        hint = "[Hint: reached end of results.]"
        return f"{output}\n\n{hint}" if output else hint
    return output


def _format_search_context_match(
    root: Path,
    file_path: Path,
    lines: list[str],
    line_index: int,
    *,
    context: int,
) -> list[str]:
    start = max(0, line_index - context)
    end = min(len(lines), line_index + context + 1)
    relative = file_path.relative_to(root)
    formatted: list[str] = []
    for index in range(start, end):
        separator = ":" if index == line_index else "-"
        formatted.append(f"{relative}{separator}{index + 1}{separator}{lines[index]}")
    return formatted


def _coerce_shell_output_limit(arguments: dict) -> int:
    max_chars = int(arguments.get("max_chars", _DEFAULT_SHELL_OUTPUT_CHARS))
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    return min(max_chars, _MAX_SHELL_OUTPUT_CHARS)


def _coerce_diff_output_limit(arguments: dict) -> int:
    max_chars = int(arguments.get("max_chars", _DEFAULT_DIFF_OUTPUT_CHARS))
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    return min(max_chars, _MAX_DIFF_OUTPUT_CHARS)


def _truncate_tool_output(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    head_chars = max(1, int(max_chars * 0.4))
    tail_chars = max(1, max_chars - head_chars)
    notice = (
        f"\n\n[Output truncated to {max_chars} chars. "
        "Showing the beginning and end. Use a narrower command or max_chars "
        "for focused output.]\n\n"
    )
    return text[:head_chars] + notice + text[-tail_chars:], True


def _searchable_paths(path: Path) -> list[Path]:
    return [path] if path.is_file() else sorted(path.rglob("*"))


def _workspace_outline_files(path: Path, *, file_glob: str) -> list[Path]:
    if path.is_file():
        return [path]
    files: list[Path] = []
    for candidate in sorted(path.rglob(file_glob)):
        if (
            not candidate.is_file()
            or any(part in _SKIPPED_SUGGESTION_DIRS for part in candidate.parts)
            or _is_secret_env_path(candidate)
            or _is_binary_like_path(candidate)
        ):
            continue
        files.append(candidate)
    return files


def _format_python_arguments(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    positional = [argument.arg for argument in node.args.posonlyargs + node.args.args]
    if node.args.vararg:
        positional.append(f"*{node.args.vararg.arg}")
    keyword_only = [argument.arg for argument in node.args.kwonlyargs]
    if keyword_only and not node.args.vararg:
        positional.append("*")
    positional.extend(keyword_only)
    if node.args.kwarg:
        positional.append(f"**{node.args.kwarg.arg}")
    return ", ".join(positional)


def _python_outline_lines(
    root: Path,
    path: Path,
    *,
    include_private: bool,
) -> tuple[list[str], str]:
    relative = path.relative_to(root)
    if path.suffix != ".py":
        return [], f"unsupported file type for outline: {relative}"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return [], f"{relative}:{exc.lineno or '?'}: syntax_error: {exc.msg}"
    except OSError as exc:
        return [], f"{relative}: read_error: {exc}"

    lines: list[str] = []

    def visible(name: str) -> bool:
        return include_private or not name.startswith("_")

    def visit_body(body: list[ast.stmt], class_stack: list[str]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                if visible(node.name):
                    qualified = ".".join([*class_stack, node.name])
                    lines.append(f"{relative}:{node.lineno}:class {qualified}")
                visit_body(node.body, [*class_stack, node.name])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not visible(node.name):
                    continue
                qualified = ".".join([*class_stack, node.name])
                if class_stack:
                    kind = "async_method" if isinstance(node, ast.AsyncFunctionDef) else "method"
                else:
                    kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                lines.append(
                    f"{relative}:{node.lineno}:{kind} {qualified}({_format_python_arguments(node)})"
                )

    visit_body(tree.body, [])
    return lines, ""


def _python_import_lines(
    root: Path,
    path: Path,
    *,
    module_filter: str = "",
) -> tuple[list[str], str]:
    relative = path.relative_to(root)
    if path.suffix != ".py":
        return [], f"unsupported file type for imports: {relative}"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return [], f"{relative}:{exc.lineno or '?'}: syntax_error: {exc.msg}"
    except OSError as exc:
        return [], f"{relative}: read_error: {exc}"

    lines: list[str] = []
    filter_text = module_filter.lower()

    def include(module: str) -> bool:
        return not filter_text or filter_text in module.lower()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
            modules = [module for module in modules if include(module)]
            if modules:
                lines.append(f"{relative}:{node.lineno}:import {', '.join(modules)}")
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * int(node.level or 0)
            module = prefix + (node.module or "")
            names = [alias.name for alias in node.names]
            rendered = f"{module} import {', '.join(names)}".strip()
            if include(module) or any(include(name) for name in names):
                lines.append(f"{relative}:{node.lineno}:from {rendered}")
    return sorted(lines), ""


def _symbol_parts(outline_line: str) -> tuple[str, str]:
    try:
        _location, rest = outline_line.rsplit(":", 1)
        kind, signature = rest.split(" ", 1)
    except ValueError:
        return "", ""
    name = signature.split("(", 1)[0]
    return kind, name


def _symbol_matches(
    outline_line: str,
    *,
    query: str,
    match: str,
    kind_filter: str,
) -> bool:
    kind, name = _symbol_parts(outline_line)
    if not kind or not name:
        return False
    if kind_filter != "any" and kind != kind_filter:
        return False
    if match == "exact":
        return name == query or name.rsplit(".", 1)[-1] == query
    if match == "regex":
        return re.search(query, name) is not None
    return query.lower() in name.lower()


def _python_syntax_diagnostic(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    if path.suffix != ".py":
        return f"{relative}: unsupported file type for syntax_check"
    try:
        ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return (
            f"{relative}:{exc.lineno or '?'}:{exc.offset or '?'}: "
            f"syntax_error: {exc.msg}"
        )
    except OSError as exc:
        return f"{relative}: read_error: {exc}"
    return ""


def _post_write_python_syntax_payload(root: Path, path: Path) -> tuple[dict, tuple[str, ...]]:
    if path.suffix != ".py":
        return {}, ()
    diagnostic = _python_syntax_diagnostic(root, path)
    syntax_ok = diagnostic == ""
    return (
        {
            "syntax_ok": syntax_ok,
            "syntax_diagnostic": diagnostic,
        },
        (
            "syntax_checked:true",
            f"syntax_ok:{str(syntax_ok).lower()}",
        ),
    )


def _numbered_read_window(path: Path, offset: int, limit: int) -> tuple[str, int, bool]:
    selected: list[str] = []
    total_lines = 0
    truncated = False
    end_line = offset + limit - 1
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            total_lines = line_number
            if line_number < offset:
                continue
            if line_number > end_line:
                truncated = True
                continue
            content = line.rstrip("\n\r")
            if len(content) > _MAX_READ_LINE_CHARS:
                content = content[:_MAX_READ_LINE_CHARS] + "... [line truncated]"
            selected.append(f"{line_number}|{content}")
    output = "\n".join(selected)
    if truncated:
        output += (
            f"\n\n[Hint: file has more content. Use offset={end_line + 1} "
            "with a focused limit to continue reading.]"
        )
    elif offset > 1:
        output += f"\n\n[Hint: reached end of file at line {total_lines}.]"
    return output, total_lines, truncated


def _tree_children(path: Path, *, include_files: bool, include_hidden: bool) -> list[Path]:
    children: list[Path] = []
    try:
        iterator = path.iterdir()
    except OSError:
        return children
    for item in iterator:
        name = item.name
        if item.is_dir() and name in _SKIPPED_TREE_DIRS:
            continue
        if name.startswith(".") and not include_hidden:
            continue
        if item.is_file():
            if not include_files or _is_secret_env_path(item):
                continue
        children.append(item)
    return sorted(children, key=lambda item: (not item.is_dir(), item.name.lower()))


def _format_workspace_tree(
    root: Path,
    start: Path,
    *,
    depth: int,
    max_entries: int,
    include_files: bool,
    include_hidden: bool,
) -> tuple[str, int, bool]:
    root_label = _relative_path_text(root, start)
    lines = [f"{root_label}/" if start.is_dir() else root_label]
    emitted = 0
    truncated = False

    def walk(directory: Path, prefix: str, remaining_depth: int) -> None:
        nonlocal emitted, truncated
        if truncated or remaining_depth <= 0:
            return
        children = _tree_children(
            directory,
            include_files=include_files,
            include_hidden=include_hidden,
        )
        for index, child in enumerate(children):
            if emitted >= max_entries:
                truncated = True
                return
            is_last = index == len(children) - 1
            connector = "`-- " if is_last else "|-- "
            name = child.name + ("/" if child.is_dir() else "")
            lines.append(f"{prefix}{connector}{name}")
            emitted += 1
            if child.is_dir():
                extension = "    " if is_last else "|   "
                walk(child, prefix + extension, remaining_depth - 1)

    if start.is_dir():
        walk(start, "", depth)
    if truncated:
        lines.append(
            f"[Hint: tree truncated after {max_entries} entries. Use path, depth, "
            "or max_entries to narrow the view.]"
        )
    elif start.is_dir() and depth == 0:
        lines.append("[Hint: depth=0 only shows the root path.]")
    return "\n".join(lines), emitted, truncated


def _iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _memory_scope(arguments: dict, *, default: str = "workspace") -> str:
    scope = str(arguments.get("scope", default)).strip().lower()
    if scope not in _MEMORY_SCOPES:
        raise ValueError("scope must be user, project, or workspace")
    return scope


def _memory_text(arguments: dict, name: str, *, default: str = "") -> str:
    value = str(arguments.get(name, default)).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _memory_limit(arguments: dict, *, default: int = _DEFAULT_MEMORY_LIMIT) -> int:
    value = int(arguments.get("limit", default))
    if value < 1:
        raise ValueError("limit must be at least 1")
    return min(value, _MAX_MEMORY_LIMIT)


def _memory_store(root: Path) -> AutonomyStore:
    return AutonomyStore(workspace_db_path(root))


def build_local_tool_registry(
    workspace: str | Path,
    toolsets: ToolsetConfiguration | None = None,
    *,
    require_available: bool = True,
    delegate_runner: DelegateRunner | None = None,
) -> ToolRegistry:
    from . import browser_tools_available

    root = Path(workspace).resolve()
    registry = ToolRegistry()

    def memory_remember(arguments: dict) -> Observation:
        try:
            memory = _memory_store(root).create_memory(
                scope=_memory_scope(arguments),
                wing=_memory_text(arguments, "wing", default="general"),
                room=_memory_text(arguments, "room", default="general"),
                content=_memory_text(arguments, "content"),
                source_run_id=str(arguments.get("source_run_id", "")).strip(),
            )
        except (TypeError, ValueError, OSError) as exc:
            return Observation("", False, error=str(exc))
        return Observation(
            "",
            True,
            output=json.dumps(memory, sort_keys=True),
            evidence=(f"memory:{memory['id']}",),
            side_effects=("persistent-memory",),
        )

    def validate_memory_remember(arguments: dict) -> None:
        _memory_scope(arguments)
        _memory_text(arguments, "wing", default="general")
        _memory_text(arguments, "room", default="general")
        _memory_text(arguments, "content")
        source_run_id = arguments.get("source_run_id", "")
        if source_run_id is not None and not isinstance(source_run_id, str):
            raise ValueError("source_run_id must be a string")

    def memory_recall(arguments: dict) -> Observation:
        try:
            memories = _memory_store(root).search_memories(
                _memory_text(arguments, "query"),
                scope=_memory_scope(arguments) if "scope" in arguments else None,
                limit=_memory_limit(arguments, default=5),
            )
        except (TypeError, ValueError, OSError) as exc:
            return Observation("", False, error=str(exc))
        return Observation(
            "",
            True,
            output=json.dumps({"memories": memories}, sort_keys=True),
            evidence=tuple(f"memory:{memory['id']}" for memory in memories),
        )

    def validate_memory_recall(arguments: dict) -> None:
        _memory_text(arguments, "query")
        if "scope" in arguments:
            _memory_scope(arguments)
        _memory_limit(arguments, default=5)

    def memory_list(arguments: dict) -> Observation:
        try:
            memories = _memory_store(root).list_memories(
                scope=_memory_scope(arguments) if "scope" in arguments else None,
                wing=(
                    str(arguments["wing"]).strip()
                    if str(arguments.get("wing", "")).strip()
                    else None
                ),
                room=(
                    str(arguments["room"]).strip()
                    if str(arguments.get("room", "")).strip()
                    else None
                ),
                limit=_memory_limit(arguments),
            )
        except (TypeError, ValueError, OSError) as exc:
            return Observation("", False, error=str(exc))
        return Observation(
            "",
            True,
            output=json.dumps({"memories": memories}, sort_keys=True),
            evidence=tuple(f"memory:{memory['id']}" for memory in memories),
        )

    def validate_memory_list(arguments: dict) -> None:
        if "scope" in arguments:
            _memory_scope(arguments)
        _memory_limit(arguments)

    def memory_forget(arguments: dict) -> Observation:
        try:
            memory_id = _memory_text(arguments, "id")
            forgotten = _memory_store(root).forget_memory(memory_id)
        except (TypeError, ValueError, OSError) as exc:
            return Observation("", False, error=str(exc))
        return Observation(
            "",
            True,
            output=json.dumps({"forgotten": forgotten, "id": memory_id}, sort_keys=True),
            side_effects=("persistent-memory",),
        )

    def validate_memory_forget(arguments: dict) -> None:
        _memory_text(arguments, "id")

    def read_file(arguments: dict) -> Observation:
        raw_path = str(arguments["path"])
        path = _resolve_inside(root, raw_path)
        if not path.is_file():
            return Observation(
                "",
                False,
                error=_missing_path_error(
                    root,
                    raw_path,
                    f"not a file: {_relative_path_text(root, path)}",
                    kind="file",
                ),
            )
        try:
            _validate_readable_text_path(path)
        except ValueError as exc:
            return Observation("", False, error=str(exc))
        offset, limit, pagination_requested = _coerce_read_window(arguments)
        size = path.stat().st_size
        if not pagination_requested and size <= _MAX_RAW_READ_CHARS:
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(text.splitlines()) <= _DEFAULT_READ_LIMIT:
                return Observation(
                    "",
                    True,
                    output=text,
                    evidence=(f"read:{path}", f"revision:{_file_revision(path)}"),
                )

        output, total_lines, truncated = _numbered_read_window(path, offset, limit)
        return Observation(
            "",
            True,
            output=output,
            evidence=(
                f"read:{path}",
                f"read_window:{offset}:{limit}:{total_lines}",
                f"revision:{_file_revision(path)}",
                f"truncated:{str(truncated).lower()}",
            ),
        )

    def read_many_files(arguments: dict) -> Observation:
        paths, offset, limit, max_chars = _coerce_read_many_options(arguments)
        results: list[dict] = []
        used_chars = 0
        successful = 0
        error_count = 0
        truncated_any = False
        for raw_path in paths:
            path = _resolve_inside(root, raw_path)
            relative = _relative_path_text(root, path)
            if not path.is_file():
                error_count += 1
                results.append(
                    {
                        "path": relative,
                        "succeeded": False,
                        "error": _missing_path_error(
                            root,
                            raw_path,
                            f"not a file: {relative}",
                            kind="file",
                        ),
                    }
                )
                continue
            try:
                _validate_readable_text_path(path)
            except ValueError as exc:
                error_count += 1
                results.append(
                    {
                        "path": relative,
                        "succeeded": False,
                        "error": str(exc),
                    }
                )
                continue
            if used_chars >= max_chars:
                truncated_any = True
                results.append(
                    {
                        "path": relative,
                        "succeeded": False,
                        "error": "read_many max_chars budget exhausted before this file",
                    }
                )
                continue
            output, total_lines, truncated = _numbered_read_window(path, offset, limit)
            remaining = max_chars - used_chars
            if len(output) > remaining:
                output = output[:remaining] + "\n[Hint: read_many max_chars reached.]"
                truncated = True
            used_chars += len(output)
            truncated_any = truncated_any or truncated
            successful += 1
            results.append(
                {
                    "path": relative,
                    "succeeded": True,
                    "content": output,
                    "offset": offset,
                    "limit": limit,
                    "revision": _file_revision(path),
                    "total_lines": total_lines,
                    "truncated": truncated,
                }
            )
        payload = {
            "files": results,
            "succeeded_count": successful,
            "error_count": error_count,
            "max_chars": max_chars,
            "chars_used": used_chars,
            "truncated": truncated_any,
        }
        return Observation(
            "",
            successful > 0,
            output=json.dumps(payload, sort_keys=True),
            evidence=(
                f"read_many:{successful}:{len(paths)}",
                f"read_window:{offset}:{limit}",
                f"errors:{error_count}",
                f"truncated:{str(truncated_any).lower()}",
            ),
        )

    def list_files(arguments: dict) -> Observation:
        raw_path = str(arguments.get("path", "."))
        path = _resolve_inside(root, raw_path)
        if not path.is_dir():
            return Observation(
                "",
                False,
                error=_missing_path_error(
                    root,
                    raw_path,
                    f"not a directory: {_relative_path_text(root, path)}",
                    kind="dir",
                ),
            )
        recursive = bool(arguments.get("recursive", False))
        entries = path.rglob("*") if recursive else path.iterdir()
        values = sorted(
            str(item.relative_to(root))
            for item in entries
            if not (item.is_file() and _is_secret_env_path(item))
        )
        offset, limit = _coerce_list_window(arguments)
        page = values[offset : offset + limit]
        truncated = len(values) > offset + limit
        return Observation(
            "",
            True,
            output=_format_paged_results(page, offset=offset, limit=limit, truncated=truncated),
            evidence=(
                f"listed:{path}",
                f"list_window:{offset}:{limit}:{len(values)}",
                f"truncated:{str(truncated).lower()}",
            ),
        )

    def tree_files(arguments: dict) -> Observation:
        raw_path = str(arguments.get("path", "."))
        path = _resolve_inside(root, raw_path)
        if not path.exists():
            return Observation(
                "",
                False,
                error=_missing_path_error(
                    root,
                    raw_path,
                    f"path not found: {_relative_path_text(root, path)}",
                    kind="any",
                ),
            )
        if path.is_file():
            try:
                _validate_readable_text_path(path)
            except ValueError as exc:
                return Observation("", False, error=str(exc))
        depth, max_entries, include_files, include_hidden = _coerce_tree_options(arguments)
        output, entries, truncated = _format_workspace_tree(
            root,
            path,
            depth=depth,
            max_entries=max_entries,
            include_files=include_files,
            include_hidden=include_hidden,
        )
        return Observation(
            "",
            True,
            output=output,
            evidence=(
                f"tree:{path}",
                f"tree_options:{depth}:{max_entries}:{str(include_files).lower()}:{str(include_hidden).lower()}",
                f"tree_entries:{entries}",
                f"truncated:{str(truncated).lower()}",
            ),
        )

    def _stat_payload(path: Path) -> dict:
        if path.is_file() and _is_secret_env_path(path):
            raise ValueError(_secret_env_error(path))
        stat_result = path.stat()
        if path.is_dir():
            child_paths = [
                child for child in path.iterdir()
                if not (child.is_file() and _is_secret_env_path(child))
            ]
            path_type = "directory"
            extra = {
                "children_count": len(child_paths),
                "file_count": sum(1 for child in child_paths if child.is_file()),
                "directory_count": sum(1 for child in child_paths if child.is_dir()),
            }
        elif path.is_file():
            path_type = "file"
            extra = {
                "suffix": path.suffix,
                "binary_like": _is_binary_like_path(path),
            }
        else:
            path_type = "other"
            extra = {}
        return {
            "path": _relative_path_text(root, path),
            "type": path_type,
            "size_bytes": stat_result.st_size,
            "modified_time": _iso_timestamp(stat_result.st_mtime),
            "permissions_octal": oct(stat_result.st_mode & 0o777),
            "revision": _file_revision(path),
            **extra,
        }

    def stat_path(arguments: dict) -> Observation:
        raw_path = str(arguments.get("path", "."))
        path = _resolve_inside(root, raw_path)
        if not path.exists():
            return Observation(
                "",
                False,
                error=_missing_path_error(
                    root,
                    raw_path,
                    f"path not found: {_relative_path_text(root, path)}",
                    kind="any",
                ),
            )
        payload = _stat_payload(path)
        return Observation(
            "",
            True,
            output=json.dumps(payload, sort_keys=True),
            evidence=(f"stat:{path}:{payload['type']}",),
        )

    def stat_many_paths(arguments: dict) -> Observation:
        paths = _coerce_many_paths(arguments)
        results: list[dict] = []
        successful = 0
        error_count = 0
        for raw_path in paths:
            path = _resolve_inside(root, raw_path)
            relative = _relative_path_text(root, path)
            if not path.exists():
                error_count += 1
                results.append(
                    {
                        "path": relative,
                        "succeeded": False,
                        "error": _missing_path_error(
                            root,
                            raw_path,
                            f"path not found: {relative}",
                            kind="any",
                        ),
                    }
                )
                continue
            try:
                payload = _stat_payload(path)
            except ValueError as exc:
                error_count += 1
                results.append(
                    {
                        "path": relative,
                        "succeeded": False,
                        "error": str(exc),
                    }
                )
                continue
            successful += 1
            results.append({"succeeded": True, **payload})
        payload = {
            "paths": results,
            "succeeded_count": successful,
            "error_count": error_count,
        }
        return Observation(
            "",
            successful > 0,
            output=json.dumps(payload, sort_keys=True),
            evidence=(
                f"stat_many:{successful}:{len(paths)}",
                f"errors:{error_count}",
            ),
        )

    def _git(args: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
        )

    def _git_relative_pathspec(path: Path) -> str:
        relative = _relative_path_text(root, path)
        return "." if relative == "." else relative

    def _is_secret_relative_path(relative: str) -> bool:
        return any(part.lower() in _SECRET_ENV_FILENAMES for part in Path(relative).parts)

    def _filter_git_status_lines(lines: list[str]) -> tuple[list[str], int]:
        visible: list[str] = []
        omitted = 0
        for line in lines:
            path_text = line[3:].strip()
            if " -> " in path_text:
                path_text = path_text.split(" -> ", 1)[1].strip()
            if _is_secret_relative_path(path_text):
                omitted += 1
                continue
            visible.append(line)
        return visible, omitted

    def _git_changed_paths(pathspec: str, *, staged: bool) -> tuple[list[str], int, str]:
        args = ["diff", "--name-only"]
        if staged:
            args.append("--cached")
        args.extend(["--", pathspec])
        completed = _git(args)
        if completed.returncode != 0:
            return [], 0, completed.stderr.strip() or completed.stdout.strip()
        visible: list[str] = []
        omitted = 0
        for line in completed.stdout.splitlines():
            relative = line.strip()
            if not relative:
                continue
            if _is_secret_relative_path(relative):
                omitted += 1
                continue
            visible.append(relative)
        return visible, omitted, ""

    def diff_workspace(arguments: dict) -> Observation:
        if not shutil.which("git"):
            return Observation("", False, error="git is not installed")
        raw_path = str(arguments.get("path", "."))
        path = _resolve_inside(root, raw_path)
        if _is_secret_env_path(path):
            return Observation("", False, error=_secret_env_error(path))
        staged = bool(arguments.get("staged", False))
        stat_only = bool(arguments.get("stat_only", False))
        max_chars = _coerce_diff_output_limit(arguments)
        repo_check = _git(["rev-parse", "--is-inside-work-tree"], timeout=10)
        if repo_check.returncode != 0 or repo_check.stdout.strip() != "true":
            return Observation("", False, error="workspace is not inside a git work tree")
        pathspec = _git_relative_pathspec(path)
        status_result = _git(["status", "--short", "--", pathspec], timeout=20)
        if status_result.returncode != 0:
            return Observation(
                "",
                False,
                error=status_result.stderr.strip() or status_result.stdout.strip(),
                evidence=(f"git_status_exit:{status_result.returncode}",),
            )
        status_lines, omitted_status = _filter_git_status_lines(status_result.stdout.splitlines())
        changed_paths, omitted_diff, changed_error = _git_changed_paths(pathspec, staged=staged)
        if changed_error:
            return Observation("", False, error=changed_error)
        diff_paths = changed_paths[:_MAX_DIFF_PATHS]
        omitted_path_limit = max(0, len(changed_paths) - len(diff_paths))
        diff_stat = ""
        diff_output = ""
        diff_truncated = False
        if diff_paths:
            diff_args = ["diff", "--stat"]
            if staged:
                diff_args.append("--cached")
            diff_args.extend(["--", *diff_paths])
            stat_result = _git(diff_args, timeout=30)
            if stat_result.returncode == 0:
                diff_stat, _ = _truncate_tool_output(stat_result.stdout.strip(), 8000)
            if not stat_only:
                raw_diff_args = ["diff", "--no-color"]
                if staged:
                    raw_diff_args.append("--cached")
                raw_diff_args.extend(["--", *diff_paths])
                raw_diff_result = _git(raw_diff_args, timeout=30)
                if raw_diff_result.returncode != 0:
                    return Observation(
                        "",
                        False,
                        error=raw_diff_result.stderr.strip() or raw_diff_result.stdout.strip(),
                    )
                redacted_diff, diff_redacted = redact_sensitive_text(raw_diff_result.stdout)
                diff_output, diff_truncated = _truncate_tool_output(redacted_diff, max_chars)
            else:
                diff_redacted = False
        else:
            diff_redacted = False
        payload = {
            "path": pathspec,
            "staged": staged,
            "stat_only": stat_only,
            "status_short": status_lines,
            "changed_files": diff_paths,
            "diff_stat": diff_stat,
            "diff": diff_output,
            "truncated": diff_truncated,
            "omitted_secret_paths": omitted_status + omitted_diff,
            "omitted_path_limit": omitted_path_limit,
        }
        return Observation(
            "",
            True,
            output=json.dumps(payload, sort_keys=True),
            evidence=(
                f"git_diff_paths:{len(diff_paths)}",
                f"git_status_lines:{len(status_lines)}",
                f"staged:{str(staged).lower()}",
                f"stat_only:{str(stat_only).lower()}",
                f"diff_truncated:{str(diff_truncated).lower()}",
                f"diff_redacted:{str(diff_redacted).lower()}",
                f"secret_paths_omitted:{omitted_status + omitted_diff}",
            ),
        )

    def _revision_precondition_error(arguments: dict, path: Path) -> str:
        expected_revision = str(arguments.get("expected_revision", "") or "").strip()
        if not expected_revision:
            return ""
        current_revision = _file_revision(path)
        if current_revision != expected_revision:
            relative = _relative_path_text(root, path)
            return (
                f"expected_revision mismatch for {relative}: "
                f"expected {expected_revision}, current {current_revision}. "
                "Re-read or stat the path before writing."
            )
        return ""

    def write_file(arguments: dict) -> Observation:
        path = _resolve_inside(root, str(arguments["path"]))
        try:
            _validate_writable_text_path(path)
        except ValueError as exc:
            return Observation("", False, error=str(exc))
        if path.exists() and path.is_dir():
            return Observation("", False, error=f"path is a directory: {path}")
        revision_error = _revision_precondition_error(arguments, path)
        if revision_error:
            return Observation("", False, error=revision_error, evidence=("revision_mismatch:true",))
        create_parent_dirs = bool(arguments.get("create_parent_dirs", True))
        parent = path.parent
        if not parent.exists():
            if create_parent_dirs:
                parent.mkdir(parents=True, exist_ok=True)
            else:
                return Observation("", False, error=f"parent directory does not exist: {parent}")
        existed = path.exists()
        previous_revision = _file_revision(path)
        before = path.read_text(encoding="utf-8", errors="replace") if existed else ""
        content = str(arguments["content"])
        path.write_text(content, encoding="utf-8")
        syntax_payload, syntax_evidence = _post_write_python_syntax_payload(root, path)
        payload = {
            "path": str(path.relative_to(root)),
            "bytes_written": len(content.encode("utf-8")),
            "created": not existed,
            "diff": _short_unified_diff(path, before, content),
            "previous_revision": previous_revision,
            "revision": _file_revision(path),
            **syntax_payload,
        }
        return Observation(
            "",
            True,
            output=json.dumps(payload, sort_keys=True),
            evidence=(f"write:{path}", *syntax_evidence),
            side_effects=("file-write",),
        )

    def patch_file(arguments: dict) -> Observation:
        path = _resolve_inside(root, str(arguments["path"]))
        try:
            _validate_writable_text_path(path)
        except ValueError as exc:
            return Observation("", False, error=str(exc))
        if not path.is_file():
            return Observation("", False, error=f"not a file: {path}")
        revision_error = _revision_precondition_error(arguments, path)
        if revision_error:
            return Observation("", False, error=revision_error, evidence=("revision_mismatch:true",))
        previous_revision = _file_revision(path)
        before = path.read_text(encoding="utf-8", errors="replace")
        old_string = str(arguments["old_string"])
        new_string = str(arguments["new_string"])
        replace_all = bool(arguments.get("replace_all", False))
        match_mode = str(arguments.get("match_mode", "exact")).strip().lower()
        if match_mode == "strip_lines":
            after, count, error = _replace_by_stripped_lines(
                before,
                old_string,
                new_string,
                replace_all=replace_all,
            )
            if error:
                return Observation("", False, error=error)
        else:
            count = before.count(old_string)
            if count == 0:
                return Observation(
                    "",
                    False,
                    error=(
                        "old_string was not found. Re-read the current file or use "
                        "match_mode=strip_lines for indentation-only differences."
                    ),
                )
            if count > 1 and not replace_all:
                return Observation(
                    "",
                    False,
                    error="old_string is not unique; set replace_all=true to replace all matches",
                )
            after = before.replace(old_string, new_string, -1 if replace_all else 1)
        diff = _short_unified_diff(path, before, after)
        path.write_text(after, encoding="utf-8")
        syntax_payload, syntax_evidence = _post_write_python_syntax_payload(root, path)
        payload = {
            "path": str(path.relative_to(root)),
            "replacements": count if replace_all else 1,
            "match_mode": match_mode,
            "diff": diff,
            "previous_revision": previous_revision,
            "revision": _file_revision(path),
            **syntax_payload,
        }
        return Observation(
            "",
            True,
            output=json.dumps(payload, sort_keys=True),
            evidence=(f"patch:{path}:{payload['replacements']}", *syntax_evidence),
            side_effects=("file-write",),
        )

    def trash_path(arguments: dict) -> Observation:
        raw_path = str(arguments["path"])
        path = _resolve_inside(root, raw_path)
        try:
            _validate_trashable_path(root, path)
        except ValueError as exc:
            return Observation("", False, error=str(exc))
        trash_binary = shutil.which("trash")
        if not trash_binary:
            return Observation(
                "",
                False,
                error="trash CLI is not installed; install trash before using filesystem.trash",
            )
        kind = "directory" if path.is_dir() else "file"
        completed = subprocess.run(
            [trash_binary, str(path)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        redacted_stdout, stdout_redacted = redact_sensitive_text(completed.stdout)
        redacted_stderr, stderr_redacted = redact_sensitive_text(completed.stderr)
        payload = {
            "path": _relative_path_text(root, path),
            "kind": kind,
            "trashed": completed.returncode == 0,
        }
        return Observation(
            "",
            completed.returncode == 0,
            output=json.dumps(payload, sort_keys=True),
            error=redacted_stderr or redacted_stdout,
            evidence=(
                f"trash:{path}",
                f"exit_code:{completed.returncode}",
                f"stdout_redacted:{str(stdout_redacted).lower()}",
                f"stderr_redacted:{str(stderr_redacted).lower()}",
            ),
            side_effects=("file-delete",),
            exit_code=completed.returncode,
        )

    def make_directory(arguments: dict) -> Observation:
        raw_path = str(arguments["path"])
        path = _resolve_inside(root, raw_path)
        try:
            _validate_mutable_workspace_path(root, path, action="create")
        except ValueError as exc:
            return Observation("", False, error=str(exc))
        parents = bool(arguments.get("parents", True))
        exist_ok = bool(arguments.get("exist_ok", False))
        if path.exists() and not path.is_dir():
            return Observation("", False, error=f"path exists and is not a directory: {raw_path}")
        if path.exists() and path.is_dir() and not exist_ok:
            return Observation("", False, error=f"directory already exists: {raw_path}")
        try:
            path.mkdir(parents=parents, exist_ok=exist_ok)
        except OSError as exc:
            return Observation("", False, error=f"{type(exc).__name__}: {exc}")
        payload = {
            "path": _relative_path_text(root, path),
            "created": True,
            "parents": parents,
            "exist_ok": exist_ok,
        }
        return Observation(
            "",
            True,
            output=json.dumps(payload, sort_keys=True),
            evidence=(f"mkdir:{path}",),
            side_effects=("file-write",),
        )

    def move_path(arguments: dict) -> Observation:
        source = _resolve_inside(root, str(arguments["source"]))
        destination = _resolve_inside(root, str(arguments["destination"]))
        try:
            _validate_mutable_workspace_path(root, source, action="move")
            _validate_mutable_workspace_path(root, destination, action="move into")
        except ValueError as exc:
            return Observation("", False, error=str(exc))
        if not source.exists():
            return Observation("", False, error=f"source does not exist: {_relative_path_text(root, source)}")
        if destination.exists():
            return Observation("", False, error=f"destination already exists: {_relative_path_text(root, destination)}")
        if source.is_dir() and (destination == source or source in destination.parents):
            return Observation("", False, error="cannot move a directory into itself")
        create_parent_dirs = bool(arguments.get("create_parent_dirs", True))
        if not destination.parent.exists():
            if create_parent_dirs:
                destination.parent.mkdir(parents=True, exist_ok=True)
            else:
                return Observation(
                    "",
                    False,
                    error=f"destination parent does not exist: {_relative_path_text(root, destination.parent)}",
                )
        try:
            shutil.move(str(source), str(destination))
        except OSError as exc:
            return Observation("", False, error=f"{type(exc).__name__}: {exc}")
        payload = {
            "source": _relative_path_text(root, source),
            "destination": _relative_path_text(root, destination),
            "kind": "directory" if destination.is_dir() else "file",
        }
        return Observation(
            "",
            True,
            output=json.dumps(payload, sort_keys=True),
            evidence=(f"move:{source}:{destination}",),
            side_effects=("file-write", "file-delete"),
        )

    def search_text(arguments: dict) -> Observation:
        raw_path = str(arguments.get("path", "."))
        path = _resolve_inside(root, raw_path)
        if not path.exists():
            return Observation(
                "",
                False,
                error=_missing_path_error(
                    root,
                    raw_path,
                    f"path not found: {_relative_path_text(root, path)}",
                    kind="any",
                ),
            )
        query = str(arguments["query"])
        offset, limit = _coerce_search_window(arguments, _DEFAULT_SEARCH_TEXT_LIMIT)
        if path.is_file() and _is_secret_env_path(path):
            return Observation("", False, error=_secret_env_error(path))
        rg_lines = _run_rg(
            root,
            [
                "rg",
                "--fixed-strings",
                "--line-number",
                "--no-heading",
                "--color",
                "never",
                query,
                _relative_search_path(root, path),
            ],
            offset=offset,
            limit=limit,
        )
        if rg_lines is not None:
            lines, truncated = rg_lines
            return Observation(
                "",
                True,
                output=_format_paged_results(lines, offset=offset, limit=limit, truncated=truncated),
                evidence=(
                    f"search:rg:{query}:{len(lines)}",
                    f"search_window:{offset}:{limit}",
                    f"truncated:{str(truncated).lower()}",
                ),
            )
        matches: list[str] = []
        seen = 0
        truncated = False
        files = _searchable_paths(path)
        for file_path in files:
            if len(matches) >= limit and truncated:
                break
            if not file_path.is_file() or ".git" in file_path.parts or _is_secret_env_path(file_path):
                continue
            try:
                for line_number, line in enumerate(
                    file_path.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if query in line:
                        if seen < offset:
                            seen += 1
                            continue
                        if len(matches) >= limit:
                            truncated = True
                            break
                        matches.append(f"{file_path.relative_to(root)}:{line_number}:{line}")
                        seen += 1
            except OSError:
                continue
        return Observation(
            "",
            True,
            output=_format_paged_results(matches, offset=offset, limit=limit, truncated=truncated),
            evidence=(
                f"search:{query}:{len(matches)}",
                f"search_window:{offset}:{limit}",
                f"truncated:{str(truncated).lower()}",
            ),
        )

    def search_files(arguments: dict) -> Observation:
        raw_path = str(arguments.get("path", "."))
        path = _resolve_inside(root, raw_path)
        if not path.exists():
            return Observation(
                "",
                False,
                error=_missing_path_error(
                    root,
                    raw_path,
                    f"path not found: {_relative_path_text(root, path)}",
                    kind="any",
                ),
            )
        pattern = str(arguments["pattern"])
        target = str(arguments.get("target", "content")).strip().lower()
        file_glob = str(arguments.get("file_glob", "") or "")
        offset, limit, output_mode, context = _coerce_search_files_options(arguments)
        if path.is_file() and _is_secret_env_path(path):
            return Observation("", False, error=_secret_env_error(path))
        matches: list[str] = []
        files = _searchable_paths(path)
        if target == "files":
            seen = 0
            truncated = False
            for file_path in files:
                if len(matches) >= limit and truncated:
                    break
                if ".git" in file_path.parts or (file_path.is_file() and _is_secret_env_path(file_path)):
                    continue
                relative = str(file_path.relative_to(root))
                if fnmatch.fnmatch(file_path.name, pattern) or fnmatch.fnmatch(relative, pattern):
                    if seen < offset:
                        seen += 1
                        continue
                    if len(matches) >= limit:
                        truncated = True
                        break
                    matches.append(relative)
                    seen += 1
            return Observation(
                "",
                True,
                output=_format_paged_results(matches, offset=offset, limit=limit, truncated=truncated),
                evidence=(
                    f"search_files:files:{pattern}:{len(matches)}",
                    f"search_window:{offset}:{limit}",
                    f"truncated:{str(truncated).lower()}",
                ),
            )

        rg_command = [
            "rg",
            "--color",
            "never",
        ]
        if output_mode == "content":
            rg_command.extend(["--line-number", "--no-heading", "--with-filename"])
            if context:
                rg_command.extend(["--context", str(context)])
        elif output_mode == "files_only":
            rg_command.append("--files-with-matches")
        elif output_mode == "count":
            rg_command.append("--count-matches")
        if file_glob:
            rg_command.extend(["--glob", file_glob])
        rg_command.extend([pattern, _relative_search_path(root, path)])
        rg_lines = _run_rg(root, rg_command, offset=offset, limit=limit)
        if rg_lines is not None:
            lines, truncated = rg_lines
            return Observation(
                "",
                True,
                output=_format_paged_results(lines, offset=offset, limit=limit, truncated=truncated),
                evidence=(
                    f"search_files:rg:{output_mode}:{pattern}:{len(lines)}",
                    f"search_window:{offset}:{limit}",
                    f"search_context:{context}",
                    f"truncated:{str(truncated).lower()}",
                ),
            )
        regex = re.compile(pattern)
        seen = 0
        truncated = False
        counts: dict[str, int] = {}
        files_with_matches: set[str] = set()
        for file_path in files:
            if len(matches) >= limit and truncated:
                break
            if (
                not file_path.is_file()
                or ".git" in file_path.parts
                or _is_binary_like_path(file_path)
                or _is_secret_env_path(file_path)
            ):
                continue
            if file_glob and not fnmatch.fnmatch(file_path.name, file_glob):
                continue
            try:
                lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
                relative = str(file_path.relative_to(root))
                file_match_count = 0
                for line_index, line in enumerate(lines):
                    if regex.search(line):
                        file_match_count += 1
                        files_with_matches.add(relative)
                        if output_mode in {"files_only", "count"}:
                            continue
                        if seen < offset:
                            seen += 1
                            continue
                        if len(matches) >= limit:
                            truncated = True
                            break
                        if context:
                            matches.extend(
                                _format_search_context_match(
                                    root,
                                    file_path,
                                    lines,
                                    line_index,
                                    context=context,
                                )
                            )
                        else:
                            matches.append(f"{relative}:{line_index + 1}:{line}")
                        seen += 1
                if file_match_count:
                    counts[relative] = file_match_count
            except OSError:
                continue
        if output_mode == "files_only":
            values = sorted(files_with_matches)
            page = values[offset : offset + limit]
            truncated = len(values) > offset + limit
            return Observation(
                "",
                True,
                output=_format_paged_results(page, offset=offset, limit=limit, truncated=truncated),
                evidence=(
                    f"search_files:files_only:{pattern}:{len(page)}",
                    f"search_window:{offset}:{limit}",
                    f"search_context:{context}",
                    f"truncated:{str(truncated).lower()}",
                ),
            )
        if output_mode == "count":
            values = [f"{relative}:{count}" for relative, count in sorted(counts.items())]
            page = values[offset : offset + limit]
            truncated = len(values) > offset + limit
            return Observation(
                "",
                True,
                output=_format_paged_results(page, offset=offset, limit=limit, truncated=truncated),
                evidence=(
                    f"search_files:count:{pattern}:{len(page)}",
                    f"search_window:{offset}:{limit}",
                    f"search_context:{context}",
                    f"truncated:{str(truncated).lower()}",
                ),
            )
        return Observation(
            "",
            True,
            output=_format_paged_results(matches, offset=offset, limit=limit, truncated=truncated),
            evidence=(
                f"search_files:content:{pattern}:{len(matches)}",
                f"search_window:{offset}:{limit}",
                f"search_context:{context}",
                f"truncated:{str(truncated).lower()}",
            ),
        )

    def outline_files(arguments: dict) -> Observation:
        raw_path = str(arguments.get("path", "."))
        path = _resolve_inside(root, raw_path)
        if not path.exists():
            return Observation(
                "",
                False,
                error=_missing_path_error(
                    root,
                    raw_path,
                    f"path not found: {_relative_path_text(root, path)}",
                    kind="any",
                ),
            )
        if path.is_file():
            try:
                _validate_readable_text_path(path)
            except ValueError as exc:
                return Observation("", False, error=str(exc))
        offset, limit, file_glob, include_private = _coerce_outline_options(arguments)
        files = _workspace_outline_files(path, file_glob=file_glob)
        symbols: list[str] = []
        errors: list[str] = []
        for file_path in files:
            lines, error = _python_outline_lines(
                root,
                file_path,
                include_private=include_private,
            )
            symbols.extend(lines)
            if error:
                errors.append(error)
        if path.is_file() and errors:
            return Observation("", False, error=errors[0])
        values = symbols
        if not values and errors:
            values = [f"[Skipped] {error}" for error in errors[:limit]]
        page = values[offset : offset + limit]
        truncated = len(values) > offset + limit
        return Observation(
            "",
            True,
            output=_format_paged_results(page, offset=offset, limit=limit, truncated=truncated),
            evidence=(
                f"outline_files:{len(files)}",
                f"outline_symbols:{len(symbols)}",
                f"outline_errors:{len(errors)}",
                f"outline_window:{offset}:{limit}",
                f"truncated:{str(truncated).lower()}",
            ),
        )

    def imports_files(arguments: dict) -> Observation:
        raw_path = str(arguments.get("path", "."))
        path = _resolve_inside(root, raw_path)
        if not path.exists():
            return Observation(
                "",
                False,
                error=_missing_path_error(
                    root,
                    raw_path,
                    f"path not found: {_relative_path_text(root, path)}",
                    kind="any",
                ),
            )
        if path.is_file():
            try:
                _validate_readable_text_path(path)
            except ValueError as exc:
                return Observation("", False, error=str(exc))
        offset, limit, file_glob, module_filter = _coerce_import_options(arguments)
        files = _workspace_outline_files(path, file_glob=file_glob)
        imports: list[str] = []
        errors: list[str] = []
        for file_path in files:
            lines, error = _python_import_lines(
                root,
                file_path,
                module_filter=module_filter,
            )
            imports.extend(lines)
            if error:
                errors.append(error)
        if path.is_file() and errors:
            return Observation("", False, error=errors[0])
        page = imports[offset : offset + limit]
        truncated = len(imports) > offset + limit
        return Observation(
            "",
            True,
            output=_format_paged_results(page, offset=offset, limit=limit, truncated=truncated),
            evidence=(
                f"import_files:{len(files)}",
                f"import_matches:{len(imports)}",
                f"import_errors:{len(errors)}",
                f"import_filter:{module_filter}",
                f"import_window:{offset}:{limit}",
                f"truncated:{str(truncated).lower()}",
            ),
        )

    def symbol_search(arguments: dict) -> Observation:
        raw_path = str(arguments.get("path", "."))
        path = _resolve_inside(root, raw_path)
        if not path.exists():
            return Observation(
                "",
                False,
                error=_missing_path_error(
                    root,
                    raw_path,
                    f"path not found: {_relative_path_text(root, path)}",
                    kind="any",
                ),
            )
        if path.is_file():
            try:
                _validate_readable_text_path(path)
            except ValueError as exc:
                return Observation("", False, error=str(exc))
        query, offset, limit, file_glob, include_private, match, kind_filter = (
            _coerce_symbol_search_options(arguments)
        )
        files = _workspace_outline_files(path, file_glob=file_glob)
        matches: list[str] = []
        errors: list[str] = []
        for file_path in files:
            lines, error = _python_outline_lines(
                root,
                file_path,
                include_private=include_private,
            )
            if error:
                errors.append(error)
                continue
            matches.extend(
                line
                for line in lines
                if _symbol_matches(
                    line,
                    query=query,
                    match=match,
                    kind_filter=kind_filter,
                )
            )
        page = matches[offset : offset + limit]
        truncated = len(matches) > offset + limit
        return Observation(
            "",
            True,
            output=_format_paged_results(page, offset=offset, limit=limit, truncated=truncated),
            evidence=(
                f"symbol_files:{len(files)}",
                f"symbol_matches:{len(matches)}",
                f"symbol_errors:{len(errors)}",
                f"symbol_match_mode:{match}",
                f"symbol_kind:{kind_filter}",
                f"symbol_window:{offset}:{limit}",
                f"truncated:{str(truncated).lower()}",
            ),
        )

    def syntax_check(arguments: dict) -> Observation:
        raw_path = str(arguments.get("path", "."))
        path = _resolve_inside(root, raw_path)
        if not path.exists():
            return Observation(
                "",
                False,
                error=_missing_path_error(
                    root,
                    raw_path,
                    f"path not found: {_relative_path_text(root, path)}",
                    kind="any",
                ),
            )
        if path.is_file():
            try:
                _validate_readable_text_path(path)
            except ValueError as exc:
                return Observation("", False, error=str(exc))
        offset, limit, file_glob = _coerce_syntax_options(arguments)
        files = _workspace_outline_files(path, file_glob=file_glob)
        diagnostics = [
            diagnostic
            for file_path in files
            if (diagnostic := _python_syntax_diagnostic(root, file_path))
        ]
        page = diagnostics[offset : offset + limit]
        truncated = len(diagnostics) > offset + limit
        syntax_ok = not diagnostics
        output = (
            f"OK: checked {len(files)} Python file(s); no syntax errors found."
            if syntax_ok
            else _format_paged_results(page, offset=offset, limit=limit, truncated=truncated)
        )
        return Observation(
            "",
            True,
            output=output,
            evidence=(
                f"syntax_files:{len(files)}",
                f"syntax_errors:{len(diagnostics)}",
                f"syntax_ok:{str(syntax_ok).lower()}",
                f"syntax_window:{offset}:{limit}",
                f"truncated:{str(truncated).lower()}",
            ),
        )

    def shell_execute(arguments: dict) -> Observation:
        command = str(arguments["command"])
        timeout = min(int(arguments.get("timeout", 60)), 300)
        max_chars = _coerce_shell_output_limit(arguments)
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
            shell=True,
        )
        redacted_stdout, stdout_redacted = redact_sensitive_text(completed.stdout)
        redacted_stderr, stderr_redacted = redact_sensitive_text(completed.stderr)
        output, stdout_truncated = _truncate_tool_output(redacted_stdout, max_chars)
        error, stderr_truncated = _truncate_tool_output(redacted_stderr, max_chars)
        return Observation(
            "",
            completed.returncode == 0,
            output=output,
            error=error,
            evidence=(
                f"exit_code:{completed.returncode}",
                f"stdout_truncated:{str(stdout_truncated).lower()}",
                f"stderr_truncated:{str(stderr_truncated).lower()}",
                f"stdout_redacted:{str(stdout_redacted).lower()}",
                f"stderr_redacted:{str(stderr_redacted).lower()}",
            ),
            exit_code=completed.returncode,
        )

    def assistant_respond(arguments: dict) -> Observation:
        return Observation(
            "",
            True,
            output=str(arguments["response"]).strip(),
            evidence=("assistant_response",),
        )

    def validate_read(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments["path"]))
        _validate_readable_text_path(path)
        _coerce_read_window(arguments)

    def validate_read_many(arguments: dict) -> None:
        paths, _, _, _ = _coerce_read_many_options(arguments)
        for raw_path in paths:
            path = _resolve_inside(root, raw_path)
            _validate_readable_text_path(path)

    def validate_list(arguments: dict) -> None:
        _resolve_inside(root, str(arguments.get("path", ".")))
        _coerce_list_window(arguments)

    def validate_tree(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        if path.is_file():
            _validate_readable_text_path(path)
        _coerce_tree_options(arguments)

    def validate_stat(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        if path.is_file() and _is_secret_env_path(path):
            raise ValueError(_secret_env_error(path))

    def validate_stat_many(arguments: dict) -> None:
        for raw_path in _coerce_many_paths(arguments):
            path = _resolve_inside(root, raw_path)
            if path.is_file() and _is_secret_env_path(path):
                raise ValueError(_secret_env_error(path))

    def validate_diff(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        if _is_secret_env_path(path):
            raise ValueError(_secret_env_error(path))
        _coerce_diff_output_limit(arguments)

    def validate_write(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments["path"]))
        _validate_writable_text_path(path)
        if path.exists() and path.is_dir():
            raise ValueError(f"path is a directory: {arguments['path']}")
        if "content" not in arguments or not isinstance(arguments["content"], str):
            raise ValueError("content must be a string")
        if "expected_revision" in arguments and not isinstance(arguments["expected_revision"], str):
            raise ValueError("expected_revision must be a string")
        create_parent_dirs = bool(arguments.get("create_parent_dirs", True))
        if not create_parent_dirs and not path.parent.exists():
            raise ValueError("parent directory does not exist")

    def validate_patch(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments["path"]))
        _validate_writable_text_path(path)
        if not path.is_file():
            raise ValueError(f"not a file: {arguments['path']}")
        if not str(arguments["old_string"]):
            raise ValueError("old_string must not be empty")
        if "new_string" not in arguments or not isinstance(arguments["new_string"], str):
            raise ValueError("new_string must be a string")
        if "expected_revision" in arguments and not isinstance(arguments["expected_revision"], str):
            raise ValueError("expected_revision must be a string")
        match_mode = str(arguments.get("match_mode", "exact")).strip().lower()
        if match_mode not in {"exact", "strip_lines"}:
            raise ValueError("match_mode must be exact or strip_lines")

    def validate_trash(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments["path"]))
        _validate_trashable_path(root, path)

    def validate_mkdir(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments["path"]))
        _validate_mutable_workspace_path(root, path, action="create")
        if path.exists() and not path.is_dir():
            raise ValueError(f"path exists and is not a directory: {arguments['path']}")
        if "parents" in arguments and not isinstance(arguments["parents"], bool):
            raise ValueError("parents must be a boolean")
        if "exist_ok" in arguments and not isinstance(arguments["exist_ok"], bool):
            raise ValueError("exist_ok must be a boolean")

    def validate_move(arguments: dict) -> None:
        source = _resolve_inside(root, str(arguments["source"]))
        destination = _resolve_inside(root, str(arguments["destination"]))
        _validate_mutable_workspace_path(root, source, action="move")
        _validate_mutable_workspace_path(root, destination, action="move into")
        if not source.exists():
            raise ValueError(f"source does not exist: {_relative_path_text(root, source)}")
        if destination.exists():
            raise ValueError(f"destination already exists: {_relative_path_text(root, destination)}")
        if source.is_dir() and (destination == source or source in destination.parents):
            raise ValueError("cannot move a directory into itself")
        if "create_parent_dirs" in arguments and not isinstance(arguments["create_parent_dirs"], bool):
            raise ValueError("create_parent_dirs must be a boolean")

    def trash_available() -> tuple[bool, str]:
        return (
            (True, "")
            if shutil.which("trash")
            else (False, "trash CLI is not installed")
        )

    def validate_search(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        if path.is_file() and _is_secret_env_path(path):
            raise ValueError(_secret_env_error(path))
        if not str(arguments["query"]):
            raise ValueError("query must not be empty")
        _coerce_search_window(arguments, _DEFAULT_SEARCH_TEXT_LIMIT)

    def validate_search_files(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        if path.is_file() and _is_secret_env_path(path):
            raise ValueError(_secret_env_error(path))
        pattern = str(arguments["pattern"])
        if not pattern:
            raise ValueError("pattern must not be empty")
        target = str(arguments.get("target", "content")).strip().lower()
        if target not in {"content", "files"}:
            raise ValueError("target must be content or files")
        if target == "content":
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex pattern: {exc}") from exc
        _coerce_search_files_options(arguments)

    def validate_outline(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        if path.is_file():
            _validate_readable_text_path(path)
            if path.suffix != ".py":
                raise ValueError("filesystem.outline currently supports Python files")
        _coerce_outline_options(arguments)

    def validate_imports(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        if path.is_file():
            _validate_readable_text_path(path)
            if path.suffix != ".py":
                raise ValueError("filesystem.imports currently supports Python files")
        _coerce_import_options(arguments)

    def validate_symbol_search(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        if path.is_file():
            _validate_readable_text_path(path)
            if path.suffix != ".py":
                raise ValueError("filesystem.symbol_search currently supports Python files")
        _coerce_symbol_search_options(arguments)

    def validate_syntax(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        if path.is_file():
            _validate_readable_text_path(path)
            if path.suffix != ".py":
                raise ValueError("filesystem.syntax_check currently supports Python files")
        _coerce_syntax_options(arguments)

    def validate_shell(arguments: dict) -> None:
        if not str(arguments["command"]).strip():
            raise ValueError("command must not be empty")
        timeout = int(arguments.get("timeout", 60))
        if timeout < 1:
            raise ValueError("timeout must be at least 1")
        _coerce_shell_output_limit(arguments)

    def validate_assistant_respond(arguments: dict) -> None:
        if not str(arguments["response"]).strip():
            raise ValueError("response must not be empty")

    registry.register(
        "filesystem.read",
        read_file,
        validate_read,
        description=(
            "Read a UTF-8 text file inside the workspace. Use offset and limit "
            "for large files; paginated output is LINE|CONTENT."
        ),
        toolset="file",
        argument_contract={
            "path": "string",
            "offset": "integer line number, 1-indexed (optional)",
            "limit": "integer max lines, default 500, max 2000 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.read_many",
        read_many_files,
        validate_read_many,
        description=(
            "Read multiple UTF-8 workspace text files in one bounded action. "
            "Use this for small manifest or entrypoint batches."
        ),
        toolset="file",
        argument_contract={
            "paths": "array of string paths, max 12",
            "offset": "integer line number applied to each file, 1-indexed (optional)",
            "limit": "integer max lines per file, default 200, max 2000 (optional)",
            "max_chars": "integer max total output chars, default 50000, max 100000 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.list",
        list_files,
        validate_list,
        description="List files or directories inside the workspace.",
        toolset="file",
        argument_contract={
            "path": "string (optional)",
            "recursive": "boolean (optional)",
            "limit": "integer max entries, default 500, max 2000 (optional)",
            "offset": "integer entry offset, 0-indexed (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.tree",
        tree_files,
        validate_tree,
        description=(
            "Return a compact ASCII tree for a workspace path. Prefer this for "
            "repository orientation before broad recursive listing."
        ),
        toolset="file",
        argument_contract={
            "path": "string (optional)",
            "depth": "integer max directory depth, default 3, max 8 (optional)",
            "max_entries": "integer max emitted entries, default 200, max 1000 (optional)",
            "include_files": "boolean, default true (optional)",
            "include_hidden": "boolean, default false; .git/.autonomy still skipped (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.stat",
        stat_path,
        validate_stat,
        description=(
            "Return JSON metadata for one workspace file or directory without reading file content."
        ),
        toolset="file",
        argument_contract={"path": "string, default . (optional)"},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.stat_many",
        stat_many_paths,
        validate_stat_many,
        description=(
            "Return JSON metadata for multiple workspace paths in one bounded action "
            "without reading file content."
        ),
        toolset="file",
        argument_contract={
            "paths": "array of string paths, max 50",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.diff",
        diff_workspace,
        validate_diff,
        description=(
            "Return bounded read-only git status and diff information for the workspace "
            "or one workspace path, omitting secret environment files."
        ),
        toolset="file",
        argument_contract={
            "path": "string, default . (optional)",
            "staged": "boolean; inspect staged diff instead of unstaged diff (optional)",
            "stat_only": "boolean; include diff stat but omit full diff text (optional)",
            "max_chars": "integer max diff chars, default 50000, max 200000 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.write",
        write_file,
        validate_write,
        description="Create or overwrite a UTF-8 text file inside the workspace.",
        toolset="file",
        argument_contract={
            "path": "string",
            "content": "string",
            "create_parent_dirs": "boolean (optional)",
            "expected_revision": "string from filesystem.stat/read evidence; fail if current revision differs (optional)",
        },
        default_risk=RiskLevel.MEDIUM,
        side_effects=("file-write",),
    )
    registry.register(
        "filesystem.patch",
        patch_file,
        validate_patch,
        description="Replace text in one UTF-8 workspace file and return a unified diff.",
        toolset="file",
        argument_contract={
            "path": "string",
            "old_string": "string",
            "new_string": "string",
            "replace_all": "boolean (optional)",
            "match_mode": "exact|strip_lines (optional)",
            "expected_revision": "string from filesystem.stat/read evidence; fail if current revision differs (optional)",
        },
        default_risk=RiskLevel.MEDIUM,
        side_effects=("file-write",),
    )
    registry.register(
        "filesystem.trash",
        trash_path,
        validate_trash,
        description=(
            "Move one workspace file or directory to the system Trash using the trash CLI. "
            "Never use shell rm/rmdir for file deletion."
        ),
        toolset="file",
        argument_contract={"path": "string"},
        default_risk=RiskLevel.MEDIUM,
        side_effects=("file-delete",),
        availability_check=trash_available,
    )
    registry.register(
        "filesystem.mkdir",
        make_directory,
        validate_mkdir,
        description="Create one workspace directory without using shell mkdir.",
        toolset="file",
        argument_contract={
            "path": "string",
            "parents": "boolean create parent directories, default true (optional)",
            "exist_ok": "boolean allow existing directory, default false (optional)",
        },
        default_risk=RiskLevel.MEDIUM,
        side_effects=("file-write",),
    )
    registry.register(
        "filesystem.move",
        move_path,
        validate_move,
        description="Move or rename one workspace file or directory without overwriting destination.",
        toolset="file",
        argument_contract={
            "source": "string",
            "destination": "string",
            "create_parent_dirs": "boolean create destination parent directories, default true (optional)",
        },
        default_risk=RiskLevel.MEDIUM,
        side_effects=("file-write", "file-delete"),
    )
    registry.register(
        "filesystem.search_files",
        search_files,
        validate_search_files,
        description="Search workspace files by regex content or filename glob.",
        toolset="file",
        argument_contract={
            "pattern": "string",
            "target": "content|files (optional)",
            "path": "string (optional)",
            "file_glob": "string (optional)",
            "limit": "integer (optional)",
            "offset": "integer result offset, 0-indexed (optional)",
            "output_mode": "content|files_only|count for target=content, default content (optional)",
            "context": "integer context lines around content matches, default 0, max 10 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.outline",
        outline_files,
        validate_outline,
        description=(
            "Return a compact Python symbol outline for a file or directory. "
            "Use this before broad reads when orienting within Python code."
        ),
        toolset="file",
        argument_contract={
            "path": "string file or directory, default . (optional)",
            "file_glob": "glob for directory mode, default *.py (optional)",
            "limit": "integer max symbols, default 200, max 1000 (optional)",
            "offset": "integer symbol offset, 0-indexed (optional)",
            "include_private": "boolean include names starting with _, default false (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.imports",
        imports_files,
        validate_imports,
        description=(
            "Return Python import statements for a file or directory. "
            "Use this to understand module dependencies without reading full files."
        ),
        toolset="file",
        argument_contract={
            "path": "string file or directory, default . (optional)",
            "file_glob": "glob for directory mode, default *.py (optional)",
            "module_filter": "string contains filter for imported module/name (optional)",
            "limit": "integer max imports, default 200, max 1000 (optional)",
            "offset": "integer import offset, 0-indexed (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.symbol_search",
        symbol_search,
        validate_symbol_search,
        description=(
            "Search Python class/function/method definitions by symbol name. "
            "Use this to locate code before reading full files."
        ),
        toolset="file",
        argument_contract={
            "query": "string symbol name or pattern",
            "path": "string file or directory, default . (optional)",
            "file_glob": "glob for directory mode, default *.py (optional)",
            "match": "contains|exact|regex, default contains (optional)",
            "kind": "any|class|function|async_function|method|async_method, default any (optional)",
            "limit": "integer max matches, default 200, max 1000 (optional)",
            "offset": "integer match offset, 0-indexed (optional)",
            "include_private": "boolean include names starting with _, default false (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.syntax_check",
        syntax_check,
        validate_syntax,
        description=(
            "Check Python syntax for a workspace file or directory without executing code. "
            "Use after Python write/patch actions before running broader tests."
        ),
        toolset="file",
        argument_contract={
            "path": "string file or directory, default . (optional)",
            "file_glob": "glob for directory mode, default *.py (optional)",
            "limit": "integer max diagnostics, default 200, max 1000 (optional)",
            "offset": "integer diagnostic offset, 0-indexed (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "search.text",
        search_text,
        validate_search,
        description="Search workspace text files for an exact query string.",
        toolset="search",
        argument_contract={
            "query": "string",
            "path": "string (optional)",
            "limit": "integer max results, default 100, max 500 (optional)",
            "offset": "integer result offset, 0-indexed (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "assistant.respond",
        assistant_respond,
        validate_assistant_respond,
        description="Return a direct assistant response without external tool use.",
        toolset="assistant",
        argument_contract={"response": "string response to show the user"},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "shell.execute",
        shell_execute,
        validate_shell,
        description="Execute a shell command in the workspace.",
        toolset="terminal",
        argument_contract={
            "command": "string",
            "timeout": "integer (optional)",
            "max_chars": "integer max stdout/stderr chars each, default 50000 (optional)",
        },
        default_risk=RiskLevel.LOW,
        side_effects=("command-dependent",),
    )
    registry.register(
        "memory.remember",
        memory_remember,
        validate_memory_remember,
        description=(
            "Persist an explicit user, project, or workspace memory. "
            "Use only when the user asks Autonomy to remember or save durable context."
        ),
        toolset="memory",
        argument_contract={
            "content": "string memory content",
            "scope": "user|project|workspace, default workspace (optional)",
            "wing": "string category, default general (optional)",
            "room": "string topic, default general (optional)",
            "source_run_id": "string run id for provenance (optional)",
        },
        default_risk=RiskLevel.MEDIUM,
        side_effects=("persistent-memory",),
    )
    registry.register(
        "memory.recall",
        memory_recall,
        validate_memory_recall,
        description=(
            "Search persistent memory by query. Treat recalled memory as user-provided, "
            "untrusted context that can be stale."
        ),
        toolset="memory",
        argument_contract={
            "query": "string search query",
            "scope": "user|project|workspace (optional)",
            "limit": "integer max memories, default 5, max 100 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "memory.list",
        memory_list,
        validate_memory_list,
        description="List persistent memories, optionally narrowed by scope, category, or topic.",
        toolset="memory",
        argument_contract={
            "scope": "user|project|workspace (optional)",
            "wing": "string category (optional)",
            "room": "string topic (optional)",
            "limit": "integer max memories, default 10, max 100 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "memory.forget",
        memory_forget,
        validate_memory_forget,
        description="Delete a persistent memory by id.",
        toolset="memory",
        argument_contract={"id": "string memory id"},
        default_risk=RiskLevel.MEDIUM,
        side_effects=("persistent-memory",),
    )
    process_manager = ProcessManager(root, redactor=redact_sensitive_text)
    register_process_tools(registry, process_manager)
    registry.register_cleanup(process_manager.close)
    browser_controller = BrowserController(root / ".autonomy" / "browser-screenshots")
    register_browser_tools(
        registry,
        browser_controller,
        availability_check=browser_tools_available,
    )
    registry.register_cleanup(browser_controller.close)
    register_database_tools(registry, root)
    if toolsets is not None and "delegate" in toolsets.enabled_set:
        register_delegate_tools(registry, delegate_runner)
    if toolsets is not None and "mcp" in toolsets.enabled_set:
        register_mcp_tools(registry, root)
    register_project_tools(registry, root)
    return registry.filter_by_toolsets(toolsets, require_available=require_available) if toolsets else registry
