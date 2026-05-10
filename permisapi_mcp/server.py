"""Serveur MCP stdio pour PermisAPI.

Implemente le Model Context Protocol (Anthropic, novembre 2024) en
mode stdio pour Claude Desktop / Cursor / Windsurf / autres clients
MCP-compatibles.

Lance via la commande `permisapi-mcp` (entry point pyproject.toml).
La cle PermisAPI est lue depuis l'env PERMISAPI_KEY au demarrage.

Architecture :
  - Imports `mcp` lib (Anthropic) au runtime, gracieux si absent
  - Tools list construit a partir de tools.TOOL_SCHEMAS
  - Dispatch via tools.call_tool() (logique testable separement)
"""
from __future__ import annotations

import asyncio
import logging
import sys

logger = logging.getLogger("permisapi-mcp")
logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)-5s [permisapi-mcp] %(message)s",
    stream=sys.stderr,  # IMPORTANT : MCP stdio utilise stdout pour le protocole
)


def main() -> None:
    """Entry point synchrone (appele par `permisapi-mcp` script)."""
    try:
        asyncio.run(_run_server())
    except KeyboardInterrupt:
        logger.info("Arret demande, bye.")
    except Exception:  # noqa: BLE001
        logger.exception("Erreur fatale")
        sys.exit(1)


async def _run_server() -> None:
    """Cree le serveur MCP et lance le transport stdio."""
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import TextContent, Tool
    except ImportError as exc:
        logger.error(
            "Le package `mcp` n'est pas installe. Lance "
            "`pip install permisapi-mcp` pour tout installer correctement. "
            "Erreur : %s",
            exc,
        )
        sys.exit(2)

    from permisapi_mcp.tools import TOOL_SCHEMAS, call_tool

    app = Server("permisapi")

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in TOOL_SCHEMAS
        ]

    @app.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None) -> list[TextContent]:
        result_text = await call_tool(name, arguments or {})
        return [TextContent(type="text", text=result_text)]

    logger.info("permisapi-mcp pret. Connecte via stdio.")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    main()
