from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPERVISED_CHILD_ENV = "PY_FRP_SUPERVISED_CHILD"
SUPERVISOR_ROOT_ENV = "PY_FRP_SUPERVISOR_ROOT"
SUPERVISOR_GENERATION_ENV = "PY_FRP_SUPERVISOR_GENERATION"
LEGACY_WINDOWS_SUPERVISOR_ENV = "PY_FRP_WINDOWS_RESTART_SUPERVISOR"
RESTART_ENV_PREFIX = "PY_FRP_RESTART_"
CHILD_RESTART_EXIT_CODE = 75


@dataclass(frozen=True)
class ChildStatus:
    version: str
    pid: int


@dataclass(frozen=True)
class RestartState:
    target_version: str
    environment: dict[str, str]


class SupervisorChannel:
    """Parent-side file channel shared with one child generation at a time."""

    def __init__(self, root: Path):
        self.root = root
        self.status_path = root / "child-status.json"
        self.command_path = root / "command.json"
        self.restart_state_path = root / "restart-state.json"

    def prepare_child_environment(
        self,
        base_environment: dict[str, str],
        generation: str,
    ) -> dict[str, str]:
        for path in (self.status_path, self.command_path, self.restart_state_path):
            path.unlink(missing_ok=True)
        environment = base_environment.copy()
        environment[SUPERVISED_CHILD_ENV] = "1"
        environment[SUPERVISOR_ROOT_ENV] = str(self.root)
        environment[SUPERVISOR_GENERATION_ENV] = generation
        return environment

    def child_status(self, generation: str) -> ChildStatus | None:
        payload = _read_json(self.status_path)
        if payload is None or payload.get("generation") != generation:
            return None
        version = payload.get("version")
        pid = payload.get("pid")
        if not isinstance(version, str) or not isinstance(pid, int):
            return None
        return ChildStatus(version=version, pid=pid)

    def request_restart(self, generation: str, target_version: str) -> None:
        _write_json(
            self.command_path,
            {
                "generation": generation,
                "command": "restart",
                "target_version": target_version,
            },
        )

    def restart_state(self, generation: str) -> RestartState | None:
        payload = _read_json(self.restart_state_path)
        if payload is None or payload.get("generation") != generation:
            return None
        target = payload.get("target_version")
        raw_environment = payload.get("environment")
        if not isinstance(target, str) or not isinstance(raw_environment, dict):
            return None
        environment = {
            key: value
            for key, value in raw_environment.items()
            if isinstance(key, str)
            and isinstance(value, str)
            and key.startswith(RESTART_ENV_PREFIX)
        }
        return RestartState(target_version=target, environment=environment)


def create_supervisor_directory() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="py-frp-supervisor-")


def is_supervised_child() -> bool:
    return (
        os.environ.get(SUPERVISED_CHILD_ENV) == "1"
        and bool(os.environ.get(SUPERVISOR_ROOT_ENV))
        and bool(os.environ.get(SUPERVISOR_GENERATION_ENV))
    )


def publish_child_status(version: str) -> None:
    channel = _child_channel()
    if channel is None:
        return
    root, generation = channel
    _write_json(
        root / "child-status.json",
        {
            "generation": generation,
            "version": version,
            "pid": os.getpid(),
        },
    )


def supervisor_restart_target() -> str | None:
    channel = _child_channel()
    if channel is None:
        return None
    root, generation = channel
    payload = _read_json(root / "command.json")
    if (
        payload is None
        or payload.get("generation") != generation
        or payload.get("command") != "restart"
    ):
        return None
    target = payload.get("target_version")
    return target if isinstance(target, str) and target else None


def publish_restart_state(target_version: str) -> bool:
    channel = _child_channel()
    if channel is None:
        return False
    root, generation = channel
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(RESTART_ENV_PREFIX)
    }
    _write_json(
        root / "restart-state.json",
        {
            "generation": generation,
            "target_version": target_version,
            "environment": environment,
        },
    )
    return True


def clear_internal_environment(environment: dict[str, str]) -> dict[str, str]:
    """Remove an inherited child channel before creating a new supervisor."""
    return {
        key: value
        for key, value in environment.items()
        if key not in {
            SUPERVISED_CHILD_ENV,
            SUPERVISOR_ROOT_ENV,
            SUPERVISOR_GENERATION_ENV,
            LEGACY_WINDOWS_SUPERVISOR_ENV,
        }
    }


def replace_restart_environment(
    environment: dict[str, str],
    restart_environment: dict[str, str],
) -> None:
    for key in tuple(environment):
        if key.startswith(RESTART_ENV_PREFIX):
            environment.pop(key, None)
    environment.update(restart_environment)


def _child_channel() -> tuple[Path, str] | None:
    if not is_supervised_child():
        return None
    root = Path(os.environ[SUPERVISOR_ROOT_ENV])
    generation = os.environ[SUPERVISOR_GENERATION_ENV]
    return root, generation


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, separators=(",", ":"), sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    os.replace(temporary_path, path)
