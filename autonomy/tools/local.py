from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from ..models import Observation, RiskLevel
from ..toolsets import ToolsetConfiguration
from .registry import ToolRegistry
from .toolsets.browser import BrowserController, register_browser_tools
from .toolsets.process import ProcessManager, register_process_tools
from .toolsets.web import register_web_tools


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


def _resolve_inside(root: Path, raw_path: str) -> Path:
    if not str(raw_path).strip():
        raise ValueError("path must not be empty")
    path = Path(raw_path)
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path escapes workspace: {raw_path}")
    return resolved


def _is_binary_like_path(path: Path) -> bool:
    return path.suffix.lower() in _BINARY_LIKE_EXTENSIONS


def _validate_text_file_path(path: Path) -> None:
    if _is_binary_like_path(path):
        raise ValueError(f"binary-like file extension is not supported: {path.suffix}")


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


def build_local_tool_registry(
    workspace: str | Path,
    toolsets: ToolsetConfiguration | None = None,
) -> ToolRegistry:
    from . import browser_tools_available

    root = Path(workspace).resolve()
    registry = ToolRegistry()

    def read_file(arguments: dict) -> Observation:
        path = _resolve_inside(root, str(arguments["path"]))
        if not path.is_file():
            return Observation("", False, error=f"not a file: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        return Observation("", True, output=text, evidence=(f"read:{path}",))

    def list_files(arguments: dict) -> Observation:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        if not path.is_dir():
            return Observation("", False, error=f"not a directory: {path}")
        recursive = bool(arguments.get("recursive", False))
        entries = path.rglob("*") if recursive else path.iterdir()
        values = sorted(str(item.relative_to(root)) for item in entries)
        return Observation("", True, output="\n".join(values), evidence=(f"listed:{path}",))

    def write_file(arguments: dict) -> Observation:
        path = _resolve_inside(root, str(arguments["path"]))
        _validate_text_file_path(path)
        if path.exists() and path.is_dir():
            return Observation("", False, error=f"path is a directory: {path}")
        create_parent_dirs = bool(arguments.get("create_parent_dirs", True))
        parent = path.parent
        if not parent.exists():
            if create_parent_dirs:
                parent.mkdir(parents=True, exist_ok=True)
            else:
                return Observation("", False, error=f"parent directory does not exist: {parent}")
        existed = path.exists()
        before = path.read_text(encoding="utf-8", errors="replace") if existed else ""
        content = str(arguments["content"])
        path.write_text(content, encoding="utf-8")
        payload = {
            "path": str(path.relative_to(root)),
            "bytes_written": len(content.encode("utf-8")),
            "created": not existed,
            "diff": _short_unified_diff(path, before, content),
        }
        return Observation(
            "",
            True,
            output=json.dumps(payload, sort_keys=True),
            evidence=(f"write:{path}",),
            side_effects=("file-write",),
        )

    def patch_file(arguments: dict) -> Observation:
        path = _resolve_inside(root, str(arguments["path"]))
        _validate_text_file_path(path)
        if not path.is_file():
            return Observation("", False, error=f"not a file: {path}")
        before = path.read_text(encoding="utf-8", errors="replace")
        old_string = str(arguments["old_string"])
        new_string = str(arguments["new_string"])
        replace_all = bool(arguments.get("replace_all", False))
        count = before.count(old_string)
        if count == 0:
            return Observation("", False, error="old_string was not found")
        if count > 1 and not replace_all:
            return Observation(
                "",
                False,
                error="old_string is not unique; set replace_all=true to replace all matches",
            )
        after = before.replace(old_string, new_string, -1 if replace_all else 1)
        diff = _short_unified_diff(path, before, after)
        path.write_text(after, encoding="utf-8")
        payload = {
            "path": str(path.relative_to(root)),
            "replacements": count if replace_all else 1,
            "diff": diff,
        }
        return Observation(
            "",
            True,
            output=json.dumps(payload, sort_keys=True),
            evidence=(f"patch:{path}:{payload['replacements']}",),
            side_effects=("file-write",),
        )

    def search_text(arguments: dict) -> Observation:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        query = str(arguments["query"])
        matches: list[str] = []
        files = [path] if path.is_file() else path.rglob("*")
        for file_path in files:
            if not file_path.is_file() or ".git" in file_path.parts:
                continue
            try:
                for line_number, line in enumerate(
                    file_path.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if query in line:
                        matches.append(f"{file_path.relative_to(root)}:{line_number}:{line}")
            except OSError:
                continue
        return Observation(
            "",
            True,
            output="\n".join(matches),
            evidence=(f"search:{query}:{len(matches)}",),
        )

    def search_files(arguments: dict) -> Observation:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        pattern = str(arguments["pattern"])
        target = str(arguments.get("target", "content")).strip().lower()
        file_glob = str(arguments.get("file_glob", "") or "")
        limit = min(max(int(arguments.get("limit", 50)), 1), 500)
        matches: list[str] = []
        files = [path] if path.is_file() else path.rglob("*")
        if target == "files":
            for file_path in files:
                if len(matches) >= limit:
                    break
                if ".git" in file_path.parts:
                    continue
                relative = str(file_path.relative_to(root))
                if fnmatch.fnmatch(file_path.name, pattern) or fnmatch.fnmatch(relative, pattern):
                    matches.append(relative)
            return Observation(
                "",
                True,
                output="\n".join(matches),
                evidence=(f"search_files:files:{pattern}:{len(matches)}",),
            )

        regex = re.compile(pattern)
        for file_path in files:
            if len(matches) >= limit:
                break
            if not file_path.is_file() or ".git" in file_path.parts or _is_binary_like_path(file_path):
                continue
            if file_glob and not fnmatch.fnmatch(file_path.name, file_glob):
                continue
            try:
                for line_number, line in enumerate(
                    file_path.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if regex.search(line):
                        matches.append(f"{file_path.relative_to(root)}:{line_number}:{line}")
                        if len(matches) >= limit:
                            break
            except OSError:
                continue
        return Observation(
            "",
            True,
            output="\n".join(matches),
            evidence=(f"search_files:content:{pattern}:{len(matches)}",),
        )

    def shell_execute(arguments: dict) -> Observation:
        command = str(arguments["command"])
        timeout = min(int(arguments.get("timeout", 60)), 300)
        completed = subprocess.run(
            shlex.split(command),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
        )
        output = completed.stdout
        error = completed.stderr
        return Observation(
            "",
            completed.returncode == 0,
            output=output,
            error=error,
            evidence=(f"exit_code:{completed.returncode}",),
            exit_code=completed.returncode,
        )

    def validate_read(arguments: dict) -> None:
        _resolve_inside(root, str(arguments["path"]))

    def validate_list(arguments: dict) -> None:
        _resolve_inside(root, str(arguments.get("path", ".")))

    def validate_write(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments["path"]))
        _validate_text_file_path(path)
        if path.exists() and path.is_dir():
            raise ValueError(f"path is a directory: {arguments['path']}")
        if "content" not in arguments or not isinstance(arguments["content"], str):
            raise ValueError("content must be a string")
        create_parent_dirs = bool(arguments.get("create_parent_dirs", True))
        if not create_parent_dirs and not path.parent.exists():
            raise ValueError("parent directory does not exist")

    def validate_patch(arguments: dict) -> None:
        path = _resolve_inside(root, str(arguments["path"]))
        _validate_text_file_path(path)
        if not path.is_file():
            raise ValueError(f"not a file: {arguments['path']}")
        if not str(arguments["old_string"]):
            raise ValueError("old_string must not be empty")
        if "new_string" not in arguments or not isinstance(arguments["new_string"], str):
            raise ValueError("new_string must be a string")

    def validate_search(arguments: dict) -> None:
        _resolve_inside(root, str(arguments.get("path", ".")))
        if not str(arguments["query"]):
            raise ValueError("query must not be empty")

    def validate_search_files(arguments: dict) -> None:
        _resolve_inside(root, str(arguments.get("path", ".")))
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
        limit = int(arguments.get("limit", 50))
        if limit < 1:
            raise ValueError("limit must be at least 1")

    def validate_shell(arguments: dict) -> None:
        if not str(arguments["command"]).strip():
            raise ValueError("command must not be empty")
        timeout = int(arguments.get("timeout", 60))
        if timeout < 1:
            raise ValueError("timeout must be at least 1")

    registry.register(
        "filesystem.read",
        read_file,
        validate_read,
        description="Read a UTF-8 text file inside the workspace.",
        toolset="file",
        argument_contract={"path": "string"},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.list",
        list_files,
        validate_list,
        description="List files or directories inside the workspace.",
        toolset="file",
        argument_contract={"path": "string (optional)", "recursive": "boolean (optional)"},
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
        },
        default_risk=RiskLevel.MEDIUM,
        side_effects=("file-write",),
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
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "search.text",
        search_text,
        validate_search,
        description="Search workspace text files for an exact query string.",
        toolset="search",
        argument_contract={"query": "string", "path": "string (optional)"},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "shell.execute",
        shell_execute,
        validate_shell,
        description="Execute a shell command in the workspace.",
        toolset="terminal",
        argument_contract={"command": "string", "timeout": "integer (optional)"},
        default_risk=RiskLevel.LOW,
        side_effects=("command-dependent",),
    )
    process_manager = ProcessManager(root)
    register_process_tools(registry, process_manager)
    registry.register_cleanup(process_manager.close)
    register_web_tools(registry)
    browser_controller = BrowserController()
    register_browser_tools(
        registry,
        browser_controller,
        availability_check=browser_tools_available,
    )
    registry.register_cleanup(browser_controller.close)
    return registry.filter_by_toolsets(toolsets) if toolsets else registry
