from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any


LOGGER = logging.getLogger(__name__)
MAX_MESSAGE_BYTES = 1024 * 1024
BUFFER_SIZE = 64 * 1024


class ProtocolError(RuntimeError):
    """Raised when a peer sends an invalid protocol message."""


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    line = await reader.readline()
    if not line:
        return None
    if len(line) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message exceeds maximum size")
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("message is not valid JSON") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    return message


async def write_message(
    writer: asyncio.StreamWriter,
    message: Mapping[str, Any],
) -> None:
    payload = json.dumps(
        dict(message),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message exceeds maximum size")
    writer.write(payload + b"\n")
    await writer.drain()


async def close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None or writer.is_closing():
        return
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass


async def pipe_streams(
    left_reader: asyncio.StreamReader,
    left_writer: asyncio.StreamWriter,
    right_reader: asyncio.StreamReader,
    right_writer: asyncio.StreamWriter,
) -> None:
    left_to_right = asyncio.create_task(_copy_stream(left_reader, right_writer))
    right_to_left = asyncio.create_task(_copy_stream(right_reader, left_writer))
    pending = {left_to_right, right_to_left}
    cancel_remaining = False

    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    cancel_remaining = True
                    raise
                except (ConnectionError, OSError) as exc:
                    LOGGER.debug("stream relay disconnected: %r", exc)
                    cancel_remaining = True
                except Exception as exc:
                    LOGGER.debug("stream relay ended with %r", exc)
                    cancel_remaining = True
            if cancel_remaining:
                break
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    await asyncio.gather(
        close_writer(left_writer),
        close_writer(right_writer),
        return_exceptions=True,
    )


async def _copy_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            chunk = await reader.read(BUFFER_SIZE)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionError, OSError):
        raise
    finally:
        try:
            writer.write_eof()
        except (AttributeError, ConnectionError, OSError, RuntimeError):
            pass
