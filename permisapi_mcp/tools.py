"""Tools métier : appels à l'API PermisAPI, indépendants du protocole MCP.

Cette couche est testable sans avoir le `mcp` package installé : on
mock juste httpx ou on utilise MockTransport. Le server.py construit
les Tool MCP à partir d'ici.

11 tools exposés :
  1. search_permits         : GET /v1/permits avec filtres (Free)
  2. get_permit_details     : GET /v1/permits/{num_pa} (Free)
  3. find_dvf_neighbors     : GET /v1/permits/{num_pa}/dvf (Pro, 12 ans)
  4. get_mdb_score          : GET /v1/permits/{num_pa}/score (Pro, v0.2 10 signaux)
  5. get_plu_zoning         : GET /v1/permits/{num_pa}/plu (Pro)
  6. get_risks              : GET /v1/permits/{num_pa}/risks (Pro)
  7. get_parcelle_geometry  : GET /v1/permits/{num_pa}/parcelle (Pro, cadastre DGFiP)
  8. get_existing_buildings : GET /v1/permits/{num_pa}/batiments-existants (Pro, terrain nu vs bâti)
  9. bulk_enrich_list       : POST /v1/permits/bulk-enrich (Business, croise liste client)
  10. fuzzy_search_addresses : GET /v1/search?q=text (Free, pg_trgm fuzzy)
  11. get_permit_full_view  : GET /v1/permits/{num_pa}/360 (Pro, composite 6-en-1)

Sécurité : la clé API du user est lue depuis l'env PERMISAPI_KEY au
démarrage du serveur, jamais transmise via les arguments d'un tool.
Pas de risque qu'un LLM puisse "leaker" la clé dans une réponse.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

PERMISAPI_BASE = os.environ.get("PERMISAPI_BASE_URL", "https://api.permisapi.fr")
HTTP_TIMEOUT = 15.0


class PermisapiError(Exception):
    """Erreur retournée par l'API PermisAPI (4xx / 5xx)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"PermisAPI {status_code} : {detail}")


