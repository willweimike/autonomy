from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import yaml

from ...models import Observation, RiskLevel
from ..redaction import redact_sensitive_text
from ..registry import ToolRegistry


_DEFAULT_OUTPUT_CHARS = 50_000
_MAX_OUTPUT_CHARS = 200_000
_DEFAULT_GIT_LOG_LIMIT = 10
_MAX_GIT_LOG_LIMIT = 100
_MANIFESTS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
)
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


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _deny_secret_path(path: Path) -> None:
    if path.name.lower() in _SECRET_ENV_FILENAMES:
        raise ValueError(f"access denied: {path.name} is a secret-bearing environment file")


def _coerce_limit(arguments: dict, *, default: int = _DEFAULT_OUTPUT_CHARS) -> int:
    limit = int(arguments.get("max_chars", default))
    if limit < 1:
        raise ValueError("max_chars must be at least 1")
    return min(limit, _MAX_OUTPUT_CHARS)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + "\n[truncated]", True


def _json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True)


def _run_git(root: Path, args: list[str], *, max_chars: int, timeout: int = 20) -> Observation:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    stdout, stdout_redacted = redact_sensitive_text(completed.stdout)
    stderr, stderr_redacted = redact_sensitive_text(completed.stderr)
    output, truncated_stdout = _truncate(stdout, max_chars)
    error, truncated_stderr = _truncate(stderr, max_chars)
    evidence = ["git"]
    if stdout_redacted or stderr_redacted:
        evidence.append("redacted")
    if truncated_stdout or truncated_stderr:
        evidence.append("truncated")
    return Observation(
        action_id="",
        succeeded=completed.returncode == 0,
        output=output,
        error=error,
        evidence=tuple(evidence),
        exit_code=completed.returncode,
    )


def _git_path_args(root: Path, raw_path: str | None) -> list[str]:
    if not raw_path:
        return []
    path = _resolve_inside(root, raw_path)
    _deny_secret_path(path)
    return ["--", _relative(root, path)]


def _existing_file(root: Path, raw_path: str) -> Path:
    path = _resolve_inside(root, raw_path)
    _deny_secret_path(path)
    if not path.is_file():
        raise ValueError(f"file does not exist: {_relative(root, path)}")
    return path


def _manifest_commands(root: Path) -> tuple[list[str], dict[str, list[str]]]:
    manifests: list[str] = []
    commands: dict[str, list[str]] = {"test": [], "dev": [], "build": []}
    for name in _MANIFESTS:
        if (root / name).exists():
            manifests.append(name)
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            package = {}
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        if isinstance(scripts, dict):
            if "test" in scripts:
                commands["test"].append("npm test")
            if "dev" in scripts:
                commands["dev"].append("npm run dev")
            if "build" in scripts:
                commands["build"].append("npm run build")
    if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file():
        commands["test"].append("python3.13 -m pytest")
    elif (root / "setup.py").is_file() or (root / "setup.cfg").is_file():
        commands["test"].append("python3.13 -m pytest")
    if (root / "Cargo.toml").is_file():
        commands["test"].append("cargo test")
    if (root / "go.mod").is_file():
        commands["test"].append("go test ./...")
    return manifests, {key: value for key, value in commands.items() if value}


