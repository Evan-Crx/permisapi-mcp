"""Tools metier : appels a l'API PermisAPI, independants du protocole MCP.

Cette couche est testable sans avoir le `mcp` package installe : on
mock juste httpx ou on utilise MockTransport. Le server.py construit
les Tool MCP a partir d'ici.

7 tools exposes :
  1. search_permits        : GET /v1/permits avec filtres
  2. get_permit_details    : GET /v1/permits/{num_pa}
  3. find_dvf_neighbors    : GET /v1/permits/{num_pa}/dvf
  4. get_mdb_score         : GET /v1/permits/{num_pa}/score
  5. get_plu_zoning        : GET /v1/permits/{num_pa}/plu
  6. get_risks             : GET /v1/permits/{num_pa}/risks
  7. get_permit_full_view  : GET /v1/permits/{num_pa}/360 (composite 6-en-1)

Securite : la cle API du user est lue depuis l'env PERMISAPI_KEY au
demarrage du serveur, jamais transmise via les arguments d'un tool.
Pas de risque qu'un LLM puisse "leaker" la cle dans une reponse.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

PERMISAPI_BASE = os.environ.get("PERMISAPI_BASE_URL", "https://api.permisapi.fr")
HTTP_TIMEOUT = 15.0


class PermisapiError(Exception):
    """Erreur retournee par l'API PermisAPI (4xx / 5xx)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"PermisAPI {status_code} : {detail}")


def _api_key() -> str:
    """Lit la cle PermisAPI depuis l'env. Raise si absente."""
    key = os.environ.get("PERMISAPI_KEY")
    if not key:
        raise RuntimeError(
            "PERMISAPI_KEY non configuree. Ajoute-la dans la config "
            "MCP de ton client (Claude Desktop, Cursor, etc). Obtiens "
            "une cle gratuite sur https://permisapi.fr/#pricing"
        )
    return key


async def _http_get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Helper GET sur PermisAPI avec auth + gestion d'erreur."""
    headers = {
        "X-API-Key": _api_key(),
        "Accept": "application/json",
        "User-Agent": "permisapi-mcp/0.2.1",
    }
    own_client = client is None
    c = client or httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    try:
        resp = await c.get(f"{PERMISAPI_BASE}{path}", params=params, headers=headers)
    finally:
        if own_client:
            await c.aclose()

    if resp.status_code >= 400:
        try:
            payload = resp.json()
            detail = payload.get("detail") or payload.get("message") or resp.text
        except (ValueError, AttributeError):
            detail = resp.text[:200]
        raise PermisapiError(resp.status_code, str(detail))

    return resp.json()


