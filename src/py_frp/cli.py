from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from . import __version__
from .client import Client
from .config import (
    ConfigError,
    describe_client_config,
    describe_server_config,
    load_client_config,
    load_server_config,
)
from .elevate import ElevationError, is_admin, relaunch_once, should_elevate_server
from .server import Server


LOGGER = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(effective_argv)
    _configure_logging(args.log_level)

    try:
        if args.command == "server":
            return _run_server_command(args, effective_argv)
        if args.command == "client":
            return _run_client_command(args)
    except KeyboardInterrupt:
        return 130
    except ConfigError as exc:
        LOGGER.error("configuration error: %s", exc)
        return 2
    except ElevationError as exc:
        LOGGER.error("elevation failed: %s", exc)
        return 1
    return 2


def server_main(argv: Sequence[str] | None = None) -> int:
    return main(["server", *(sys.argv[1:] if argv is None else argv)])


def client_main(argv: Sequence[str] | None = None) -> int:
    return main(["client", *(sys.argv[1:] if argv is None else argv)])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="py-frp",
        description="A small Python TCP reverse tunnel.",
    )
    parser.add_argument("--version", action="version", version=f"py-frp {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server = subparsers.add_parser("server", aliases=["frps"], help="run the public server")
    _add_runtime_options(server)
    server.add_argument("-c", "--config", required=True, help="server config path")
    server.add_argument(
        "--elevate",
        action="store_true",
        help="relaunch as administrator/root once before binding ports",
    )
    server.add_argument(
        "--auto-elevate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="auto elevate once when configured listen ports are privileged",
    )
    server.add_argument("--elevation-attempted", action="store_true", help=argparse.SUPPRESS)

    client = subparsers.add_parser("client", aliases=["frpc"], help="run the private client")
    _add_runtime_options(client)
    client.add_argument("-c", "--config", required=True, help="client config path")

    return parser


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="logging level",
    )


def _run_server_command(args: argparse.Namespace, effective_argv: Sequence[str]) -> int:
    config = load_server_config(args.config)
    LOGGER.info("loaded server config: %s", describe_server_config(config))
    needs_elevation = args.elevate or (args.auto_elevate and should_elevate_server(config))
    if needs_elevation and not args.elevation_attempted and not is_admin():
        LOGGER.info("relaunching with administrator/root privileges")
        return relaunch_once(effective_argv)
    return asyncio.run(Server(config).serve_forever())


def _run_client_command(args: argparse.Namespace) -> int:
    config = load_client_config(args.config)
    LOGGER.info("loaded client config: %s", describe_client_config(config))
    return asyncio.run(Client(config).run())


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
