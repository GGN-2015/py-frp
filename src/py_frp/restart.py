from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from .supervisor_ipc import (
    CHILD_RESTART_EXIT_CODE,
    RESTART_ENV_PREFIX,
    publish_restart_state,
)


LOGGER = logging.getLogger(__name__)
RESTART_TARGET_VERSION_ENV = f"{RESTART_ENV_PREFIX}TARGET_VERSION"


def request_restart(expected_version: str) -> None:
    """Return preserved compatibility state and restart control to the parent."""
    os.environ[RESTART_TARGET_VERSION_ENV] = expected_version
    if not publish_restart_state(expected_version):
        raise RuntimeError("automatic restart requires the py-frp supervisor")
    LOGGER.info("requesting restart from supervisor for version %s", expected_version)
    raise SystemExit(CHILD_RESTART_EXIT_CODE)


def restart_current_command(
    argv: Sequence[str],
    *,
    expected_version: str | None = None,
) -> None:
    """Compatibility wrapper for callers of the pre-supervisor restart API."""
    del argv
    if expected_version is None:
        raise RuntimeError("a restart target version is required")
    request_restart(expected_version)
