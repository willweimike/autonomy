from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ...models import Observation, RiskLevel

if TYPE_CHECKING:
    from ..registry import ToolRegistry


MAX_OUTPUT_CHARS = 64 * 1024
MAX_WAIT_SECONDS = 120


@dataclass
class ManagedProcess:
    process_id: str
    command: str
    cwd: Path
    process: subprocess.Popen[str]
    started_at: float
    output: str = ""
    reader_thread: threading.Thread | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def append_output(self, chunk: str) -> None:
        with self.lock:
            self.output = (self.output + chunk)[-MAX_OUTPUT_CHARS:]

    def output_text(self) -> str:
        with self.lock:
            return self.output


class ProcessManager:
    def __init__(self, workspace: str | Path, *, max_processes: int = 8):
        self.root = Path(workspace).resolve()
        self.max_processes = max_processes
        self._processes: dict[str, ManagedProcess] = {}
        self._lock = threading.RLock()

    def start(self, arguments: dict) -> Observation:
        command = str(arguments["command"]).strip()
        cwd = self._resolve_workdir(str(arguments.get("workdir", ".")))
        with self._lock:
            self._reap_exited_locked()
            active = [record for record in self._processes.values() if record.process.poll() is None]
            if len(active) >= self.max_processes:
                return Observation(
                    "",
                    False,
                    error=f"maximum active processes reached: {self.max_processes}",
                    evidence=("process_limit_reached",),
                )

        process_id = f"proc_{uuid.uuid4().hex[:12]}"
        try:
            process = subprocess.Popen(
                shlex.split(command),
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                start_new_session=(os.name != "nt"),
            )
        except Exception as exc:
            return Observation(
                "",
                False,
                error=f"{type(exc).__name__}: {exc}",
                evidence=(f"process_start_error:{type(exc).__name__}",),
            )

        record = ManagedProcess(
            process_id=process_id,
            command=command,
            cwd=cwd,
            process=process,
            started_at=time.time(),
        )
        with self._lock:
            self._processes[process_id] = record
        reader_thread = threading.Thread(target=self._read_output, args=(record,), daemon=True)
        record.reader_thread = reader_thread
        reader_thread.start()
        return Observation(
            "",
            True,
            output=json.dumps(self._status_payload(record), sort_keys=True),
            evidence=(f"process_started:{process_id}",),
            side_effects=("process", "command-dependent"),
        )

    def poll(self, arguments: dict) -> Observation:
        record = self._get(str(arguments["process_id"]))
        return Observation(
            "",
            True,
            output=json.dumps(self._status_payload(record), sort_keys=True),
            evidence=(f"process_poll:{record.process_id}:{self._status(record)}",),
        )

    def log(self, arguments: dict) -> Observation:
        record = self._get(str(arguments["process_id"]))
        max_chars = min(max(int(arguments.get("max_chars", 12000)), 1), MAX_OUTPUT_CHARS)
        output = record.output_text()
        truncated = len(output) > max_chars
        payload = {
            **self._status_payload(record, include_preview=False),
            "output": output[-max_chars:],
            "truncated": truncated,
        }
        return Observation(
            "",
            True,
            output=json.dumps(payload, sort_keys=True),
            evidence=(f"process_log:{record.process_id}",),
        )

    def wait(self, arguments: dict) -> Observation:
        record = self._get(str(arguments["process_id"]))
        timeout = min(max(int(arguments["timeout"]), 1), MAX_WAIT_SECONDS)
        timed_out = False
        try:
            record.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        if not timed_out and record.reader_thread:
            record.reader_thread.join(timeout=1)
        payload = self._status_payload(record)
        payload["timed_out"] = timed_out
        return Observation(
            "",
            True,
            output=json.dumps(payload, sort_keys=True),
            evidence=(f"process_wait:{record.process_id}:{self._status(record)}",),
            exit_code=record.process.returncode,
        )

    def stop(self, arguments: dict) -> Observation:
        record = self._get(str(arguments["process_id"]))
        was_running = record.process.poll() is None
        if was_running:
            self._terminate(record)
        if record.reader_thread:
            record.reader_thread.join(timeout=1)
        payload = self._status_payload(record)
        payload["was_running"] = was_running
        return Observation(
            "",
            True,
            output=json.dumps(payload, sort_keys=True),
            evidence=(f"process_stopped:{record.process_id}:{self._status(record)}",),
            side_effects=("process",),
            exit_code=record.process.returncode,
        )

    def close(self) -> None:
        with self._lock:
            records = list(self._processes.values())
        for record in records:
            if record.process.poll() is None:
                self._terminate(record)

    def validate_start(self, arguments: dict) -> None:
        command = str(arguments["command"]).strip()
        if not command:
            raise ValueError("command must not be empty")
        if not shlex.split(command):
            raise ValueError("command must include an executable")
        self._resolve_workdir(str(arguments.get("workdir", ".")))

    def validate_process_id(self, arguments: dict) -> None:
        process_id = str(arguments["process_id"]).strip()
        if not process_id:
            raise ValueError("process_id must not be empty")
        if not process_id.startswith("proc_"):
            raise ValueError("process_id is invalid")

    def validate_wait(self, arguments: dict) -> None:
        self.validate_process_id(arguments)
        if "timeout" not in arguments:
            raise ValueError("timeout is required")
        timeout = int(arguments["timeout"])
        if timeout < 1:
            raise ValueError("timeout must be at least 1")
        if timeout > MAX_WAIT_SECONDS:
            raise ValueError(f"timeout must be at most {MAX_WAIT_SECONDS}")

    def validate_log(self, arguments: dict) -> None:
        self.validate_process_id(arguments)
        max_chars = int(arguments.get("max_chars", 12000))
        if max_chars < 1:
            raise ValueError("max_chars must be at least 1")

    def _read_output(self, record: ManagedProcess) -> None:
        stream = record.process.stdout
        if stream is None:
            return
        try:
            for chunk in iter(stream.readline, ""):
                if not chunk:
                    break
                record.append_output(chunk)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _resolve_workdir(self, raw_path: str) -> Path:
        if not raw_path.strip():
            raise ValueError("workdir must not be empty")
        path = Path(raw_path)
        resolved = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError(f"workdir escapes workspace: {raw_path}")
        if not resolved.is_dir():
            raise ValueError(f"workdir is not a directory: {raw_path}")
        return resolved

    def _get(self, process_id: str) -> ManagedProcess:
        with self._lock:
            record = self._processes.get(process_id)
        if record is None:
            raise ValueError(f"unknown process_id: {process_id}")
        return record

    def _status(self, record: ManagedProcess) -> str:
        return "running" if record.process.poll() is None else "exited"

    def _status_payload(self, record: ManagedProcess, *, include_preview: bool = True) -> dict:
        status = self._status(record)
        payload = {
            "process_id": record.process_id,
            "pid": record.process.pid,
            "command": record.command,
            "cwd": str(record.cwd.relative_to(self.root)),
            "status": status,
            "uptime_seconds": round(max(time.time() - record.started_at, 0), 3),
            "exit_code": record.process.returncode,
        }
        if include_preview:
            output = record.output_text()
            payload["output_preview"] = output[-4000:]
            payload["output_truncated"] = len(output) > 4000
        return payload

    def _reap_exited_locked(self) -> None:
        for record in self._processes.values():
            record.process.poll()

    def _terminate(self, record: ManagedProcess) -> None:
        if record.process.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(record.process.pid), signal.SIGTERM)
            else:
                record.process.terminate()
            record.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(os.getpgid(record.process.pid), signal.SIGKILL)
            else:
                record.process.kill()
            record.process.wait(timeout=5)
        except ProcessLookupError:
            record.process.poll()
        if record.reader_thread:
            record.reader_thread.join(timeout=1)


