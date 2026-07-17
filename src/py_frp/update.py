import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Set, Tuple, TypeVar

try:
    from importlib import metadata  # novermin: guarded by the backport below
except ImportError:  # pragma: no cover - Python 3.6 and 3.7
    import importlib_metadata as metadata

from packaging.version import InvalidVersion, Version

from . import __version__
from .compat import create_task
from .restart import RESTART_TARGET_VERSION_ENV
from .supervisor_ipc import is_supervised_child, supervisor_restart_target


LOGGER = logging.getLogger(__name__)
DISTRIBUTION_NAME = "py-simple-nat-tunnel"
UPDATE_CHECK_INTERVAL = 5.0
PACKAGE_DIRECTORY = Path(__file__).resolve().parent

T = TypeVar("T")


@dataclass(frozen=True)
class VersionChange:
    previous: str
    current: str


def installed_version() -> Optional[str]:
    try:
        distributions = metadata.distributions(name=DISTRIBUTION_NAME)
    except OSError:
        return None
    candidates: Set[str] = set()
    for distribution in distributions:
        try:
            candidate_directory = Path(distribution.locate_file("py_frp")).resolve()
        except (AttributeError, OSError, RuntimeError, TypeError):
            continue
        if os.path.normcase(str(candidate_directory)) != os.path.normcase(
            str(PACKAGE_DIRECTORY)
        ):
            continue
        candidates.add(distribution.version)
    parsed: List[Tuple[Version, str]] = []
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
    initial_version: Optional[str] = None,
    interval: float = UPDATE_CHECK_INTERVAL,
    version_reader: Callable[[], Optional[str]] = installed_version,
) -> VersionChange:
    if interval <= 0:
        raise ValueError("update check interval must be greater than zero")
    baseline = initial_version or __version__
    supervised = is_supervised_child()
    suppressed_target = None if supervised else _failed_restart_target(baseline)
    while True:
        await asyncio.sleep(interval)
        current = supervisor_restart_target() if supervised else version_reader()
        if current is None:
            LOGGER.debug("no package restart target is currently available")
            continue
        if current != baseline:
            if current == suppressed_target:
                continue
            return VersionChange(previous=baseline, current=current)


async def run_until_version_change(
    runtime: Awaitable[T],
    *,
    initial_version: Optional[str] = None,
    interval: float = UPDATE_CHECK_INTERVAL,
    version_reader: Callable[[], Optional[str]] = installed_version,
    restart_ready: Optional[Callable[[], Awaitable[None]]] = None,
) -> Tuple[Optional[T], Optional[VersionChange]]:
    runtime_task = asyncio.ensure_future(runtime)
    update_task = create_task(
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
                "package restart requested from %s to %s",
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
    initial_version: Optional[str],
    interval: float,
    version_reader: Callable[[], Optional[str]],
    restart_ready: Optional[Callable[[], Awaitable[None]]],
) -> VersionChange:
    change = await wait_for_version_change(
        initial_version=initial_version,
        interval=interval,
        version_reader=version_reader,
    )
    if restart_ready is not None:
        await restart_ready()
    return change


def _failed_restart_target(loaded_version: str) -> Optional[str]:
    target = os.environ.get(RESTART_TARGET_VERSION_ENV)
    if not target:
        return None
    if target == loaded_version:
        os.environ.pop(RESTART_TARGET_VERSION_ENV, None)
        return None
    LOGGER.error(
        "automatic restart expected package version %s but loaded %s; "
        "suppressing another restart toward %s until the installed version changes",
        target,
        loaded_version,
        target,
    )
    return target
