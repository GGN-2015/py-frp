from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from importlib import metadata
from typing import TypeVar

from packaging.version import InvalidVersion, Version

from . import __version__


LOGGER = logging.getLogger(__name__)
DISTRIBUTION_NAME = "py-simple-nat-tunnel"
UPDATE_CHECK_INTERVAL = 5.0

T = TypeVar("T")


@dataclass(frozen=True)
class VersionChange:
    previous: str
    current: str


def installed_version() -> str | None:
    try:
        candidates = {
            distribution.version
            for distribution in metadata.distributions(name=DISTRIBUTION_NAME)
        }
    except OSError:
        return None
    parsed: list[tuple[Version, str]] = []
    for candidate in candidates:
        try:
            parsed.append((Version(candidate), candidate))
        except InvalidVersion:
            LOGGER.warning("ignored invalid installed package version %r", candidate)
    if not parsed:
        return None
    return max(parsed)[1]


async def wait_for_version_change(
    *,
    initial_version: str | None = None,
    interval: float = UPDATE_CHECK_INTERVAL,
    version_reader: Callable[[], str | None] = installed_version,
) -> VersionChange:
    if interval <= 0:
        raise ValueError("update check interval must be greater than zero")
    baseline = initial_version or __version__
    while True:
        await asyncio.sleep(interval)
        current = version_reader()
        if current is None:
            LOGGER.debug("installed package version is temporarily unavailable")
            continue
        if current != baseline:
            return VersionChange(previous=baseline, current=current)


async def run_until_version_change(
    runtime: Awaitable[T],
    *,
    initial_version: str | None = None,
    interval: float = UPDATE_CHECK_INTERVAL,
    version_reader: Callable[[], str | None] = installed_version,
    restart_ready: Callable[[], Awaitable[None]] | None = None,
) -> tuple[T | None, VersionChange | None]:
    runtime_task = asyncio.ensure_future(runtime)
    update_task = asyncio.create_task(
        _wait_until_restart_ready(
            initial_version=initial_version,
            interval=interval,
            version_reader=version_reader,
            restart_ready=restart_ready,
        )
    )
    try:
        done, _ = await asyncio.wait(
            {runtime_task, update_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if update_task in done:
            change = update_task.result()
            LOGGER.info(
                "installed package changed from %s to %s; restarting",
                change.previous,
                change.current,
            )
            runtime_task.cancel()
            await asyncio.gather(runtime_task, return_exceptions=True)
            return None, change
        return runtime_task.result(), None
    finally:
        if not update_task.done():
            update_task.cancel()
            await asyncio.gather(update_task, return_exceptions=True)
        if not runtime_task.done():
            runtime_task.cancel()
            await asyncio.gather(runtime_task, return_exceptions=True)


async def _wait_until_restart_ready(
    *,
    initial_version: str | None,
    interval: float,
    version_reader: Callable[[], str | None],
    restart_ready: Callable[[], Awaitable[None]] | None,
) -> VersionChange:
    change = await wait_for_version_change(
        initial_version=initial_version,
        interval=interval,
        version_reader=version_reader,
    )
    if restart_ready is not None:
        await restart_ready()
    return change


def restart_current_command(argv: Sequence[str]) -> None:
    if not sys.executable:
        raise RuntimeError("cannot restart because the Python executable is unknown")
    command = [sys.executable, "-m", "py_frp", *argv]
    LOGGER.info("restarting command in current terminal: %s", command)
    os.execv(sys.executable, command)
