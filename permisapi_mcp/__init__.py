"""permisapi-mcp : serveur MCP pour PermisAPI.

Permet a Claude Desktop, Cursor, Windsurf, ou tout client MCP-compatible
de consulter les permis de construire France en langage naturel.

Usage :
    pip install permisapi-mcp

    # Configure Claude Desktop avec :
    # {
    #   "mcpServers": {
    #     "permisapi": {
    #       "command": "permisapi-mcp",
    #       "env": {"PERMISAPI_KEY": "pk_live_..."}
    #     }
    #   }
    # }

Voir docs/MCP_SETUP.md pour le guide complet.
"""

__version__ = "0.2.1"
