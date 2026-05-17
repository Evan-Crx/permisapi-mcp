# permisapi-mcp

Serveur **MCP** (Model Context Protocol, Anthropic) pour [PermisAPI](https://permisapi.fr).

Permet à **Claude Desktop**, **Claude.ai web**, **ChatGPT custom GPT**, **Cursor**,
**Windsurf** ou tout client MCP-compatible de consulter **1,2 M+ permis de
construire de France** (Sitadel 2014-2026, résidentiel + non-résidentiel, depuis
2014) en langage naturel.

16 outils disponibles : recherche par adresse, **score d'opportunité Marchand
de Biens v0.3 et son explication transparente** (les 11 signaux pondérés
détaillés en français avec interprétation contextualisée), prix au m² des
ventes voisines sur 12 ans, zonage urbanisme PLU, risques (inondation,
sismique, ICPE), parcelle cadastre DGFiP (par identifiant Etalab ou par
géométrie), **bâtiments existants** (terrain nu vs déjà bâti), **parcelles
voisines d'un permis** (pattern d'activité local marchand de biens),
**recherche par polygone GeoJSON custom** (ZAC, périmètre opération),
statistiques densité commune et enrichissement de liste client.

## Deux modes au choix

### Mode 1 : SSE hosted (recommandé, zéro installation)

Connecte directement Claude.ai web ou ChatGPT à `https://mcp.permisapi.fr/mcp`
avec ta clé PermisAPI en Bearer token. Pas de Python à installer, pas de
config locale, ça marche depuis n'importe quel browser.

**Claude.ai web** (Settings > Integrations > Add MCP server) :

```
URL    : https://mcp.permisapi.fr/mcp
Auth   : Bearer
Token  : pk_live_VOTRE_CLE
```

**Cursor / Windsurf** (`~/.cursor/mcp.json`) :

```json
{
  "mcpServers": {
    "permisapi-hosted": {
      "url": "https://mcp.permisapi.fr/mcp",
      "headers": { "Authorization": "Bearer pk_live_VOTRE_CLE" }
    }
  }
}
```

Mode supporté : Streamable HTTP (spec actuelle MCP) sur `/mcp` ET SSE legacy sur
`/sse` + `/messages/` (backward compat). Aucune donnée n'est stockée côté
serveur MCP, c'est un proxy authentifié vers `api.permisapi.fr`.

### Mode 2 : stdio local (Claude Desktop classique)

Pour Claude Desktop ou si tu préfères tout en local, install Python et le
package `permisapi-mcp` :

## Pré-requis