def register_process_tools(registry: ToolRegistry, manager: ProcessManager) -> None:
    registry.register(
        "process.start",
        manager.start,
        manager.validate_start,
        description="Start a background process in the workspace.",
        toolset="terminal",
        argument_contract={"command": "string", "workdir": "string (optional)"},
        default_risk=RiskLevel.MEDIUM,
        side_effects=("process", "command-dependent"),
    )
    registry.register(
        "process.poll",
        manager.poll,
        manager.validate_process_id,
        description="Inspect a managed background process status and recent output.",
        toolset="terminal",
        argument_contract={"process_id": "string"},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "process.log",
        manager.log,
        manager.validate_log,
        description="Read buffered output for a managed background process.",
        toolset="terminal",
        argument_contract={"process_id": "string", "max_chars": "integer (optional)"},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "process.wait",
        manager.wait,
        manager.validate_wait,
        description="Wait for a managed background process to exit within a bounded timeout.",
        toolset="terminal",
        argument_contract={"process_id": "string", "timeout": "integer"},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "process.stop",
        manager.stop,
        manager.validate_process_id,
        description="Terminate a managed background process.",
        toolset="terminal",
        argument_contract={"process_id": "string"},
        default_risk=RiskLevel.MEDIUM,
        side_effects=("process",),
    )