def _api_key() -> str:
    """Lit la clé PermisAPI depuis l'env. Raise si absente."""
    key = os.environ.get("PERMISAPI_KEY")
    if not key:
        raise RuntimeError(
            "PERMISAPI_KEY non configurée. Ajoute-la dans la config "
            "MCP de ton client (Claude Desktop, Cursor, etc). Obtiens "
            "une clé gratuite sur https://permisapi.fr/#pricing"
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
        "User-Agent": "permisapi-mcp/0.5.0",
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


async def _http_post(
    path: str,
    json: dict[str, Any] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Helper POST sur PermisAPI avec auth + gestion d'erreur."""
    headers = {
        "X-API-Key": _api_key(),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "permisapi-mcp/0.5.0",
    }
    own_client = client is None
    c = client or httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    try:
        resp = await c.post(f"{PERMISAPI_BASE}{path}", json=json, headers=headers)
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
            "Recherche des permis de construire de France avec filtres "
            "combinables : département, commune, type de permis, état, "
            "dates, surface min, SIREN demandeur. Retourne une page de "
            "permits avec leurs infos de base. Plan Free OK."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dep_code": {
                    "type": "string",
                    "description": "Code département INSEE 2 chars (ex 75 Paris, 33 Bordeaux). Requis pour Free/Explorer.",
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
                    "description": "État permis : 1=Accordé, 2=Tacite, 3=Refusé, 4=Irrecevable, 5=Retrait, 6=Achevé.",
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
                        "Tri du résultat. Champs autorisés : "
                        "date_reelle_autorisation, date_depot, an_depot, "
                        "superficie_terrain. Préfixe '-' pour descendant "
                        "(ex '-superficie_terrain' pour les plus grandes "
                        "surfaces en premier). Défaut '-date_reelle_autorisation'."
                    ),
                },
                "min_score": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": (
                        "Filtre les permis avec un Score Opportunité MDB "
                        ">= N (0-100). Réservé aux plans Pro+. Permet de "
                        "shortlister les top opportunités en 1 call. "
                        "Coût quota = `limit` unités (pas 1)."
                    ),
                },
                "max_risk": {
                    "type": "string",
                    "enum": ["low", "moderate", "high", "critical"],
                    "description": (
                        "Filtre les permis dont la commune est au-dessus "
                        "du tier de risque spécifié (Géorisques BRGM). "
                        "Exemple 'moderate' retourne uniquement low + "
                        "moderate. Réservé aux plans Pro+. Coût quota = "
                        "`limit` unités (pas 1). Use case marchand de "
                        "biens : exclure d'office les zones inondables / "
                        "Seveso / sismiques eleves."
                    ),
                },
                "plu_zone_type": {
                    "type": "string",
                    "enum": ["U", "AU", "A", "N", "OTHER"],
                    "description": (
                        "Filtre par prefix de zonage urbanisme PLU. U = "
                        "urbain constructible, AU = a urbaniser, A = "
                        "agricole non constructible, N = naturelle non "
                        "constructible. Réservé aux plans Pro+. Coût "
                        "quota = limit unites."
                    ),
                },
                "plu_constructible": {
                    "type": "boolean",
                    "description": (
                        "Filtre par verdict de constructibilite PLU. "
                        "true = uniquement zones constructibles (U + AU). "
                        "false = uniquement zones non constructibles (A + N). "
                        "Réservé aux plans Pro+. Coût quota = limit unites. "
                        "Combine avec min_score pour shortlister les vraies "
                        "opportunités MDB."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
        },
    },
    {
        "name": "get_permit_details",
        "description": (
            "Recupere tous les détails d'un permis a partir de son "
            "identifiant Sitadel (num_pa) : adresse complète, demandeur, "
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
            "Calcule le Score Opportunité Marchand de Biens v0.1 pour "
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
            "Retourne le zonage urbanisme PLU au point géocodé du permis "
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
            "Score agrege 0-100 + tier. Source Géorisques BRGM. Plan "
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
        "name": "get_parcelle_geometry",
        "description": (
            "Géométrie précise de la parcelle cadastre DGFiP du permis "
            "(via Etalab open data). Retourne un GeoJSON Polygon WGS84 "
            "+ surface mesurée en m2 + identifiant Etalab. Permet de "
            "visualiser le polygon exact de la parcelle sur une carte "
            "(vs juste le point lat/lng adresse). Plan Pro+ uniquement. "
            "404 si la commune n'est pas encore en cache (rare, hors "
            "couverture Etalab DOM très récents)."
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
        "name": "get_existing_buildings",
        "description": (
            "Liste les bâtiments cadastraux déjà construits sur la "
            "parcelle du permit. Use case CRITIQUE marchand de biens : "
            "distinguer parcelle nue (vraie construction neuve, value-add "
            "max) vs parcelle bâtie (extension/rénovation, value-add "
            "moindre). Retourne nb_batiments total + décompte par type "
            "(bâti dur / bâti léger / autre) + flag parcelle_nue boolean "
            "+ détails individuels (id Etalab, type label FR, centroïde, "
            "dates création/MAJ cadastre). Source : cadastre.data.gouv.fr "
            "via Etalab (DGFiP). Plan Pro+ uniquement. Coût 1 unité quota. "
            "404 si le permit n'a pas de polygone cadastre disponible "
            "(rare, ~15% des permits)."
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
        "name": "bulk_enrich_list",
        "description": (
            "Croisez une liste fournie par l'utilisateur (max 1000 lignes) "
            "avec les permis de France pour recuperer en 1 appel : permis a "
            "proximite + score d'opportunité + risques + zonage + "
            "parcelle cadastre. Plan Business+ uniquement, coût = 1 unité "
            "quota par ligne. Use case : enrichir une liste prospects/"
            "patrimoine. 3 modes par ligne : (lat+lng) ou (adresse) ou "
            "(commune+section+numero). Le champ 'ref' identifié chaque "
            "ligne dans la response."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1000,
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "string", "description": "Identifiant client (echo)"},
                            "lat": {"type": "number"},
                            "lng": {"type": "number"},
                            "adresse": {"type": "string"},
                            "commune": {"type": "string"},
                            "section": {"type": "string"},
                            "numero": {"type": "string"},
                        },
                        "required": ["ref"],
                    },
                },
                "radius_m": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 10000,
                    "default": 500,
                },
                "max_matches": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 3,
                },
            },
            "required": ["rows"],
        },
    },
    {
        "name": "fuzzy_search_addresses",
        "description": (
            "Recherche fuzzy par texte libre sur les adresses (rue, "
            "ville, lieudit). Utilise pg_trgm côté DB : insensible aux "
            "accents et à la casse, tolérant aux typos. Idéal pour "
            "trouver un permis quand on connaît l'adresse approximative "
            "mais pas le code postal ou commune INSEE précis. Tous "
            "plans (avec respect du scope géo). Coût 1 unité quota. "
            "Exemple : 'rue victor hugo paris', 'cours de l ile bordeaux'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 100,
                    "description": "Texte libre a chercher (min 2 chars).",
                },
                "dep_code": {
                    "type": "string",
                    "description": "Restreindre a un département spécifique (ex '75').",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                },
            },
            "required": ["q"],
        },
    },
    {
        "name": "get_permit_full_view",
        "description": (
            "Vue 360 complète d'un permis en 1 seul appel : détail + "
            "sirene + dvf + score MDB + zonage PLU + risques. Le moyen "
            "le plus efficace pour analyser un permis quand tu veux "
            "tout d'un coup au lieu d'appeler 6 outils séparés. "
            "Coût : 6 unités de quota Pro+ (1 par sous-feature, "
            "identique à 6 calls séparés). Pour Free / Explorer, retourne "
            "uniquement le détail (coût 1 unité). En cas d'échec d'une "
            "sous-feature (ex: PLU timeout), le champ vaut null et "
            "l'erreur est listée dans fetch_errors. Latence typique 5-7s."
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
    """Défense en profondeur côté MCP : valide que num_pa est string et
    safe (pas d'injection URL).
    """
    if not isinstance(num_pa, str):
        raise ValueError(f"num_pa doit être une string, reçu : {type(num_pa).__name__}")
    if not num_pa or len(num_pa) > 50:
        raise ValueError("num_pa doit faire entre 1 et 50 caractères")
    # Whitelist : alphanumeric + space + - + _
    for c in num_pa:
        if not (c.isalnum() or c in " -_"):
            raise ValueError(f"num_pa contient un caractère interdit : {c!r}")
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
        "sort", "min_score", "max_risk", "plu_zone_type", "plu_constructible",
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


async def fuzzy_search_addresses(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/search?q=text : fuzzy search adresses via pg_trgm.

    Tous plans (avec respect du scope geo). Coût 1 unité quota.
    Retourne une liste de PermitSummary tries par similarity desc.
    """
    q = arguments.get("q")
    if not isinstance(q, str) or len(q) < 2:
        raise ValueError("q (texte) requis, min 2 chars")
    params: dict[str, Any] = {"q": q}
    dep = arguments.get("dep_code")
    if dep:
        params["dep_code"] = dep
    limit = arguments.get("limit", 20)
    if isinstance(limit, int) and 1 <= limit <= 50:
        params["limit"] = limit
    return await _http_get("/v1/search", params=params, client=client)


async def bulk_enrich_list(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """POST /v1/permits/bulk-enrich : croise une liste fournie par
    l'utilisateur (max 1000 lignes) avec les permis de France.

    Plan Business+ uniquement. Coût = 1 unité quota par ligne.
    """
    rows = arguments.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows (array) requis, min 1 ligne")
    body: dict[str, Any] = {"rows": rows}
    radius = arguments.get("radius_m")
    if isinstance(radius, int) and 10 <= radius <= 10_000:
        body["radius_m"] = radius
    max_matches = arguments.get("max_matches")
    if isinstance(max_matches, int) and 1 <= max_matches <= 10:
        body["max_matches"] = max_matches
    return await _http_post("/v1/permits/bulk-enrich", json=body, client=client)


async def get_parcelle_geometry(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits/{num_pa}/parcelle : géométrie cadastre DGFiP.

    Retourne un GeoJSON Polygon de la parcelle + surface mesurée DGFiP +
    identifiant Etalab. Permet de visualiser le polygon précis sur
    une carte. Plan Pro+ uniquement. 404 si pas en cache (rare).
    """
    num_pa = _validate_num_pa(arguments.get("num_pa"))
    return await _http_get(f"/v1/permits/{num_pa}/parcelle", client=client)


async def get_existing_buildings(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits/{num_pa}/batiments-existants : bâtiments cadastraux.

    Use case marchand de biens : distinguer parcelle nue (vraie
    construction neuve) vs parcelle bâtie (extension/rénovation).
    Plan Pro+ uniquement. Coût 1 unité quota.
    """
    num_pa = _validate_num_pa(arguments.get("num_pa"))
    return await _http_get(
        f"/v1/permits/{num_pa}/batiments-existants", client=client
    )


async def get_permit_full_view(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits/{num_pa}/360 : vue agrégée 6-en-1.

    Coût 6 unités de quota Pro+ (1 par sous-feature). Le payload
    inclut les mêmes shapes que les 6 endpoints individuels, agrégés
    sous les clés detail/sirene/dvf/score/plu/risks. Voir le champ
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
    "get_parcelle_geometry": get_parcelle_geometry,
    "get_existing_buildings": get_existing_buildings,
    "fuzzy_search_addresses": fuzzy_search_addresses,
    "bulk_enrich_list": bulk_enrich_list,
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
    Les erreurs sont retournées comme texte d'erreur, pas raised, car
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
