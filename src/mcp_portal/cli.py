"""Command-line entrypoint.

There is deliberately no default --mode: mode is required in config and cannot be
reached by omission.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from mcp_portal.app import build_app
from mcp_portal.config.loader import ConfigError, load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-portal")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("serve", "Serve the configured tools over stdio."),
        ("validate", "Load and validate the config, then report the tool surface."),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", required=True, type=Path)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    args = _parser().parse_args(argv)

    try:
        loaded = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        app = build_app(loaded)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "validate":
        for tool in app.invoker.tools():
            print(f"{tool.name}\t{tool.title}")
        asyncio.run(app.aclose())
        return 0

    from mcp_portal.server.stdio import run_stdio

    async def _serve() -> None:
        try:
            await run_stdio(app, loaded.config.server.name)
        finally:
            await app.aclose()

    asyncio.run(_serve())
    return 0
