from __future__ import annotations

import os
import platform
import sys
from collections.abc import Sequence

from .config import ServerConfig, privileged_listen_ports


class ElevationError(RuntimeError):
    """Raised when the process cannot be relaunched with administrator rights."""


def should_elevate_server(config: ServerConfig) -> bool:
    if platform.system() == "Windows":
        return False
    return bool(privileged_listen_ports(config))


def is_admin() -> bool:
    try:
        from py_admin_launch import is_admin as imported_is_admin
    except ImportError:
        return False
    return bool(imported_is_admin())


def relaunch_once(argv: Sequence[str]) -> int:
    try:
        from py_admin_launch import launch
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ElevationError("py-admin-launch is not installed") from exc

    command = [sys.executable, "-m", "py_frp", *argv, "--elevation-attempted"]
    result = launch(command, cwd=os.getcwd(), wait=True)
    return 0 if result.returncode is None else int(result.returncode)