# ----------------------------------------------------------------------------
# Tool definitions (JSON Schema for inputs)
# ----------------------------------------------------------------------------


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_permits",
        "description": (
            "Recherche des permis de construire France avec filtres "
            "combinables : departement, commune, type de permis, etat, "
            "dates, surface min, SIREN demandeur. Retourne une page de "
            "permits avec leurs infos de base. Plan Free OK."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dep_code": {
                    "type": "string",
                    "description": "Code departement INSEE 2 chars (ex 75 Paris, 33 Bordeaux). Requis pour Free/Explorer.",
                },
                "comm_code": {
                    "type": "string",
                    "description": "Code commune INSEE 5 chars (ex 75116 Paris 16e).",
                },
                "permit_type": {
                    "type": "string",
                    "enum": [
                        "PC_LOGEMENT", "PC_LOCAUX",
                        "DP_LOGEMENT", "DP_LOCAUX",
                        "PA", "PD",
                    ],
                },
                "etat_pa": {
                    "type": "integer",
                    "description": "Etat permis : 1=Accorde, 2=Tacite, 3=Refuse, 4=Irrecevable, 5=Retrait, 6=Acheve.",
                    "minimum": 1, "maximum": 6,
                },
                "date_from": {"type": "string", "description": "YYYY-MM-DD."},
                "date_to": {"type": "string", "description": "YYYY-MM-DD."},
                "min_superficie": {
                    "type": "integer", "description": "Surface terrain min en m².",
                },
                "siren_dem": {
                    "type": "string",
                    "description": "SIREN du demandeur (9 chiffres).",
                },
                "sort": {
                    "type": "string",
                    "description": (
                        "Tri du resultat. Champs autorises : "
                        "date_reelle_autorisation, date_depot, an_depot, "
                        "superficie_terrain. Prefixe '-' pour descendant "
                        "(ex '-superficie_terrain' pour les plus grandes "
                        "surfaces en premier). Defaut '-date_reelle_autorisation'."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
        },
    },
    {
        "name": "get_permit_details",
        "description": (
            "Recupere tous les details d'un permis a partir de son "
            "identifiant Sitadel (num_pa) : adresse complete, demandeur, "
            "dates, surface, parcelle cadastre, lat/lng. Plan Free OK."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {
                    "type": "string",
                    "description": "Identifiant Sitadel unique (ex PC07404021K1).",
                },
            },
            "required": ["num_pa"],
        },
    },
    {
        "name": "find_dvf_neighbors",
        "description": (
            "Pour un permis, retourne les top transactions immobilieres "
            "DVF voisines (5 ans glissants). Permet d'estimer la valeur "
            "fonciere du quartier. Plan Pro+ uniquement."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
                "type_local": {
                    "type": "string",
                    "description": "CSV des types : 1=Maison, 2=Appartement, 3=Dependance, 4=Local commercial.",
                },
                "min_year": {"type": "integer", "minimum": 2014, "maximum": 2100},
            },
            "required": ["num_pa"],
        },
    },
    {
        "name": "get_mdb_score",
        "description": (
            "Calcule le Score Opportunite Marchand de Biens v0.1 pour "
            "un permis (note 0-100 + tier low/medium/high/premium + "
            "breakdown de 7 signaux ponderes). Plan Pro+ uniquement."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {"type": "string"},
            },
            "required": ["num_pa"],
        },
    },
    {
        "name": "get_plu_zoning",
        "description": (
            "Retourne le zonage urbanisme PLU au point geocode du permis "
            "(UA/UB urbain, AU a urbaniser, A agricole, N naturelle) avec "
            "verdict booleen constructible et raison juridique. Source "
            "Geoportail de l'Urbanisme. Plan Pro+ uniquement."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {"type": "string"},
            },
            "required": ["num_pa"],
        },
    },
    {
        "name": "get_risks",
        "description": (
            "Risques naturels et technologiques (inondation, seisme, "
            "argile, ICPE proches) connus sur la commune du permis. "
            "Score agrege 0-100 + tier. Source Georisques BRGM. Plan "
            "Pro+ uniquement."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {"type": "string"},
            },
            "required": ["num_pa"],
        },
    },
    {
        "name": "get_permit_full_view",
        "description": (
            "Vue 360 complete d'un permis en 1 seul appel : detail + "
            "sirene + dvf + score MDB + zonage PLU + risques. Le moyen "
            "le plus efficace pour analyser un permis quand tu veux "
            "tout d'un coup au lieu d'appeler 6 outils separes. "
            "Cout : 6 unites de quota Pro+ (1 par sous-feature, "
            "identique a 6 calls separes). Pour Free / Explorer, retourne "
            "uniquement le detail (cout 1 unite). En cas d'echec d'une "
            "sous-feature (ex: PLU timeout), le champ vaut null et "
            "l'erreur est listee dans fetch_errors. Latence typique 5-7s."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {
                    "type": "string",
                    "description": "Identifiant Sitadel unique (ex PC07404021K1).",
                },
            },
            "required": ["num_pa"],
        },
    },
]


# ----------------------------------------------------------------------------
# Tool implementations
# ----------------------------------------------------------------------------


