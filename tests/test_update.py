from __future__ import annotations

import asyncio
import os
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from py_frp import cli
from py_frp.restart import (
    RESTART_TARGET_VERSION_ENV,
    WINDOWS_RESTART_EXIT_CODE,
    WINDOWS_RESTART_SUPERVISOR_ENV,
    restart_current_command,
)
from py_frp.update import (
    PACKAGE_DIRECTORY,
    VersionChange,
    installed_version,
    run_until_version_change,
    wait_for_version_change,
)


class UpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_version_change_ignores_missing_metadata(self) -> None:
        versions = iter((None, "0.3.0", "0.4.0"))

        change = await wait_for_version_change(
            initial_version="0.3.0",
            interval=0.001,
            version_reader=lambda: next(versions),
        )

        self.assertEqual(change, VersionChange(previous="0.3.0", current="0.4.0"))

    async def test_version_change_cancels_runtime(self) -> None:
        cancelled = asyncio.Event()
        versions = iter(("0.3.0", "0.4.0"))

        async def runtime() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        result, change = await run_until_version_change(
            runtime(),
            initial_version="0.3.0",
            interval=0.001,
            version_reader=lambda: next(versions),
        )

        self.assertIsNone(result)
        self.assertEqual(change, VersionChange(previous="0.3.0", current="0.4.0"))
        self.assertTrue(cancelled.is_set())

    async def test_runtime_completion_stops_version_monitor(self) -> None:
        async def runtime() -> int:
            return 7

        result, change = await run_until_version_change(
            runtime(),
            initial_version="0.3.0",
            interval=60,
            version_reader=lambda: "0.3.0",
        )

        self.assertEqual(result, 7)
        self.assertIsNone(change)

    async def test_update_waits_until_restart_state_is_ready(self) -> None:
        ready = asyncio.Event()

        async def runtime() -> None:
            await asyncio.Event().wait()

        monitored = asyncio.create_task(
            run_until_version_change(
                runtime(),
                initial_version="0.3.0",
                interval=0.001,
                version_reader=lambda: "0.4.0",
                restart_ready=ready.wait,
            )
        )
        await asyncio.sleep(0.01)
        self.assertFalse(monitored.done())

        ready.set()
        result, change = await asyncio.wait_for(monitored, timeout=1)

        self.assertIsNone(result)
        self.assertEqual(change, VersionChange(previous="0.3.0", current="0.4.0"))

    def test_installed_version_returns_none_when_distribution_is_absent(self) -> None:
        with mock.patch(
            "py_frp.update.metadata.distributions",
            return_value=(),
        ):
            self.assertIsNone(installed_version())

    def test_installed_version_uses_metadata_for_loaded_package_path(self) -> None:
        distributions = (
            _distribution("9.0.0", Path("C:/shadowed/site-packages/py_frp")),
            _distribution("0.4.0", PACKAGE_DIRECTORY),
        )
        with mock.patch(
            "py_frp.update.metadata.distributions",
            return_value=distributions,
        ):
            self.assertEqual(installed_version(), "0.4.0")

    def test_installed_version_uses_highest_valid_metadata_at_loaded_path(self) -> None:
        distributions = (
            _distribution("0.2.0", PACKAGE_DIRECTORY),
            _distribution("not-a-version", PACKAGE_DIRECTORY),
            _distribution("0.4.0", PACKAGE_DIRECTORY),
        )
        with mock.patch(
            "py_frp.update.metadata.distributions",
            return_value=distributions,
        ):
            self.assertEqual(installed_version(), "0.4.0")

    async def test_failed_restart_target_does_not_loop_on_same_version(self) -> None:
        versions = iter(("0.4.0", "0.4.0", "0.5.0"))
        with (
            mock.patch.dict(
                os.environ,
                {RESTART_TARGET_VERSION_ENV: "0.4.0"},
            ),
            self.assertLogs("py_frp.update", level="ERROR") as captured,
        ):
            change = await wait_for_version_change(
                initial_version="0.3.0",
                interval=0.001,
                version_reader=lambda: next(versions),
            )

        self.assertEqual(change, VersionChange(previous="0.3.0", current="0.5.0"))
        self.assertIn("suppressing another restart", captured.output[0])

    async def test_successful_restart_clears_target_guard(self) -> None:
        with mock.patch.dict(
            os.environ,
            {RESTART_TARGET_VERSION_ENV: "0.3.0"},
        ):
            change = await wait_for_version_change(
                initial_version="0.3.0",
                interval=0.001,
                version_reader=lambda: "0.4.0",
            )
            self.assertNotIn(RESTART_TARGET_VERSION_ENV, os.environ)

        self.assertEqual(change, VersionChange(previous="0.3.0", current="0.4.0"))

    async def test_server_closes_immediately_before_restart(self) -> None:
        events: list[str] = []
        preserved_tokens: list[str | None] = []

        class FakeServer:
            config = SimpleNamespace(
                source_flavor="token-pool",
                pool_tokens=("same-random-token",),
            )

            async def serve_forever(self) -> None:
                try:
                    await asyncio.Event().wait()
                finally:
                    events.append("runtime cleaned")

            async def close(self) -> None:
                events.append("server closed")

            async def notify_restarting(self) -> None:
                events.append("clients notified")

            def preserve_tls_for_restart(self) -> None:
                events.append("TLS preserved")

        async def fake_monitor(runtime, **kwargs):
            task = asyncio.create_task(runtime)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return None, VersionChange(previous="0.3.0", current="0.4.0")

        def fake_restart(argv, *, expected_version=None) -> None:
            events.append("restarted")
            events.append(f"target {expected_version}")
            preserved_tokens.append(os.environ.get(cli.POOL_TOKEN_ENV))

        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("py_frp.cli.run_until_version_change", new=fake_monitor),
            mock.patch(
                "py_frp.cli.restart_current_command",
                side_effect=fake_restart,
            ),
        ):
            result = await cli._run_server_until_update(
                FakeServer(),  # type: ignore[arg-type]
                ["server", "--port-pool", "6000"],
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            [
                "runtime cleaned",
                "TLS preserved",
                "clients notified",
                "server closed",
                "restarted",
                "target 0.4.0",
            ],
        )
        self.assertEqual(preserved_tokens, ["same-random-token"])

    def test_posix_restart_executes_current_python_with_preserved_arguments(self) -> None:
        with (
            mock.patch("py_frp.restart.sys.executable", "C:\\Python\\python.exe"),
            mock.patch("py_frp.restart.os.name", "posix"),
            mock.patch("py_frp.restart.os.execv") as execv,
        ):
            restart_current_command(
                ["client", "--server", "example.com:7000", "--force"]
            )

        execv.assert_called_once_with(
            "C:\\Python\\python.exe",
            [
                "C:\\Python\\python.exe",
                "-m",
                "py_frp",
                "client",
                "--server",
                "example.com:7000",
                "--force",
            ],
        )

    def test_windows_restart_runs_replacement_in_foreground_terminal(self) -> None:
        process = mock.Mock()
        process.wait.return_value = 23
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("py_frp.restart.sys.executable", "C:\\Python\\python.exe"),
            mock.patch("py_frp.restart.os.name", "nt"),
            mock.patch("py_frp.restart.subprocess.Popen", return_value=process) as popen,
            mock.patch("py_frp.restart.signal.signal", return_value=object()),
            mock.patch("py_frp.restart.os.execv") as execv,
            self.assertRaises(SystemExit) as raised,
        ):
            restart_current_command(
                ["server", "--port-pool", "6000"],
                expected_version="0.5.3",
            )

        self.assertEqual(raised.exception.code, 23)
        execv.assert_not_called()
        args, kwargs = popen.call_args
        self.assertEqual(
            args[0],
            [
                "C:\\Python\\python.exe",
                "-m",
                "py_frp",
                "server",
                "--port-pool",
                "6000",
            ],
        )
        self.assertEqual(kwargs["cwd"], os.getcwd())
        self.assertEqual(kwargs["env"][RESTART_TARGET_VERSION_ENV], "0.5.3")
        self.assertEqual(kwargs["env"][WINDOWS_RESTART_SUPERVISOR_ENV], "1")

    def test_supervised_windows_child_requests_restart_without_nesting(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {WINDOWS_RESTART_SUPERVISOR_ENV: "1"},
            ),
            mock.patch("py_frp.restart.os.name", "nt"),
            mock.patch("py_frp.restart.subprocess.Popen") as popen,
            self.assertRaises(SystemExit) as raised,
        ):
            restart_current_command(
                ["client", "--server", "example.com:7000"],
                expected_version="0.5.4",
            )

        self.assertEqual(raised.exception.code, WINDOWS_RESTART_EXIT_CODE)
        popen.assert_not_called()

    def test_windows_supervisor_reaps_child_after_ctrl_c(self) -> None:
        process = mock.Mock()
        process.wait.side_effect = (KeyboardInterrupt(), 130)
        with (
            mock.patch("py_frp.restart.os.name", "nt"),
            mock.patch("py_frp.restart.subprocess.Popen", return_value=process),
            mock.patch("py_frp.restart.signal.signal", return_value=object()) as signal,
            self.assertRaises(SystemExit) as raised,
        ):
            restart_current_command(["client", "--server", "example.com:7000"])

        self.assertEqual(raised.exception.code, 130)
        self.assertEqual(process.wait.call_count, 2)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertEqual(signal.call_count, 4)

    def test_windows_supervisor_terminates_child_stuck_after_ctrl_c(self) -> None:
        process = mock.Mock()
        process.wait.side_effect = (
            KeyboardInterrupt(),
            subprocess.TimeoutExpired("py-frp", 5),
            1,
        )
        with (
            mock.patch("py_frp.restart.os.name", "nt"),
            mock.patch("py_frp.restart.subprocess.Popen", return_value=process),
            mock.patch("py_frp.restart.signal.signal", return_value=object()),
            self.assertRaises(SystemExit) as raised,
        ):
            restart_current_command(["server", "--port-pool", "6000"])

        self.assertEqual(raised.exception.code, 130)
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()

    def test_windows_supervisor_rotates_children_without_nesting(self) -> None:
        first = mock.Mock()
        first.wait.return_value = WINDOWS_RESTART_EXIT_CODE
        second = mock.Mock()
        second.wait.return_value = 0
        environments: list[dict[str, str]] = []
        processes = iter((first, second))

        def fake_popen(command, **kwargs):
            environments.append(kwargs["env"].copy())
            return next(processes)

        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("py_frp.restart.os.name", "nt"),
            mock.patch("py_frp.restart.subprocess.Popen", side_effect=fake_popen),
            mock.patch("py_frp.restart.signal.signal", return_value=object()),
            self.assertRaises(SystemExit) as raised,
        ):
            restart_current_command(
                ["server", "--port-pool", "6000"],
                expected_version="0.5.3",
            )

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(len(environments), 2)
        self.assertEqual(environments[0][RESTART_TARGET_VERSION_ENV], "0.5.3")
        self.assertNotIn(RESTART_TARGET_VERSION_ENV, environments[1])
        self.assertTrue(
            all(env[WINDOWS_RESTART_SUPERVISOR_ENV] == "1" for env in environments)
        )


def _distribution(version: str, package_directory: Path) -> SimpleNamespace:
    return SimpleNamespace(
        version=version,
        locate_file=lambda _: package_directory,
    )


if __name__ == "__main__":
    unittest.main()