- **Python 3.10 ou plus récent** (requis par le MCP SDK Anthropic, non négociable)
- Une clé PermisAPI : [https://permisapi.fr/#pricing](https://permisapi.fr/#pricing) (gratuite pour commencer)

## Installation

Vérifier d'abord la version Python :

```bash
python --version       # macOS / Linux / Windows
```

Si `>= 3.10` :

```bash
pip install permisapi-mcp
```

Si `< 3.10`, voir la section [Troubleshooting](#troubleshooting) plus bas
(workaround `uvx` en 1 commande, pas besoin d'upgrade système).

## Configuration Claude Desktop

Éditez `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
ou `%APPDATA%\Claude\claude_desktop_config.json` (Windows) :

```json
{
  "mcpServers": {
    "permisapi": {
      "command": "permisapi-mcp",
      "env": {
        "PERMISAPI_KEY": "pk_live_VOTRE_CLE"
      }
    }
  }
}
```

Redémarrez Claude Desktop. Vous pouvez maintenant demander :

> *« Liste les permis de logement déposés à Paris ce mois avec un score MDB > 70 »*
>
> *« Trouve-moi des opportunités MDB autour de la rue de Passy à Paris »*
>
> *« Quel est le zonage PLU du permis 0930662500027 ? »*

**Note sur le périmètre géographique** :
- **Free** : 1 département au choix (Paris par défaut). Modifiable via le dashboard.
- **Explorer** : 5 départements au choix (Paris/Lyon/Marseille/Bordeaux/Toulouse par défaut).
- **Pro / Business / Enterprise** : France entière, aucune restriction géographique.

Les exemples ci-dessus ciblent Paris (75) pour qu'ils fonctionnent immédiatement sur tous les plans. Si vous êtes sur Explorer avec ses départements par défaut, vous pouvez aussi demander "à Lyon", "à Bordeaux", etc.

## Configuration Cursor / Windsurf / autres clients

Voir le guide complet : [https://permisapi.fr/mcp](https://permisapi.fr/mcp)

## Tools disponibles (16)

| Tool | Endpoint | Plan |
|---|---|:---:|
| `search_permits` | GET /v1/permits (13 filtres) | Free |
| `get_permit_details` | GET /v1/permits/{num_pa} | Free |
| `fuzzy_search_addresses` | GET /v1/search?q=text (pg_trgm fuzzy) | Free |
| `find_dvf_neighbors` | GET /v1/permits/{num_pa}/dvf (12 ans : Cerema DVF+ 2014-2020 fusionné Geo-DVF 2021-2025) | Pro |
| `get_mdb_score` | GET /v1/permits/{num_pa}/score (Score MDB v0.3, 11 signaux) | Pro |
| **`get_score_explanation`** | **GET /v1/permits/{num_pa}/score/explain** (11 signaux décryptés + interprétation FR contextualisée + top drivers/drags + inputs concrets, USP transparence) | Pro |
| `get_plu_zoning` | GET /v1/permits/{num_pa}/plu | Pro |
| `get_risks` | GET /v1/permits/{num_pa}/risks (Géorisques BRGM) | Pro |
| `get_parcelle_geometry` | GET /v1/permits/{num_pa}/parcelle (cadastre DGFiP) | Pro |
| `get_existing_buildings` | GET /v1/permits/{num_pa}/batiments-existants (terrain nu vs bâti, use case MDB) | Pro |
| `get_parcelle_by_id` | GET /v1/parcelles/{id_parcelle} (lookup direct cadastre 14 chars Etalab) | Pro |
| `get_neighbor_parcels` | GET /v1/permits/{num_pa}/parcelles-voisines (rayon 10-2000 m, killer feature MDB) | Pro |
| `search_permits_in_polygon` | POST /v1/permits/inside-polygon (polygon GeoJSON custom ZAC) | Business |
| `get_commune_density_stats` | GET /v1/stats/commune/{code}/density (BI agrégé parcelles + bâtiments + permits) | Business |
| `get_permit_full_view` | GET /v1/permits/{num_pa}/360 (composite 6-en-1) | Pro |
| `bulk_enrich_list` | POST /v1/permits/bulk-enrich (croise liste client jusqu'à 1 000 lignes) | Business |

## Sécurité

- La clé API reste **côté user** (env var locale, jamais transmise au LLM)
- Le LLM voit uniquement les arguments des tools (pas la clé)
- Validation stricte des inputs (regex sur `num_pa`, ranges Pydantic)
- 14 outils en consultation pure (GET) + 2 outils POST (`bulk_enrich_list`
  qui croise une liste client, et `search_permits_in_polygon` qui prend un
  polygon GeoJSON custom). Tous en lecture seule côté PermisAPI : aucune
  donnée client n'est stockée, on renvoie juste les permits qui matchent.

## Troubleshooting

### `pip install permisapi-mcp` dit "package introuvable" ou "no matching distribution"

Cause la plus fréquente : votre Python est plus ancien que 3.10. Le MCP SDK
Anthropic requiert Python 3.10 minimum, on ne peut pas descendre cette borne.

Vérifiez :

```bash
python --version       # ou python3 --version
```

Si `< 3.10`, deux solutions au choix.

**Solution A (recommandée, 30 secondes) : `uvx` avec pin Python**

`uvx` installe et lance le serveur dans un Python isolé pinné à la version
voulue, sans toucher à votre installation système.

```bash
# 1. Installer uv (une seule fois)
curl -LsSf https://astral.sh/uv/install.sh | sh         # macOS / Linux
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows PowerShell

# 2. Lancer le serveur
uvx --python 3.11 permisapi-mcp
```

Puis dans la config Claude Desktop, remplacez `"command": "permisapi-mcp"` par :

```json
{
  "mcpServers": {
    "permisapi": {
      "command": "uvx",
      "args": ["--python", "3.11", "permisapi-mcp"],
      "env": { "PERMISAPI_KEY": "pk_live_VOTRE_CLE" }
    }
  }
}
```

**Solution B : upgrade Python système**

- macOS : `brew install python@3.11`
- Windows : télécharger https://www.python.org/downloads/ et cocher "Add to PATH"
- Linux : `sudo apt install python3.11` (ou équivalent distro)

Puis `pip3.11 install permisapi-mcp`.

Guide setup complet + autres FAQ : [https://permisapi.fr/mcp](https://permisapi.fr/mcp)

## Licence

MIT.

## Support

evan@permisapi.fr : réponse 24-48h sur les plans Pro+, 24-72h sur les autres.

## Code source

[github.com/Evan-Crx/permisapi-mcp](https://github.com/Evan-Crx/permisapi-mcp)