def register_project_tools(registry: ToolRegistry, root: Path) -> None:
    root = Path(root).resolve()

    def git_status(arguments: dict) -> Observation:
        return _run_git(root, ["status", "--short", "--branch"], max_chars=_coerce_limit(arguments))

    def validate_git_status(arguments: dict) -> None:
        _coerce_limit(arguments)

    def git_diff(arguments: dict) -> Observation:
        args = ["diff", "--no-ext-diff"]
        if bool(arguments.get("staged", False)):
            args.append("--cached")
        args.extend(_git_path_args(root, arguments.get("path")))
        return _run_git(root, args, max_chars=_coerce_limit(arguments))

    def validate_git_diff(arguments: dict) -> None:
        _coerce_limit(arguments)
        if "path" in arguments:
            _git_path_args(root, arguments.get("path"))

    def git_log(arguments: dict) -> Observation:
        limit = int(arguments.get("limit", _DEFAULT_GIT_LOG_LIMIT))
        if limit < 1:
            raise ValueError("limit must be at least 1")
        limit = min(limit, _MAX_GIT_LOG_LIMIT)
        args = ["log", f"-{limit}", "--oneline", "--decorate", "--no-ext-diff"]
        args.extend(_git_path_args(root, arguments.get("path")))
        return _run_git(root, args, max_chars=_coerce_limit(arguments))

    def validate_git_log(arguments: dict) -> None:
        int(arguments.get("limit", _DEFAULT_GIT_LOG_LIMIT))
        _coerce_limit(arguments)
        if "path" in arguments:
            _git_path_args(root, arguments.get("path"))

    def git_show(arguments: dict) -> Observation:
        revision = str(arguments.get("revision", "HEAD")).strip()
        if not revision:
            raise ValueError("revision must not be empty")
        args = ["show", "--stat", "--summary", "--no-ext-diff", revision]
        args.extend(_git_path_args(root, arguments.get("path")))
        return _run_git(root, args, max_chars=_coerce_limit(arguments))

    def validate_git_show(arguments: dict) -> None:
        if not str(arguments.get("revision", "HEAD")).strip():
            raise ValueError("revision must not be empty")
        _coerce_limit(arguments)
        if "path" in arguments:
            _git_path_args(root, arguments.get("path"))

    def parse_json(arguments: dict) -> Observation:
        path = _existing_file(root, str(arguments["path"]))
        parsed = json.loads(path.read_text(encoding="utf-8"))
        payload = {
            "path": _relative(root, path),
            "parsed": parsed,
        }
        output, truncated = _truncate(_json(payload), _coerce_limit(arguments))
        return Observation("", True, output=output, evidence=("json", "truncated") if truncated else ("json",))

    def validate_parse_file(arguments: dict) -> None:
        _existing_file(root, str(arguments["path"]))
        _coerce_limit(arguments)

    def parse_yaml(arguments: dict) -> Observation:
        path = _existing_file(root, str(arguments["path"]))
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        payload = {
            "path": _relative(root, path),
            "parsed": parsed,
        }
        output, truncated = _truncate(_json(payload), _coerce_limit(arguments))
        return Observation("", True, output=output, evidence=("yaml", "truncated") if truncated else ("yaml",))

    def detect_project(arguments: dict) -> Observation:
        del arguments
        manifests, commands = _manifest_commands(root)
        payload = {
            "manifests": manifests,
            "commands": commands,
        }
        return Observation("", True, output=_json(payload), evidence=("project-detect",))

    def discover_python_tests(arguments: dict) -> Observation:
        del arguments
        commands: list[str] = []
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError:
                data = {}
            if data.get("tool", {}).get("pytest", {}) or data.get("tool", {}).get("pytest.ini_options", {}) or (root / "tests").exists():
                commands.append("python3.13 -m pytest")
        elif (root / "pytest.ini").is_file() or (root / "tests").exists():
            commands.append("python3.13 -m pytest")
        if not commands:
            commands.append("python3.13 -m unittest discover")
        return Observation("", True, output=_json({"commands": commands}), evidence=("python-tests",))

    registry.register(
        "git.status",
        git_status,
        validate_git_status,
        description="Return bounded read-only git status for the workspace.",
        toolset="project",
        argument_contract={"max_chars": "integer max output chars, default 50000 (optional)"},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "git.diff",
        git_diff,
        validate_git_diff,
        description="Return bounded read-only git diff for the workspace or one path.",
        toolset="project",
        argument_contract={
            "path": "string workspace path (optional)",
            "staged": "boolean inspect staged diff (optional)",
            "max_chars": "integer max output chars, default 50000 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "git.log",
        git_log,
        validate_git_log,
        description="Return bounded recent git commits for the workspace or one path.",
        toolset="project",
        argument_contract={
            "path": "string workspace path (optional)",
            "limit": "integer commits, default 10, max 100 (optional)",
            "max_chars": "integer max output chars, default 50000 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "git.show",
        git_show,
        validate_git_show,
        description="Return bounded git show output for a revision or one path.",
        toolset="project",
        argument_contract={
            "revision": "string git revision, default HEAD (optional)",
            "path": "string workspace path (optional)",
            "max_chars": "integer max output chars, default 50000 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "json.parse",
        parse_json,
        validate_parse_file,
        description="Parse one workspace JSON file and return bounded JSON output.",
        toolset="project",
        argument_contract={
            "path": "string JSON file path",
            "max_chars": "integer max output chars, default 50000 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "yaml.parse",
        parse_yaml,
        validate_parse_file,
        description="Parse one workspace YAML file and return bounded JSON output.",
        toolset="project",
        argument_contract={
            "path": "string YAML file path",
            "max_chars": "integer max output chars, default 50000 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "project.detect",
        detect_project,
        description="Inspect common manifests and infer project commands without executing them.",
        toolset="project",
        argument_contract={},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "python.test_discover",
        discover_python_tests,
        description="Infer Python test commands without executing tests.",
        toolset="project",
        argument_contract={},
        default_risk=RiskLevel.LOW,
    )
