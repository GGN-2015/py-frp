from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import Mapping
from typing import Any


LOGGER = logging.getLogger(__name__)
MAX_MESSAGE_BYTES = 1024 * 1024
BUFFER_SIZE = 64 * 1024
TUNNEL_DATA_FRAME = b"D"
TUNNEL_EOF_FRAME = b"E"


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


async def pipe_tunnel_streams(
    plain_reader: asyncio.StreamReader,
    plain_writer: asyncio.StreamWriter,
    tunnel_reader: asyncio.StreamReader,
    tunnel_writer: asyncio.StreamWriter,
) -> None:
    to_tunnel = asyncio.create_task(_copy_to_tunnel(plain_reader, tunnel_writer))
    from_tunnel = asyncio.create_task(_copy_from_tunnel(tunnel_reader, plain_writer))
    pending = {to_tunnel, from_tunnel}
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
                except (ConnectionError, OSError, ProtocolError) as exc:
                    LOGGER.debug("encrypted tunnel relay disconnected: %r", exc)
                    cancel_remaining = True
                except Exception as exc:
                    LOGGER.debug("encrypted tunnel relay ended with %r", exc)
                    cancel_remaining = True
            if cancel_remaining:
                break
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    await asyncio.gather(
        close_writer(plain_writer),
        close_writer(tunnel_writer),
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


async def _copy_to_tunnel(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    while True:
        chunk = await reader.read(BUFFER_SIZE)
        if not chunk:
            writer.write(TUNNEL_EOF_FRAME + struct.pack("!I", 0))
            await writer.drain()
            return
        writer.write(TUNNEL_DATA_FRAME + struct.pack("!I", len(chunk)) + chunk)
        await writer.drain()


async def _copy_from_tunnel(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    while True:
        try:
            header = await reader.readexactly(5)
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError("encrypted tunnel closed before an EOF frame") from exc
        frame_type = header[:1]
        length = struct.unpack("!I", header[1:])[0]
        if frame_type == TUNNEL_EOF_FRAME:
            if length != 0:
                raise ProtocolError("invalid encrypted tunnel EOF frame")
            try:
                writer.write_eof()
            except (AttributeError, ConnectionError, OSError, RuntimeError):
                writer.close()
            return
        if frame_type != TUNNEL_DATA_FRAME:
            raise ProtocolError("unknown encrypted tunnel frame type")
        if length > BUFFER_SIZE:
            raise ProtocolError("encrypted tunnel frame exceeds maximum size")
        try:
            payload = await reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError("encrypted tunnel data frame was truncated") from exc
        writer.write(payload)
        await writer.drain()
