from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from py_frp.restart import request_restart
from py_frp.supervisor import (
    RESTART_FAILURE_LIMIT,
    LaunchResult,
    MonitorResult,
    ProcessSupervisor,
)
from py_frp.supervisor_ipc import (
    CHILD_RESTART_EXIT_CODE,
    LEGACY_WINDOWS_SUPERVISOR_ENV,
    RESTART_ENV_PREFIX,
    SUPERVISED_CHILD_ENV,
    SUPERVISOR_GENERATION_ENV,
    SUPERVISOR_ROOT_ENV,
    ChildStatus,
    RestartState,
    SupervisorChannel,
    clear_internal_environment,
    publish_child_status,
    publish_restart_state,
    supervisor_restart_target,
)


class SupervisorChannelTests(unittest.TestCase):
    def test_child_status_command_and_restart_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            channel = SupervisorChannel(Path(directory))
            environment = channel.prepare_child_environment({}, "generation-1")
            environment[f"{RESTART_ENV_PREFIX}POOL_TOKEN"] = "same-token"
            environment["UNRELATED_SECRET"] = "must-not-cross"

            with mock.patch.dict(os.environ, environment, clear=True):
                publish_child_status("0.6.0")
                status = channel.child_status("generation-1")
                self.assertIsNotNone(status)
                assert status is not None
                self.assertEqual(status.version, "0.6.0")
                self.assertEqual(status.pid, os.getpid())

                self.assertIsNone(supervisor_restart_target())
                channel.request_restart("generation-1", "0.7.0")
                self.assertEqual(supervisor_restart_target(), "0.7.0")
                self.assertTrue(publish_restart_state("0.7.0"))

            state = channel.restart_state("generation-1")

        self.assertEqual(
            state,
            RestartState(
                target_version="0.7.0",
                environment={f"{RESTART_ENV_PREFIX}POOL_TOKEN": "same-token"},
            ),
        )

    def test_stale_generation_cannot_control_current_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            channel = SupervisorChannel(Path(directory))
            environment = channel.prepare_child_environment({}, "current")
            channel.request_restart("stale", "9.9.9")
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertIsNone(supervisor_restart_target())

    def test_clear_internal_environment_preserves_compatibility_state(self) -> None:
        token_key = f"{RESTART_ENV_PREFIX}POOL_TOKEN"
        environment = {
            SUPERVISED_CHILD_ENV: "1",
            SUPERVISOR_ROOT_ENV: "temporary",
            SUPERVISOR_GENERATION_ENV: "generation",
            LEGACY_WINDOWS_SUPERVISOR_ENV: "1",
            token_key: "same-token",
            "PATH": "bin",
        }

        cleaned = clear_internal_environment(environment)

        self.assertEqual(cleaned, {token_key: "same-token", "PATH": "bin"})

    def test_restart_request_requires_supervisor(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "requires the py-frp supervisor"):
                request_restart("0.7.0")

    def test_restart_request_returns_state_and_internal_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            channel = SupervisorChannel(Path(directory))
            environment = channel.prepare_child_environment({}, "generation")
            with mock.patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(SystemExit) as captured:
                    request_restart("0.7.0")

            state = channel.restart_state("generation")

        self.assertEqual(captured.exception.code, CHILD_RESTART_EXIT_CODE)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.target_version, "0.7.0")


class ProcessSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.supervisor = ProcessSupervisor(
            ["server", "--port-pool", "6000"],
            auto_restart=True,
            update_interval=0.01,
        )

    def test_three_fast_failures_suppress_only_that_target(self) -> None:
        with (
            mock.patch("py_frp.supervisor.time.monotonic", return_value=10.0),
            self.assertLogs("py_frp.supervisor", level="WARNING") as captured,
        ):
            for number in range(RESTART_FAILURE_LIMIT):
                suppressed = self.supervisor._record_failure(
                    "0.7.0",
                    f"failure {number + 1}",
                )

        self.assertTrue(suppressed)
        self.assertEqual(self.supervisor.suppressed_target, "0.7.0")
        self.assertIn("suppression will clear", captured.output[-1])

        self.supervisor._clear_suppression_if_version_changed("0.7.0")
        self.assertEqual(self.supervisor.suppressed_target, "0.7.0")

        self.supervisor._clear_suppression_if_version_changed("0.8.0")
        self.assertIsNone(self.supervisor.suppressed_target)
        self.assertEqual(self.supervisor.failures, {})

    def test_old_child_stays_running_after_wrong_version_is_suppressed(self) -> None:
        child = mock.Mock()
        child.wait.side_effect = subprocess.TimeoutExpired("child", 0.01)
        self.supervisor.child = child
        self.supervisor.suppressed_target = "0.7.0"

        def installed() -> str:
            if child.wait.call_count >= 2:
                raise KeyboardInterrupt
            return "0.7.0"

        with mock.patch("py_frp.supervisor.installed_version", side_effect=installed):
            with self.assertRaises(KeyboardInterrupt):
                self.supervisor._monitor_child(
                    mock.Mock(),
                    "generation",
                    "0.6.0",
                )

        self.assertIs(self.supervisor.child, child)

    def test_parent_rotates_children_without_replacing_itself(self) -> None:
        channel = mock.Mock()
        channel.restart_state.return_value = RestartState(
            target_version="0.7.0",
            environment={f"{RESTART_ENV_PREFIX}POOL_TOKEN": "same-token"},
        )
        first_status = ChildStatus(version="0.6.0", pid=100)
        second_status = ChildStatus(version="0.7.0", pid=101)

        with (
            mock.patch.object(
                self.supervisor,
                "_launch_child",
                side_effect=(
                    LaunchResult(first_status, None),
                    LaunchResult(second_status, None),
                ),
            ) as launch,
            mock.patch.object(
                self.supervisor,
                "_monitor_child",
                side_effect=(
                    MonitorResult(restart_target="0.7.0"),
                    MonitorResult(returncode=0),
                ),
            ),
        ):
            result = self.supervisor._run_children(channel)

        self.assertEqual(result, 0)
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(
            self.supervisor.environment[f"{RESTART_ENV_PREFIX}POOL_TOKEN"],
            "same-token",
        )

    def test_parent_requests_restart_when_loaded_and_installed_versions_differ(self) -> None:
        child = mock.Mock()
        child.wait.side_effect = (
            subprocess.TimeoutExpired("child", 0.01),
            CHILD_RESTART_EXIT_CODE,
        )
        self.supervisor.child = child
        channel = mock.Mock()

        with mock.patch("py_frp.supervisor.installed_version", return_value="0.7.0"):
            result = self.supervisor._monitor_child(
                channel,
                "generation",
                "0.6.0",
            )

        self.assertEqual(result.restart_target, "0.7.0")
        channel.request_restart.assert_called_once_with("generation", "0.7.0")

    def test_ctrl_c_waits_for_child_and_returns_130(self) -> None:
        child = mock.Mock()
        child.wait.return_value = 130
        self.supervisor.child = child

        with (
            mock.patch.object(
                self.supervisor,
                "_run_children",
                side_effect=KeyboardInterrupt,
            ),
            mock.patch("py_frp.supervisor.create_supervisor_directory"),
        ):
            result = self.supervisor.run()

        self.assertEqual(result, 130)
        child.wait.assert_called_once()
        self.assertIsNone(self.supervisor.child)

    def test_startup_log_does_not_expose_cli_token(self) -> None:
        supervisor = ProcessSupervisor(
            [
                "client",
                "--server",
                "example.com:7000",
                "--token",
                "never-log-this-token",
            ],
            auto_restart=False,
            update_interval=5.0,
        )
        with (
            mock.patch.object(supervisor, "_run_children", return_value=0),
            self.assertLogs("py_frp.supervisor", level="INFO") as captured,
        ):
            result = supervisor.run()

        self.assertEqual(result, 0)
        self.assertNotIn("never-log-this-token", "\n".join(captured.output))


class SupervisorProcessTests(unittest.TestCase):
    def test_one_terminal_interrupt_stops_supervisor_child_and_listener(self) -> None:
        control_port = _unused_tcp_port()
        pool_port = _unused_tcp_port()
        process_options: dict[str, object] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "py_frp",
                "server",
                "--bind-host",
                "127.0.0.1",
                "--bind-port",
                str(control_port),
                "--port-pool",
                str(pool_port),
                "--no-auto-elevate",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **process_options,
        )
        try:
            _wait_for_listener(control_port, process)
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGINT)
            returncode = process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            stdout, stderr = process.communicate()

        self.assertEqual(returncode, 130, f"stdout:\n{stdout}\nstderr:\n{stderr}")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", control_port), timeout=0.2)


def _unused_tcp_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_listener(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"server exited before listening ({process.returncode})\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"server did not listen on 127.0.0.1:{port}")


if __name__ == "__main__":
    unittest.main()
