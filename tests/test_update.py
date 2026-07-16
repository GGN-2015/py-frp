from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from py_frp import cli
from py_frp.update import (
    VersionChange,
    installed_version,
    restart_current_command,
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

    def test_installed_version_uses_highest_valid_duplicate_metadata(self) -> None:
        distributions = (
            SimpleNamespace(version="0.2.0"),
            SimpleNamespace(version="not-a-version"),
            SimpleNamespace(version="0.4.0"),
        )
        with mock.patch(
            "py_frp.update.metadata.distributions",
            return_value=distributions,
        ):
            self.assertEqual(installed_version(), "0.4.0")

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

        def fake_restart(argv) -> None:
            events.append("restarted")
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
            ],
        )
        self.assertEqual(preserved_tokens, ["same-random-token"])

    def test_posix_restart_executes_current_python_with_preserved_arguments(self) -> None:
        with (
            mock.patch("py_frp.update.sys.executable", "C:\\Python\\python.exe"),
            mock.patch("py_frp.update.os.name", "posix"),
            mock.patch("py_frp.update.os.execv") as execv,
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
        completed = SimpleNamespace(returncode=23)
        with (
            mock.patch("py_frp.update.sys.executable", "C:\\Python\\python.exe"),
            mock.patch("py_frp.update.os.name", "nt"),
            mock.patch("py_frp.update.subprocess.run", return_value=completed) as run,
            mock.patch("py_frp.update.os.execv") as execv,
            self.assertRaises(SystemExit) as raised,
        ):
            restart_current_command(["server", "--port-pool", "6000"])

        self.assertEqual(raised.exception.code, 23)
        execv.assert_not_called()
        args, kwargs = run.call_args
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
        self.assertEqual(kwargs["env"], os.environ.copy())
        self.assertFalse(kwargs["check"])


if __name__ == "__main__":
    unittest.main()
