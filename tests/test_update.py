from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from py_frp import cli
from py_frp.restart import RESTART_TARGET_VERSION_ENV
from py_frp.supervisor_ipc import SupervisorChannel
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

    async def test_supervised_child_waits_for_parent_restart_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            channel = SupervisorChannel(Path(directory))
            environment = channel.prepare_child_environment(os.environ, "generation")
            with mock.patch.dict(os.environ, environment, clear=True):
                monitored = asyncio.create_task(
                    wait_for_version_change(
                        initial_version="0.3.0",
                        interval=0.001,
                        version_reader=lambda: "9.9.9",
                    )
                )
                await asyncio.sleep(0.01)
                self.assertFalse(monitored.done())

                channel.request_restart("generation", "0.4.0")
                change = await asyncio.wait_for(monitored, timeout=1)

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
        with mock.patch("py_frp.update.metadata.distributions", return_value=()):
            self.assertIsNone(installed_version())

    def test_installed_version_ignores_shadowed_package_metadata(self) -> None:
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

    async def test_unsupervised_failed_target_does_not_loop(self) -> None:
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

    async def test_server_cleanup_precedes_supervisor_restart_request(self) -> None:
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

        def fake_restart(target: str) -> None:
            events.append(f"restart target {target}")
            preserved_tokens.append(os.environ.get(cli.POOL_TOKEN_ENV))

        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("py_frp.cli.run_until_version_change", new=fake_monitor),
            mock.patch("py_frp.cli.request_restart", side_effect=fake_restart),
        ):
            result = await cli._run_server_until_update(
                FakeServer(),  # type: ignore[arg-type]
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            [
                "runtime cleaned",
                "TLS preserved",
                "clients notified",
                "server closed",
                "restart target 0.4.0",
            ],
        )
        self.assertEqual(preserved_tokens, ["same-random-token"])


def _distribution(version: str, package_directory: Path) -> SimpleNamespace:
    return SimpleNamespace(
        version=version,
        locate_file=lambda _: package_directory,
    )


if __name__ == "__main__":
    unittest.main()