def _validate_num_pa(num_pa: Any) -> str:
    """Defense en profondeur cote MCP : valide que num_pa est string et
    safe (pas d'injection URL).
    """
    if not isinstance(num_pa, str):
        raise ValueError(f"num_pa doit etre une string, recu : {type(num_pa).__name__}")
    if not num_pa or len(num_pa) > 50:
        raise ValueError("num_pa doit faire entre 1 et 50 caracteres")
    # Whitelist : alphanumeric + space + - + _
    for c in num_pa:
        if not (c.isalnum() or c in " -_"):
            raise ValueError(f"num_pa contient un caractere interdit : {c!r}")
    return num_pa


async def search_permits(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits avec filtres."""
    params: dict[str, Any] = {}
    for k in (
        "dep_code", "comm_code", "permit_type", "etat_pa",
        "date_from", "date_to", "min_superficie", "siren_dem",
        "sort",
    ):
        v = arguments.get(k)
        if v is not None and v != "":
            params[k] = v
    limit = arguments.get("limit", 20)
    params["limit"] = max(1, min(50, int(limit)))
    return await _http_get("/v1/permits", params=params, client=client)


async def get_permit_details(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits/{num_pa}."""
    num_pa = _validate_num_pa(arguments.get("num_pa"))
    return await _http_get(f"/v1/permits/{num_pa}", client=client)


async def find_dvf_neighbors(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits/{num_pa}/dvf."""
    num_pa = _validate_num_pa(arguments.get("num_pa"))
    params: dict[str, Any] = {}
    if "limit" in arguments and arguments["limit"] is not None:
        params["limit"] = max(1, min(5, int(arguments["limit"])))
    if arguments.get("type_local"):
        params["type_local"] = arguments["type_local"]
    if arguments.get("min_year") is not None:
        params["min_year"] = int(arguments["min_year"])
    return await _http_get(
        f"/v1/permits/{num_pa}/dvf", params=params or None, client=client
    )


async def get_mdb_score(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits/{num_pa}/score."""
    num_pa = _validate_num_pa(arguments.get("num_pa"))
    return await _http_get(f"/v1/permits/{num_pa}/score", client=client)


async def get_plu_zoning(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits/{num_pa}/plu."""
    num_pa = _validate_num_pa(arguments.get("num_pa"))
    return await _http_get(f"/v1/permits/{num_pa}/plu", client=client)


async def get_risks(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits/{num_pa}/risks."""
    num_pa = _validate_num_pa(arguments.get("num_pa"))
    return await _http_get(f"/v1/permits/{num_pa}/risks", client=client)


async def get_permit_full_view(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits/{num_pa}/360 : vue agregee 6-en-1.

    Cout 6 unites de quota Pro+ (1 par sous-feature). Le payload
    inclut les memes shapes que les 6 endpoints individuels, agreges
    sous les cles detail/sirene/dvf/score/plu/risks. Voir le champ
    fetch_errors pour les sous-features qui ont fail (best-effort).
    """
    num_pa = _validate_num_pa(arguments.get("num_pa"))
    return await _http_get(f"/v1/permits/{num_pa}/360", client=client)


# ----------------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------------


TOOL_HANDLERS = {
    "search_permits": search_permits,
    "get_permit_details": get_permit_details,
    "find_dvf_neighbors": find_dvf_neighbors,
    "get_mdb_score": get_mdb_score,
    "get_plu_zoning": get_plu_zoning,
    "get_risks": get_risks,
    "get_permit_full_view": get_permit_full_view,
}


async def call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Dispatch un appel de tool MCP vers la bonne fonction handler.

    Retourne du texte JSON-encoded (le format attendu par MCP TextContent).
    Les erreurs sont retournees comme texte d'erreur, pas raised, car
    MCP attend toujours du contenu (pas une exception).
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Tool inconnu : {name}"}, ensure_ascii=False)

    try:
        result = await handler(arguments, client=client)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except PermisapiError as exc:
        return json.dumps(
            {
                "error": "permisapi_error",
                "status_code": exc.status_code,
                "detail": exc.detail,
            },
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps(
            {"error": "validation", "detail": str(exc)},
            ensure_ascii=False,
        )
    except RuntimeError as exc:
        return json.dumps(
            {"error": "config", "detail": str(exc)},
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {"error": "internal", "detail": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )
