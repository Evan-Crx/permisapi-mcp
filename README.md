# permisapi-mcp

**MCP** (Model Context Protocol) server for [PermisAPI](https://permisapi.fr).

Lets **Claude Desktop**, **Cursor**, **Windsurf**, or any MCP-compatible
client query the **311 000+ French building permits** (Sitadel open data,
Etalab license) in natural language.

## Installation

```bash
pip install permisapi-mcp
```

You need a free PermisAPI API key : https://permisapi.fr/#pricing

## Claude Desktop config

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows) :

```json
{
  "mcpServers": {
    "permisapi": {
      "command": "permisapi-mcp",
      "env": {
        "PERMISAPI_KEY": "pk_live_YOUR_KEY"
      }
    }
  }
}
```

Restart Claude Desktop. Then ask in natural language :

> "List the residential permits filed in Bordeaux this month with an
> MDB score above 70"
>
> "Find me real estate dealer opportunities around rue de Passy in Paris"
>
> "What is the PLU zoning of permit PC07404021K1 ?"

## Cursor / Windsurf / other MCP clients

Full guide : https://permisapi.fr/mcp

## 7 tools available

| Tool | Endpoint | Plan |
|---|---|:---:|
| `search_permits` | GET /v1/permits | Free |
| `get_permit_details` | GET /v1/permits/{num_pa} | Free |
| `get_permit_full_view` | GET /v1/permits/{num_pa}/360 | Free (detail only) / Pro (6-in-1) |
| `find_dvf_neighbors` | GET /v1/permits/{num_pa}/dvf | Pro |
| `get_mdb_score` | GET /v1/permits/{num_pa}/score | Pro |
| `get_plu_zoning` | GET /v1/permits/{num_pa}/plu | Pro |
| `get_risks` | GET /v1/permits/{num_pa}/risks | Pro |

The composite `get_permit_full_view` (Vue 360) returns detail + sirene +
dvf + score + plu + risks in one tool call. Quota cost is honest : 1
unit on Free / Explorer (detail only), 6 units on Pro+ (1 per
sub-feature, identical to 6 separate calls).

## Security

- Your API key stays **local** (env var), never transmitted to the LLM
- LLM sees only the tool arguments, not the key
- Strict input validation (regex on `num_pa`, Pydantic ranges)
- All tools are **read-only** (GET only). No mutations, no state changes.

## Pricing

- Free 500 req/month, no card
- Explorer 49 EUR/month
- Pro 199 EUR/month (unlocks the 4 enrichment tools)
- Business 499 EUR/month
- Enterprise 1999+ EUR/month

The MCP server uses the same quota as the underlying REST API, no
separate counter.

## License

MIT.

## Support

evan@permisapi.fr (24-72h reply).
