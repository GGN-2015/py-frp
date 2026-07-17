"""Small runtime compatibility helpers for supported Python 3 releases."""

import asyncio
import functools
import ssl


def create_task(awaitable):
    """Schedule an awaitable on Python versions before ``asyncio.create_task``."""

    return asyncio.ensure_future(awaitable)


def get_running_loop():
    """Return the active event loop on Python 3.6 and newer."""

    getter = getattr(asyncio, "get_running_loop", None)
    return getter() if getter is not None else asyncio.get_event_loop()


async def to_thread(function, *args, **kwargs):
    """Run a blocking callable in the default executor."""

    loop = get_running_loop()
    call = functools.partial(function, *args, **kwargs)
    return await loop.run_in_executor(None, call)


def run(awaitable):
    """Run one top-level coroutine with ``asyncio.run``-equivalent cleanup."""

    native_run = getattr(asyncio, "run", None)
    if native_run is not None:
        return native_run(awaitable)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(awaitable)
    finally:
        pending = list(asyncio.Task.all_tasks(loop=loop))
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        shutdown_asyncgens = getattr(loop, "shutdown_asyncgens", None)
        if shutdown_asyncgens is not None:
            loop.run_until_complete(shutdown_asyncgens())
        asyncio.set_event_loop(None)
        loop.close()


def require_tls12(context):
    """Configure TLS 1.2 as the minimum across old and new ``ssl`` APIs."""

    tls_version = getattr(ssl, "TLSVersion", None)
    if tls_version is not None and hasattr(context, "minimum_version"):
        context.minimum_version = tls_version.TLSv1_2
        return

    for option_name in ("OP_NO_SSLv2", "OP_NO_SSLv3", "OP_NO_TLSv1", "OP_NO_TLSv1_1"):
        context.options |= getattr(ssl, option_name, 0)
