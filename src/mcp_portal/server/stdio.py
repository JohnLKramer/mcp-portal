"""Serve the tool set over stdio.

The lowlevel Server takes on_list_tools / on_call_tool callbacks rather than
decorators, which is what lets tools come from config at runtime.
"""

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from mcp_portal.app import App


def build_server(app: App, server_name: str) -> Server[None]:
    """Wire the app's invoker into a lowlevel Server.

    Separate from run_stdio so the callbacks a client actually reaches can be
    driven over any stream pair, stdio or in-memory, without a subprocess.
    """

    async def on_list_tools(
        context: object, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=app.invoker.tools())

    async def on_call_tool(
        context: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await app.invoker.call(params.name, params.arguments)

    return Server(
        name=server_name,
        version="0.1.0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def run_stdio(app: App, server_name: str) -> None:
    server = build_server(app, server_name)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
