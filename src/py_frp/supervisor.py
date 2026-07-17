import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, Iterator, List, Optional

from .supervisor_ipc import (
    CHILD_RESTART_EXIT_CODE,
    RESTART_ENV_PREFIX,
    ChildStatus,
    SupervisorChannel,
    clear_internal_environment,
    create_supervisor_directory,
    replace_restart_environment,
)
from .update import installed_version


LOGGER = logging.getLogger(__name__)
STARTUP_REPORT_TIMEOUT = 10.0
STARTUP_STABILITY_TIME = 1.0
CHILD_SHUTDOWN_TIMEOUT = 5.0
CHILD_TERMINATE_TIMEOUT = 2.0
RESTART_FAILURE_LIMIT = 3
RESTART_FAILURE_WINDOW = 30.0
SUPERVISOR_POLL_INTERVAL = 0.1


@dataclass(frozen=True)
class LaunchResult:
    status: Optional[ChildStatus]
    returncode: Optional[int]

    @property
    def stable(self) -> bool:
        return self.status is not None and self.returncode is None


@dataclass(frozen=True)
class MonitorResult:
    returncode: Optional[int] = None
    restart_target: Optional[str] = None


class ProcessSupervisor:
    """Own the terminal and rotate exactly one py-frp business child."""

    def __init__(
        self,
        argv: List[str],
        *,
        auto_restart: bool,
        update_interval: float,
    ):
        if not sys.executable:
            raise RuntimeError("cannot start supervisor because Python is unknown")
        self.command = [sys.executable, "-m", "py_frp", *argv]
        self.role = argv[0] if argv else "py-frp"
        self.auto_restart = auto_restart
        self.update_interval = update_interval
        self.environment = clear_internal_environment(os.environ.copy())
        self.environment.pop(f"{RESTART_ENV_PREFIX}TARGET_VERSION", None)
        self.failures: Dict[str, Deque[float]] = defaultdict(deque)
        self.suppressed_target: Optional[str] = None
        self.child: Optional[subprocess.Popen] = None

    def run(self) -> int:
        with _windows_break_as_keyboard_interrupt():
            LOGGER.info(
                "supervisor starting %s child with Python %s",
                self.role,
                sys.executable,
            )
            try:
                with create_supervisor_directory() as directory:
                    channel = SupervisorChannel(Path(directory))
                    return self._run_children(channel)
            except KeyboardInterrupt:
                with _ignore_console_interrupts():
                    self._finish_interrupted_child()
                return 130

    def _run_children(self, channel: SupervisorChannel) -> int:
        target: Optional[str] = None
        while True:
            generation = uuid.uuid4().hex
            launch = self._launch_child(channel, generation)

            if launch.status is None:
                returncode = 1 if launch.returncode is None else launch.returncode
                if target is None:
                    return returncode
                if self._record_failure(target, "child exited before reporting its version"):
                    target = self._wait_until_suppression_clears(target)
                continue

            child_version = launch.status.version
            version_mismatch = target is not None and child_version != target
            if version_mismatch:
                self._record_failure(
                    target,
                    f"child loaded {child_version} instead of {target}",
                )

            if not launch.stable:
                returncode = 1 if launch.returncode is None else launch.returncode
                if target is None:
                    return returncode
                if not version_mismatch and self._record_failure(
                    target,
                    "child exited during startup validation",
                ):
                    target = self._wait_until_suppression_clears(target)
                continue

            if target is not None and not version_mismatch:
                self._clear_failures(target)
                target = None

            result = self._monitor_child(
                channel,
                generation,
                child_version,
            )
            if result.restart_target is None:
                return 1 if result.returncode is None else result.returncode

            state = channel.restart_state(generation)
            if state is None:
                LOGGER.error("restarting child did not return compatibility state")
                return 1
            replace_restart_environment(self.environment, state.environment)
            self.environment.pop(f"{RESTART_ENV_PREFIX}TARGET_VERSION", None)
            target = state.target_version or result.restart_target

    def _launch_child(
        self,
        channel: SupervisorChannel,
        generation: str,
    ) -> LaunchResult:
        environment = channel.prepare_child_environment(self.environment, generation)
        self.child = subprocess.Popen(
            self.command,
            cwd=os.getcwd(),
            env=environment,
        )
        deadline = time.monotonic() + STARTUP_REPORT_TIMEOUT
        while time.monotonic() < deadline:
            status = channel.child_status(generation)
            if status is not None:
                try:
                    returncode = self.child.wait(timeout=STARTUP_STABILITY_TIME)
                except subprocess.TimeoutExpired:
                    return LaunchResult(status=status, returncode=None)
                self.child = None
                return LaunchResult(status=status, returncode=returncode)
            returncode = self.child.poll()
            if returncode is not None:
                self.child = None
                return LaunchResult(status=None, returncode=returncode)
            time.sleep(SUPERVISOR_POLL_INTERVAL)

        LOGGER.error("child did not report startup within %.1fs", STARTUP_REPORT_TIMEOUT)
        self._terminate_child()
        return LaunchResult(status=None, returncode=1)

    def _monitor_child(
        self,
        channel: SupervisorChannel,
        generation: str,
        child_version: str,
    ) -> MonitorResult:
        assert self.child is not None
        next_check = 0.0
        requested_target: Optional[str] = None
        while True:
            try:
                returncode = self.child.wait(timeout=SUPERVISOR_POLL_INTERVAL)
            except subprocess.TimeoutExpired:
                returncode = None
            if returncode is not None:
                self.child = None
                if returncode == CHILD_RESTART_EXIT_CODE:
                    return MonitorResult(restart_target=requested_target or "unknown")
                return MonitorResult(returncode=returncode)

            now = time.monotonic()
            if not self.auto_restart or now < next_check:
                continue
            next_check = now + self.update_interval
            current = installed_version()
            if current is None:
                continue
            self._clear_suppression_if_version_changed(current)
            if current == child_version or current == self.suppressed_target:
                continue
            if current != requested_target:
                LOGGER.info(
                    "supervisor detected installed package change from %s to %s",
                    child_version,
                    current,
                )
                channel.request_restart(generation, current)
                requested_target = current

    def _record_failure(self, target: str, reason: str) -> bool:
        now = time.monotonic()
        failures = self.failures[target]
        cutoff = now - RESTART_FAILURE_WINDOW
        while failures and failures[0] < cutoff:
            failures.popleft()
        failures.append(now)
        count = len(failures)
        LOGGER.warning(
            "replacement failure for version %s (%d/%d): %s",
            target,
            count,
            RESTART_FAILURE_LIMIT,
            reason,
        )
        if count < RESTART_FAILURE_LIMIT:
            return False
        self.suppressed_target = target
        LOGGER.warning(
            "suppressing restart toward version %s after %d failures in %.0fs; "
            "suppression will clear when the installed version changes",
            target,
            count,
            RESTART_FAILURE_WINDOW,
        )
        return True

    def _clear_failures(self, target: str) -> None:
        self.failures.pop(target, None)
        if self.suppressed_target == target:
            self.suppressed_target = None

    def _clear_suppression_if_version_changed(self, current: str) -> None:
        if self.suppressed_target is None or current == self.suppressed_target:
            return
        LOGGER.info(
            "installed version changed from suppressed target %s to %s; "
            "restart suppression cleared",
            self.suppressed_target,
            current,
        )
        self.failures.clear()
        self.suppressed_target = None

    def _wait_until_suppression_clears(self, target: str) -> str:
        while True:
            time.sleep(self.update_interval)
            current = installed_version()
            if current is None or current == target:
                continue
            self._clear_suppression_if_version_changed(current)
            return current

    def _finish_interrupted_child(self) -> None:
        if self.child is None:
            return
        LOGGER.info("interrupt received; waiting for child process to stop")
        try:
            self.child.wait(timeout=CHILD_SHUTDOWN_TIMEOUT)
            self.child = None
            return
        except subprocess.TimeoutExpired:
            LOGGER.warning("child did not stop after Ctrl+C; terminating")
        self._terminate_child()

    def _terminate_child(self) -> None:
        if self.child is None:
            return
        self.child.terminate()
        try:
            self.child.wait(timeout=CHILD_TERMINATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            LOGGER.error("child ignored termination; killing")
            self.child.kill()
            self.child.wait()
        finally:
            self.child = None


def run_supervisor(
    argv: List[str],
    *,
    auto_restart: bool,
    update_interval: float,
) -> int:
    return ProcessSupervisor(
        argv,
        auto_restart=auto_restart,
        update_interval=update_interval,
    ).run()


@contextmanager
def _windows_break_as_keyboard_interrupt() -> Iterator[None]:
    break_signal = getattr(signal, "SIGBREAK", None)
    if break_signal is None:
        yield
        return
    previous_handler = signal.signal(break_signal, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(break_signal, previous_handler)


@contextmanager
def _ignore_console_interrupts() -> Iterator[None]:
    signals = [signal.SIGINT]
    break_signal = getattr(signal, "SIGBREAK", None)
    if break_signal is not None:
        signals.append(break_signal)
    previous_handlers = [
        (interrupt, signal.signal(interrupt, signal.SIG_IGN))
        for interrupt in signals
    ]
    try:
        yield
    finally:
        for interrupt, previous_handler in reversed(previous_handlers):
            signal.signal(interrupt, previous_handler)
