# permisapi-mcp

Serveur **MCP** (Model Context Protocol, Anthropic) pour [PermisAPI](https://permisapi.fr).

Permet à **Claude Desktop**, **Cursor**, **Windsurf** ou tout client MCP-compatible
de consulter **1,2 M+ permis de construire de France** (Sitadel 2014-2026,
résidentiel + non-résidentiel, depuis 2014) en langage naturel.

11 outils disponibles : recherche par adresse, score d'opportunité Marchand de
Biens, prix au m² des ventes voisines sur 12 ans, zonage urbanisme PLU, risques
(inondation, sismique, ICPE), parcelle cadastre DGFiP, **bâtiments existants
(terrain nu vs déjà bâti)** et enrichissement de liste client.

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
> *« Quel est le zonage PLU du permis PC07404021K1 ? »*

**Note sur le périmètre géographique** :
- **Free** : 1 département au choix (Paris par défaut). Modifiable via le dashboard.
- **Explorer** : 5 départements au choix (Paris/Lyon/Marseille/Bordeaux/Toulouse par défaut).
- **Pro / Business / Enterprise** : France entière, aucune restriction géographique.

Les exemples ci-dessus ciblent Paris (75) pour qu'ils fonctionnent immédiatement sur tous les plans. Si vous êtes sur Explorer avec ses départements par défaut, vous pouvez aussi demander "à Lyon", "à Bordeaux", etc.

## Configuration Cursor / Windsurf / autres clients

Voir le guide complet : [https://permisapi.fr/mcp](https://permisapi.fr/mcp)

## Tools disponibles (11)

| Tool | Endpoint | Plan |
|---|---|:---:|
| `search_permits` | GET /v1/permits (13 filtres) | Free |
| `get_permit_details` | GET /v1/permits/{num_pa} | Free |
| `fuzzy_search_addresses` | GET /v1/search?q=text (pg_trgm fuzzy) | Free |
| `find_dvf_neighbors` | GET /v1/permits/{num_pa}/dvf (12 ans : Cerema DVF+ 2014-2020 fusionné Geo-DVF 2021-2025) | Pro |
| `get_mdb_score` | GET /v1/permits/{num_pa}/score (Score MDB v0.2, 10 signaux) | Pro |
| `get_plu_zoning` | GET /v1/permits/{num_pa}/plu | Pro |
| `get_risks` | GET /v1/permits/{num_pa}/risks (Géorisques BRGM) | Pro |
| `get_parcelle_geometry` | GET /v1/permits/{num_pa}/parcelle (cadastre DGFiP) | Pro |
| **`get_existing_buildings`** | **GET /v1/permits/{num_pa}/batiments-existants** (terrain nu vs bâti, use case MDB) | Pro |
| `get_permit_full_view` | GET /v1/permits/{num_pa}/360 (composite 6-en-1) | Pro |
| `bulk_enrich_list` | POST /v1/permits/bulk-enrich (croise liste client jusqu'à 1 000 lignes) | Business |

## Sécurité

- La clé API reste **côté user** (env var locale, jamais transmise au LLM)
- Le LLM voit uniquement les arguments des tools (pas la clé)
- Validation stricte des inputs (regex sur `num_pa`, ranges Pydantic)
- 10 outils en consultation pure (GET) + 1 outil de croisement de liste (POST
  bulk_enrich_list, lecture seule côté PermisAPI : renvoie les permis qui
  matchent les adresses du client, sans stocker la liste)

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
