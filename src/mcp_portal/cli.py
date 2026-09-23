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

    configure_parser = sub.add_parser(
        "configure",
        help="Interactively introspect an upstream and write config + policy files.",
    )
    configure_parser.add_argument("--config", required=True, type=Path)
    configure_parser.add_argument("--policy", type=Path, default=None)

    return parser


def _default_policy_path(config_path: Path, loaded_policy_file: str | None) -> Path:
    """Where to write the policy file when `--policy` wasn't given.

    If the config already references a policy file, keep writing to that same
    file so reconcile doesn't orphan it. Otherwise default to `policy.<ext>`
    next to the config, matching the config's own suffix (`.json` configs get
    a `.json` policy; everything else gets `.yaml`).
    """
    if loaded_policy_file is not None:
        candidate = Path(loaded_policy_file)
        return candidate if candidate.is_absolute() else config_path.parent / candidate
    suffix = ".json" if config_path.suffix == ".json" else ".yaml"
    return config_path.parent / f"policy{suffix}"


def _run_configure(config_path: Path, policy_path: Path | None) -> int:
    from ruamel.yaml import YAML

    from mcp_portal.config.loader import ConfigError, load_config
    from mcp_portal.config.policy import PolicyConfig
    from mcp_portal.configure.interaction import StdinPrompter
    from mcp_portal.configure.reconcile import decisions_from_config, run_reconcile
    from mcp_portal.configure.survey import run_survey
    from mcp_portal.configure.write import render_config, render_policy, write_with_confirmation
    from mcp_portal.introspect import introspect_upstreams

    try:
        loaded = load_config(config_path)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if policy_path is None:
        existing_policy_file = (
            loaded.config.policy.file if loaded.config.policy is not None else None
        )
        policy_path = _default_policy_path(config_path.resolve(), existing_policy_file)

    prompter = StdinPrompter()

    try:
        operations, base_urls = introspect_upstreams(loaded.config, loaded.base_dir)

        if loaded.config.operations:
            previous = decisions_from_config(loaded.config, loaded.policy)
            result, report = run_reconcile(operations, previous, prompter)
            if report.removed:
                print(f"removed upstream operation(s), dropped from output: {list(report.removed)}")
            if report.new:
                print(f"new operation(s) surveyed: {list(report.new)}")
            if report.changed:
                print(f"changed operation(s) re-confirmed: {list(report.changed)}")
        else:
            result = run_survey(operations, prompter)

        # Start from a full dump of the loaded config rather than rebuilding it
        # from the decisions: everything `configure` doesn't know how to touch
        # (outbound credentials, timeouts, selection, naming, classification,
        # inbound auth, HTTP bind settings) has to survive the write untouched.
        operations_rendered = render_config(
            result.decisions,
            server_name=loaded.config.server.name,
            upstream_base_urls=base_urls,
            mode="configured",
        )["operations"]

        rendered_config = loaded.config.model_dump(mode="json", by_alias=True, exclude_none=True)
        # The survey's exposure decisions are only authoritative under
        # 'configured'; every introspect-* mode re-discovers and re-merges,
        # which would serve a declined operation anyway.
        rendered_config["mode"] = "configured"
        rendered_config["operations"] = operations_rendered
        rendered_config["policy"] = {"file": str(policy_path)}
        for key, url in base_urls.items():
            if key in rendered_config["upstreams"] and url:
                # Re-dumped from the model rather than assigned into the existing
                # dict so a base_url that was absent before lands in declared
                # field order, keeping a later no-op run a genuine no-op.
                rendered_config["upstreams"][key] = (
                    loaded.config.upstreams[key]
                    .model_copy(update={"base_url": url})
                    .model_dump(mode="json", by_alias=True, exclude_none=True)
                )

        rendered_policy = render_policy(result.decisions, existing_policy=loaded.policy)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if not write_with_confirmation(policy_path, rendered_policy, prompter):
        print("policy not written.")
        return 0
    if not write_with_confirmation(config_path, rendered_config, prompter):
        print("config not written.")
        return 0

    # PolicyConfig is validated on next real load by config.loader; validate
    # eagerly here too so a malformed render is caught before the operator
    # walks away thinking `configure` succeeded.
    PolicyConfig.model_validate(YAML(typ="safe").load(policy_path.read_text()))
    return 0


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

    if args.command == "configure":
        return _run_configure(args.config, args.policy)

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
