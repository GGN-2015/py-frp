from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from collections.abc import Sequence


LOGGER = logging.getLogger(__name__)
RESTART_TARGET_VERSION_ENV = "PY_FRP_RESTART_TARGET_VERSION"
WINDOWS_RESTART_SUPERVISOR_ENV = "PY_FRP_WINDOWS_RESTART_SUPERVISOR"
WINDOWS_RESTART_EXIT_CODE = 75
WINDOWS_CHILD_SHUTDOWN_TIMEOUT = 5.0
WINDOWS_CHILD_TERMINATE_TIMEOUT = 2.0


def restart_current_command(
    argv: Sequence[str],
    *,
    expected_version: str | None = None,
) -> None:
    """Restart py-frp with the active Python and effective CLI arguments."""
    if not sys.executable:
        raise RuntimeError("cannot restart because the Python executable is unknown")
    if expected_version is not None:
        os.environ[RESTART_TARGET_VERSION_ENV] = expected_version

    command = [sys.executable, "-m", "py_frp", *argv]
    LOGGER.info("restarting command in current terminal: %s", command)
    if os.name != "nt":
        os.execv(sys.executable, command)
        return

    if os.environ.get(WINDOWS_RESTART_SUPERVISOR_ENV) == "1":
        LOGGER.info("requesting immediate restart from Windows supervisor")
        raise SystemExit(WINDOWS_RESTART_EXIT_CODE)
    _run_windows_supervisor(command)


def _run_windows_supervisor(command: list[str]) -> None:
    """Keep one terminal-attached wrapper while replacement children rotate."""
    environment = os.environ.copy()
    environment[WINDOWS_RESTART_SUPERVISOR_ENV] = "1"
    # asyncio.run installs a cancellation-oriented SIGINT handler. The serving
    # loop is already closed here, so restore ordinary KeyboardInterrupt
    # behavior while this synchronous supervisor waits for its child.
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        while True:
            process = subprocess.Popen(
                command,
                cwd=os.getcwd(),
                env=environment,
            )
            try:
                returncode = process.wait()
            except KeyboardInterrupt:
                _finish_interrupted_windows_child(process)
                raise SystemExit(130) from None
            if returncode != WINDOWS_RESTART_EXIT_CODE:
                raise SystemExit(returncode)

            # The child has already preserved stable token/TLS/fingerprint state.
            # Its target-version environment mutation cannot flow to this parent,
            # so do not pass the previous target to the next replacement.
            environment.pop(RESTART_TARGET_VERSION_ENV, None)
            LOGGER.info("Windows supervisor received restart request; restarting now")
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def _finish_interrupted_windows_child(process: subprocess.Popen[bytes]) -> None:
    """Allow shared-console Ctrl+C cleanup, then force a stuck child down."""
    LOGGER.info("interrupt received; waiting for replacement process to stop")
    previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        try:
            process.wait(timeout=WINDOWS_CHILD_SHUTDOWN_TIMEOUT)
            return
        except subprocess.TimeoutExpired:
            LOGGER.warning("replacement process did not stop after Ctrl+C; terminating")

        process.terminate()
        try:
            process.wait(timeout=WINDOWS_CHILD_TERMINATE_TIMEOUT)
            return
        except subprocess.TimeoutExpired:
            LOGGER.error("replacement process ignored termination; killing")

        process.kill()
        process.wait()
    finally:
        signal.signal(signal.SIGINT, previous_handler)
