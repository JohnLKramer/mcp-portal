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

from starlette.applications import Starlette

from mcp_portal.app import build_app
from mcp_portal.config.loader import ConfigError, load_config


async def _run_uvicorn(asgi_app: Starlette, host: str, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(asgi_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


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


def _configure_logging() -> None:
    """Turn on our own INFO logging without turning on anybody else's.

    `logging.basicConfig(level=INFO)` configures the *root* logger, which also
    switches on httpx's per-request line: one full URL per call, query string
    included, straight to stderr with no redaction boundary. Model-supplied
    argument values end up in those URLs, so the level is set on `mcp_portal`
    alone and third-party loggers keep their own defaults.
    """
    logger = logging.getLogger("mcp_portal")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        logger.addHandler(handler)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_logging()
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

    if loaded.config.server.transport == "stdio":
        from mcp_portal.server.stdio import run_stdio

        async def _serve() -> None:
            try:
                await run_stdio(app, loaded.config.server.name)
            finally:
                await app.aclose()

        asyncio.run(_serve())
        return 0

    from mcp_portal.server.http import build_http_app

    async def _serve_http() -> None:
        try:
            asgi_app = build_http_app(app, loaded.config, loaded.secrets)
            await _run_uvicorn(
                asgi_app, loaded.config.server.http.host, loaded.config.server.http.port
            )
        finally:
            await app.aclose()

    asyncio.run(_serve_http())
    return 0
