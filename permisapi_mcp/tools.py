"""Tools métier : appels à l'API PermisAPI, indépendants du protocole MCP.

Cette couche est testable sans avoir le `mcp` package installé : on
mock juste httpx ou on utilise MockTransport. Le server.py construit
les Tool MCP à partir d'ici.

16 tools exposés :
  1. search_permits         : GET /v1/permits avec filtres (Free)
  2. get_permit_details     : GET /v1/permits/{num_pa} (Free)
  3. find_dvf_neighbors     : GET /v1/permits/{num_pa}/dvf (Pro, 12 ans)
  4. get_mdb_score          : GET /v1/permits/{num_pa}/score (Pro, v0.3 11 signaux)
  5. get_score_explanation  : GET /v1/permits/{num_pa}/score/explain (Pro, transparence 11 signaux + interprétations FR)
  6. get_plu_zoning         : GET /v1/permits/{num_pa}/plu (Pro)
  7. get_risks              : GET /v1/permits/{num_pa}/risks (Pro)
  8. get_parcelle_geometry  : GET /v1/permits/{num_pa}/parcelle (Pro, cadastre DGFiP)
  9. get_existing_buildings : GET /v1/permits/{num_pa}/batiments-existants (Pro, terrain nu vs bâti)
  10. get_parcelle_by_id     : GET /v1/parcelles/{id_parcelle} (Pro, lookup direct cadastre DGFiP)
  11. search_permits_in_polygon : POST /v1/permits/inside-polygon (Business, ZAC custom)
  12. get_commune_density_stats : GET /v1/stats/commune/{code}/density (Business, BI agrégé)
  13. get_neighbor_parcels  : GET /v1/permits/{num_pa}/parcelles-voisines (Pro, pattern MDB local)
  14. bulk_enrich_list       : POST /v1/permits/bulk-enrich (Business, croise liste client)
  15. fuzzy_search_addresses : GET /v1/search?q=text (Free, pg_trgm fuzzy)
  16. get_permit_full_view  : GET /v1/permits/{num_pa}/360 (Pro, composite 6-en-1)

Sécurité : la clé API du user est lue depuis l'env PERMISAPI_KEY au
démarrage du serveur, jamais transmise via les arguments d'un tool.
Pas de risque qu'un LLM puisse "leaker" la clé dans une réponse.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
from contextlib import contextmanager
from typing import Any, Iterator

import httpx

PERMISAPI_BASE = os.environ.get("PERMISAPI_BASE_URL", "https://api.permisapi.fr")
HTTP_TIMEOUT = 15.0


# Context var pour scoper la clé API à la requête courante.
# Mode stdio (Claude Desktop) : continue d'utiliser PERMISAPI_KEY en env var.
# Mode SSE hosted : le serveur extrait la clé du header Authorization de
# chaque requête HTTP et la pousse ici via `use_api_key()`.
_current_api_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "permisapi_key", default=None
)


class PermisapiError(Exception):
    """Erreur retournée par l'API PermisAPI (4xx / 5xx)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"PermisAPI {status_code} : {detail}")


@contextmanager
def use_api_key(key: str) -> Iterator[None]:
    """Scope la clé PermisAPI au temps d'un appel MCP.

    Utilisé par le serveur SSE hosted pour injecter la clé extraite du
    header Authorization de la requête entrante. Restore proprement la
    valeur précédente à la sortie du with, sûr en concurrent (ContextVar
    est async-safe et thread-safe).
    """
    token = _current_api_key.set(key)
    try:
        yield
    finally:
        _current_api_key.reset(token)


def _api_key() -> str:
    """Résout la clé PermisAPI à partir du contexte courant.

    Ordre de priorité :
    1. ContextVar `_current_api_key` (mode SSE hosted)
    2. Env var `PERMISAPI_KEY` (mode stdio Claude Desktop / Cursor)

    Raise RuntimeError si aucune source.
    """
    key = _current_api_key.get()
    if key:
        return key
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
        "User-Agent": "permisapi-mcp/0.5.6",
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
        "User-Agent": "permisapi-mcp/0.5.6",
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
            "permits avec leurs infos de base. Plan Free OK. "
            "**Coût quota = nombre de permits retournés** (max `limit`, "
            "min 1). Exemple : limit=20 et 15 permits matchent => 15 "
            "unités décomptées. Anti-exfiltration depuis 2026-05-16 : si "
            "tu cherches a couvrir un département entier, prefere une "
            "page raisonnable (limit=10-20) et arrete-toi quand l'user a "
            "ce qu'il veut, plutôt que paginer agressivement."
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
                    "description": (
                        "Type Sitadel du permis : `PC_LOGEMENT` (permis "
                        "de construire logement), `PC_LOCAUX` (PC locaux "
                        "commerciaux/industriels), `DP_LOGEMENT` "
                        "(déclaration préalable logement), `DP_LOCAUX` "
                        "(DP locaux), `PA` (permis d'aménager : "
                        "lotissements), `PD` (permis de démolir)."
                    ),
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
                        "date_reelle_autorisation, an_depot, "
                        "superficie_terrain. Alias acceptés : date_decision, "
                        "date, year (remappés vers date_reelle_autorisation), "
                        "date_depot (remappé vers an_depot, granularité année "
                        "car Sitadel SDES ne publie pas la date complète de "
                        "dépôt). Préfixe '-' pour descendant (ex "
                        "'-superficie_terrain' pour les plus grandes surfaces "
                        "en premier). Défaut '-date_reelle_autorisation'."
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
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                    "description": (
                        "Nombre max de permits retournés par appel. Tri "
                        "par défaut sur date_reelle_autorisation desc "
                        "(les plus récents en tête). Coût quota = nombre "
                        "réel de permits retournés (max `limit`, min 1). "
                        "Pour Free 500 unités/mois, prefere limit=10-20 et "
                        "stoppe quand l'user a ce qu'il veut au lieu de "
                        "paginer agressivement."
                    ),
                },
            },
        },
    },
    {
        "name": "get_permit_details",
        "description": (
            "Récupère TOUS les détails connus d'un permis à partir de son "
            "identifiant Sitadel `num_pa`. Behavior : appelle "
            "`GET /v1/permits/{num_pa}` qui retourne l'adresse postale "
            "complète, la commune INSEE, le code département, les dates "
            "(dépôt, décision réelle d'autorisation, DAACT), la surface "
            "du terrain en m², l'état administratif (Accordé / Tacite / "
            "Refusé...), le type de permis (PC_LOGEMENT, DP_LOCAUX, PA, "
            "PD...), les coordonnées GPS lat/lng géocodées, et la "
            "référence cadastrale (sec_cadastre1 + num_cadastre1). "
            "Purpose : à utiliser quand l'user fournit un `num_pa` "
            "spécifique et veut tout savoir dessus en 1 appel. Usage "
            "guideline : préfère ce tool à `search_permits` quand "
            "l'identifiant est connu (1 unité quota au lieu de N). "
            "Coût : 1 unité quota. Plan Free OK (dans le scope géo de "
            "l'user)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {
                    "type": "string",
                    "description": (
                        "Identifiant Sitadel unique du permis. Format : 13 "
                        "caractères alphanumériques sans préfixe (ex "
                        "`0930662500027` pour un permis Seine-Saint-Denis). "
                        "Pas de slashes, pas d'espaces. À récupérer via "
                        "`search_permits` ou `fuzzy_search_addresses` si "
                        "l'user fournit juste une adresse."
                    ),
                    "minLength": 1,
                    "maxLength": 50,
                },
            },
            "required": ["num_pa"],
        },
    },
    {
        "name": "find_dvf_neighbors",
        "description": (
            "Retourne les top N transactions immobilières DVF voisines "
            "du permis (12 ans glissants : Geo-DVF Etalab 2021-2025 + "
            "Cerema DVF+ 2014-2020 fusionnés). Behavior : appelle "
            "`GET /v1/permits/{num_pa}/dvf` qui fait une jointure spatiale "
            "via les coordonnées GPS du permis et retourne les ventes "
            "passées sur des biens proches avec leur prix, date, type "
            "(maison/appartement/dépendance/local commercial), surface, "
            "nombre de pièces et distance en mètres. Filtre côté serveur "
            "les transactions < 1000 EUR (donations DGFiP). Purpose : "
            "estimer le prix au m² du quartier pour un marchand de biens, "
            "calibrer une offre d'achat, vérifier la cohérence DVF avec un "
            "prix annoncé. Usage guideline : `limit=3` est généralement "
            "suffisant pour un rapide check ; `limit=5` pour une moyenne "
            "plus robuste. Filtre `type_local=1,2` pour exclure les "
            "dépendances/locaux commerciaux si l'user vise du résidentiel. "
            "Coût : 1 unité quota. Plan Pro+ uniquement (Free/Explorer "
            "reçoivent 402)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {
                    "type": "string",
                    "description": (
                        "Identifiant Sitadel unique du permis (ex "
                        "`0930662500027`). 13 caractères alphanumériques."
                    ),
                    "minLength": 1,
                    "maxLength": 50,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "default": 3,
                    "description": (
                        "Nombre max de transactions DVF voisines à "
                        "retourner. Tri par distance croissante. Défaut 3 "
                        "(suffit pour un rapide check). Max 5 pour ce tool "
                        "(les transactions au-delà des 5 plus proches "
                        "perdent en pertinence)."
                    ),
                },
                "type_local": {
                    "type": "string",
                    "description": (
                        "Filtre par type de bien immobilier DVF (CSV "
                        "accepté). Codes DGFiP : `1`=Maison, `2`=Appartement, "
                        "`3`=Dépendance (annexes type garage / cave), "
                        "`4`=Local industriel/commercial. Exemple `1,2` "
                        "pour ne garder que maisons + appartements. Omettre "
                        "pour tous types."
                    ),
                },
                "min_year": {
                    "type": "integer",
                    "minimum": 2014,
                    "maximum": 2100,
                    "description": (
                        "Année minimum de la transaction (filtre les "
                        "transactions plus anciennes). Exemple `2020` "
                        "= seulement 2020-2026. Omettre pour avoir les "
                        "12 ans complets (2014-2026)."
                    ),
                },
            },
            "required": ["num_pa"],
        },
    },
    {
        "name": "get_mdb_score",
        "description": (
            "Calcule (ou récupère depuis le cache materialisé daily) le "
            "Score Opportunité Marchand de Biens **v0.3** d'un permis. "
            "Behavior : appelle `GET /v1/permits/{num_pa}/score` qui "
            "retourne une note 0-100 + tier (`low` / `medium` / `high` / "
            "`premium`) + breakdown des 11 signaux pondérés (dvf_density, "
            "dvf_value, plu_constructible, plu_zone_type, risk_score, "
            "sirene_quality, building_density, surface, type_permit, "
            "etat_admin, dep_focus) avec leur poids et contribution au "
            "score final. Inclut `version: \"v0.3\"`, `method: "
            "\"materialise (precomputed)\"` quand le score vient du cron "
            "daily 04h UTC (le cas typique), ou `live` quand recalculé à "
            "la demande. Purpose : aider un marchand de biens à shortlister "
            "les permis à fort potentiel value-add sans inspecter chaque "
            "permis manuellement. Usage guideline : un score >= 70 est "
            "généralement worth investigating, >= 85 est top-tier "
            "(`premium`). Combine avec `find_dvf_neighbors` pour valider "
            "la justification prix terrain. Coût : 1 unité quota. Plan "
            "Pro+ uniquement (Free/Explorer reçoivent 402)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {
                    "type": "string",
                    "description": (
                        "Identifiant Sitadel unique du permis (ex "
                        "`0930662500027`). 13 caractères alphanumériques."
                    ),
                    "minLength": 1,
                    "maxLength": 50,
                },
            },
            "required": ["num_pa"],
        },
    },
    {
        "name": "get_score_explanation",
        "description": (
            "Explication transparente du Score Opportunité Marchand de Biens "
            "**v0.3** pour un permis. Behavior : appelle `GET /v1/permits/"
            "{num_pa}/score/explain` qui retourne le score 0-100 + tier + "
            "**les 11 signaux pondérés** avec pour chacun : son libellé FR, "
            "sa doctrine (comment il est calculé en général), son poids "
            "dans le score final, sa valeur calculée 0-100 pour CE permit, "
            "sa contribution finale (valeur × poids), et **une interprétation "
            "FR contextualisée** ('Démolition pure (PD) : top signal MDB', "
            "'850 m² dans le sweet spot MDB', 'Risque critique : "
            "rédhibitoire', etc.). Inclut aussi top 3 drivers (signaux qui "
            "tirent le score vers le haut vs neutre 50) et top 3 drags "
            "(signaux qui le tirent vers le bas) avec leur delta_vs_neutral. "
            "Plus tous les inputs concrets utilisés (transparence totale : "
            "permit_type, superficie_terrain, dep_code, denom_dem, "
            "density_class, plu_zone_type, plu_constructible, risk_tier, "
            "sirene_naf_prefix, nb_batiments_in_parcelle, etc.). Purpose : "
            "audit du score 'pourquoi ce permis est noté 87 ?', "
            "justification de décision MDB, due-diligence transparente. "
            "Usage guideline : compléter get_mdb_score (qui donne juste le "
            "score brut + tier) par get_score_explanation pour comprendre "
            "le pourquoi et pouvoir justifier à un client / banquier / "
            "partenaire. Coût : 2 unités quota (composite calcul + "
            "interprétations). Plan Pro+ uniquement (Free/Explorer "
            "reçoivent 402)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {
                    "type": "string",
                    "description": (
                        "Identifiant Sitadel unique du permis (ex "
                        "`0930662500027`). 13 caractères alphanumériques. "
                        "Récupéré via `search_permits`."
                    ),
                    "minLength": 1,
                    "maxLength": 50,
                },
            },
            "required": ["num_pa"],
        },
    },
    {
        "name": "get_plu_zoning",
        "description": (
            "Retourne le zonage urbanisme PLU/POS au point GPS géocodé du "
            "permis. Behavior : appelle "
            "`GET /v1/permits/{num_pa}/plu` qui interroge le Géoportail de "
            "l'Urbanisme (apicarto.ign.fr) ou retourne le cache local si "
            "déjà fetché. Retourne : `zonage.code` brut (ex `UAa`, `UG`, "
            "`AU1`, `Nh`...), `zonage.libelle` lisible (ex `Zone urbaine "
            "centrale`), `zonage.type_zone` normalisé (`U` urbain "
            "constructible / `AU` à urbaniser / `A` agricole non "
            "constructible / `N` naturelle non constructible), "
            "`zonage.constructible` booléen avec raison juridique, "
            "`zonage.plu_revision_date` date dernière révision PLU. "
            "Purpose : vérifier qu'un projet est constructible avant tout "
            "investissement, identifier les zones à urbaniser (AU) où "
            "investir en amont. Usage guideline : si `has_plu=false` la "
            "commune n'a pas encore digitalisé son PLU (20% des communes "
            "FR en 2026, surtout rural) ; dans ce cas RNU s'applique par "
            "défaut. Coût : 1 unité quota. Plan Pro+ uniquement "
            "(Free/Explorer reçoivent 402)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {
                    "type": "string",
                    "description": (
                        "Identifiant Sitadel unique du permis (ex "
                        "`0930662500027`). 13 caractères alphanumériques. "
                        "Le permis doit être géocodé (lat/lng connus) "
                        "sinon 404."
                    ),
                    "minLength": 1,
                    "maxLength": 50,
                },
            },
            "required": ["num_pa"],
        },
    },
    {
        "name": "get_risks",
        "description": (
            "Retourne les risques naturels et technologiques connus sur la "
            "commune du permis selon Géorisques BRGM. Behavior : appelle "
            "`GET /v1/permits/{num_pa}/risks` qui retourne un score agrégé "
            "0-100 (`risk_score`), un tier qualitatif (`risk_tier` : "
            "`low` / `moderate` / `high` / `critical`) et une liste des "
            "risques détectés avec leur code Géorisques. Codes possibles : "
            "`INONDATION` (zone inondable PPRi), `MVT` (mouvement de "
            "terrain / argile), `SEISME` (zone sismique 2-5), `CYCLONE` "
            "(DOM), `INDUSTRIEL` (ICPE / Seveso à proximité), `RADON` "
            "(potentiel radon catégorie 3), `FEUX_FORET` (PPR feux), "
            "`AVALANCHE`, `NUCLEAIRE` (proximité PPI INB). Chaque risque "
            "inclut `has_ppr` (Plan de Prévention des Risques opposable) "
            "et `ppr_type` (Inondation / Mouvement / Technologique / "
            "etc.). Purpose : informer un acheteur/promoteur des risques "
            "réglementaires qui peuvent affecter la valeur ou la "
            "constructibilité, alimenter l'IAL (Information Acquéreur "
            "Locataire) obligatoire depuis 2006. Use case marchand de "
            "biens : exclure les permis en zone inondable ou Seveso. "
            "Usage guideline : un `risk_tier=high` signale 3+ risques "
            "majeurs avec PPR opposable, vérifier l'éligibilité assurance. "
            "Source : api-prim.developpement-durable.gouv.fr (Géorisques "
            "BRGM officiel). Coût : 1 unité quota. Plan Pro+ uniquement "
            "(Free/Explorer reçoivent 402)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {
                    "type": "string",
                    "description": (
                        "Identifiant Sitadel unique du permis (ex "
                        "`0930662500027`). 13 caractères alphanumériques. "
                        "Le scoring est par commune (pas par parcelle), "
                        "donc 2 permis dans la même commune retournent le "
                        "même `risk_score`."
                    ),
                    "minLength": 1,
                    "maxLength": 50,
                },
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
                    "description": "Identifiant Sitadel unique (ex 0930662500027).",
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
                    "description": "Identifiant Sitadel unique (ex 0930662500027).",
                },
            },
            "required": ["num_pa"],
        },
    },
    {
        "name": "get_parcelle_by_id",
        "description": (
            "Lookup direct cadastre DGFiP par identifiant Etalab 14 chars : "
            "retourne en 1 appel le contexte complet d'une parcelle sans "
            "passer par un permit. Réponse : centroïde lat/lng, contenance "
            "officielle DGFiP en m², commune (code INSEE + nom), département, "
            "section + numero cadastraux + prefix, polygon GeoJSON Polygon "
            "si dispo (récupéré du 1er permit lié qui a cadastre_geom), "
            "compteur bâtiments existants si polygon dispo (ST_Within), "
            "flag parcelle_nue booléen pratique pour marchand de biens. "
            "Use case : 'j'ai vu la parcelle 75104000AA0123, donne-moi tout "
            "ce qu'on en sait + tous les permis historiques associés'. "
            "Cohérent avec /v1/permits/{num_pa}/parcelle mais en lookup "
            "inverse (parcelle -> permits) plutôt que (permit -> parcelle). "
            "Permits associés retournés via matching commune + section + "
            "numero, triés par année de dépôt décroissante, limité à 50. "
            "Pour Paris/Lyon/Marseille, le mapping arrondissement -> "
            "ville-mère est automatique. Plan Pro+ uniquement. Coût quota : "
            "1 (parcelle) + 1 par permis retourné (composite cohérent avec "
            "le pattern bulk_enrich_list et /360). Source : Etalab DGFiP, "
            "35 millions de parcelles France entière, mise à jour mensuelle. "
            "404 si id_parcelle inexistant (vérifier le format 14 chars "
            "alphanumérique majuscule, ex '75104000AA0123')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id_parcelle": {
                    "type": "string",
                    "pattern": "^[A-Z0-9]{14}$",
                    "minLength": 14,
                    "maxLength": 14,
                    "description": (
                        "Identifiant cadastral DGFiP, 14 caractères "
                        "alphanumériques majuscules au format strict "
                        "`{commune_5}{prefix_3}{section_2}{numero_4}`. "
                        "Exemple Paris 4e : `75104000AA0123` (commune 75104, "
                        "prefix 000, section AA, parcelle 0123). Récupérable "
                        "depuis la réponse de get_parcelle_geometry "
                        "(champ `cadastre_id`) ou search_permits (champ "
                        "`cadastre_id` des permits Pro+)."
                    ),
                },
            },
            "required": ["id_parcelle"],
        },
    },
    {
        "name": "search_permits_in_polygon",
        "description": (
            "Recherche les permis de construire dont le point géocodé est "
            "à l'intérieur d'un polygone GeoJSON custom fourni par "
            "l'utilisateur. Killer feature foncière pour les ZAC (Zone "
            "d'Aménagement Concerté), ZA, périmètres d'opération propTech, "
            "zones de chasse marchand de biens hors limites administratives "
            "commune / département. Use case typique : 'donne-moi tous les "
            "permis logement avec un Score MDB >= 70 dans ce polygone que "
            "je viens de dessiner sur la carte'. Plan Business+ uniquement "
            "(Pro 199 EUR reste sur les 14 filtres administratifs standard "
            "de search_permits). Limite surface 1 000 km² (anti-abus). "
            "Coût quota : max(2, nombre de permis retournés) cohérent avec "
            "le pattern composite. Filtres additionnels combinables : "
            "dep_code (pré-filtre index, gain perf si polygon vaste), "
            "permit_type, min_an_depot / max_an_depot, min_score MDB v0.3. "
            "Retourne items + count + polygon_surface_km2 + polygon_center "
            "lat/lng (utile pour centrer une carte sur la zone) + "
            "filters_applied (echo des filtres pour debug). Polygon format "
            "GeoJSON RFC 7946 strict : `type: 'Polygon'`, `coordinates: "
            "[[[lng, lat], ...]]` avec anneau extérieur fermé (1er == "
            "dernier point, minimum 4 points). MultiPolygon non supporté "
            "en V1. Coordonnées WGS84 EPSG:4326."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "polygon": {
                    "type": "object",
                    "description": (
                        "Polygon GeoJSON RFC 7946. Format strict : "
                        "`{type: 'Polygon', coordinates: [[[lng, lat], ...]]}` "
                        "avec anneau extérieur fermé (1er point == dernier "
                        "point, minimum 4 points). Exemple ZAC Paris 4e : "
                        "`{type: 'Polygon', coordinates: [[[2.34, 48.85], "
                        "[2.36, 48.85], [2.36, 48.87], [2.34, 48.87], "
                        "[2.34, 48.85]]]}`. Surface max 1 000 km²."
                    ),
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["Polygon"],
                            "description": "Doit etre 'Polygon' (MultiPolygon non supporté en V1)."
                        },
                        "coordinates": {
                            "type": "array",
                            "description": "Liste de rings (anneau extérieur + trous optionnels). Chaque ring est une liste de points [lng, lat] fermée."
                        }
                    },
                    "required": ["type", "coordinates"]
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 100,
                    "description": "Nombre max de permits retournés (1-500). Default 100."
                },
                "dep_code": {
                    "type": "string",
                    "description": "Filtre additionnel : code département INSEE (ex '75'). Recommandé si polygon vaste : pré-filtre via index pour gain perf majeur."
                },
                "permit_type": {
                    "type": "string",
                    "enum": [
                        "PC_LOGEMENT",
                        "PC_LOCAUX",
                        "PA",
                        "PD",
                        "DP_LOGEMENT",
                        "DP_LOCAUX"
                    ],
                    "description": "Filtre type de permis."
                },
                "min_an_depot": {
                    "type": "integer",
                    "minimum": 2010,
                    "maximum": 2030,
                    "description": "Année dépôt minimum (inclusif). Sitadel couvre 2014-2026."
                },
                "max_an_depot": {
                    "type": "integer",
                    "minimum": 2010,
                    "maximum": 2030,
                    "description": "Année dépôt maximum (inclusif)."
                },
                "min_score": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Filtre Score MDB v0.3 minimum (0-100). Très utile combiné au polygon ZAC pour ne retourner que les permis à fort potentiel marchand de biens."
                }
            },
            "required": ["polygon"]
        }
    },
    {
        "name": "get_commune_density_stats",
        "description": (
            "Stats macro de densité urbaine pour une commune en un seul "
            "appel : nombre de parcelles cadastrales DGFiP + surface "
            "totale + nombre de bâtiments cadastraux (bâti dur / léger / "
            "autre) + nombre de permits historiques 2014-2026 ventilés par "
            "année, par type (PC_LOGEMENT, PC_LOCAUX, PA, PD, DP) et par "
            "distribution Score MDB v0.3 (low / medium / high / premium) "
            "+ indicateur de densité (bâtiments par km² + tier "
            "tres_dense / dense / moyen / faible). Use cases : SEO "
            "programmatique 35 000 communes France entière, analytics "
            "macro pour marchands de biens / promoteurs / banques en "
            "veille marché, comparaison de zones avant décision foncière. "
            "PLM mapping automatique : pour les arrondissements de Paris "
            "(75101-75120), Lyon (69381-69389), Marseille (13201-13216), "
            "les permits Sitadel stockés sur la ville-mère sont agrégés "
            "automatiquement. Plan Business+ uniquement (BI agrégée, pas "
            "soumise au scope géographique Free/Explorer). Coût quota : "
            "3 unités composite. Sources : cadastre.data.gouv.fr Etalab "
            "+ Sitadel SDES."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "commune_code": {
                    "type": "string",
                    "pattern": "^[0-9A-Z]{5}$",
                    "minLength": 5,
                    "maxLength": 5,
                    "description": (
                        "Code INSEE 5 caractères alphanumériques majuscules "
                        "de la commune. Exemples : '75104' Paris 4e, '33063' "
                        "Bordeaux, '69384' Lyon 4e, '2A004' Ajaccio (Corse). "
                        "Pour les villes-mères PLM utiliser '75056' Paris, "
                        "'69123' Lyon, '13055' Marseille (agrège tous les "
                        "arrondissements côté permits)."
                    )
                }
            },
            "required": ["commune_code"]
        }
    },
    {
        "name": "get_neighbor_parcels",
        "description": (
            "Retourne les parcelles cadastrales voisines d'un permis dans "
            "un rayon configurable (10-2000 m, default 200 m) + leur "
            "historique permits (max 5 par parcelle). Use case killer "
            "Marchand de Biens : pattern d'activite local autour d'un "
            "permis identifie. Permet de detecter zone en mutation "
            "(plusieurs permis recents sur les parcelles voisines), "
            "opportunites adjacentes (parcelles voisines sans activite "
            "recente), densification (parcelles voisines deja baties vs "
            "libres). Methode : centroide du permit source via "
            "permits.geom geocode BAN en priorite, sinon ST_Centroid("
            "cadastre_geom). Recherche PostGIS ST_DWithin sur les 35M "
            "parcelles France avec pre-filtre commune_code pour exploiter "
            "l'index. Distance calculee en geography pour metres exacts. "
            "La parcelle du permit source est exclue des voisins (pas "
            "d'auto-reference). Plan Pro+ uniquement. Cout quota : 1 + "
            "nombre de voisins retournes (composite coherent avec /360 et "
            "/v1/parcelles/{id}). Reponse : permit_commune_code + "
            "permit_center lat/lng + search_radius_m + neighbors_count + "
            "neighbors (liste triee par distance croissante). Chaque "
            "voisin contient : id_parcelle 14 chars + section + numero + "
            "distance_m precise + contenance_m2 DGFiP + centroid lat/lng "
            "+ permits_count + permits[] (max 5 items : num_pa + type + "
            "annee + etat + surface). Use case complet : (1) "
            "search_permits pour trouver un permis cible, (2) "
            "get_neighbor_parcels(num_pa=X, radius_m=200) pour scanner le "
            "voisinage, (3) get_mdb_score sur les num_pa des voisins "
            "interessants. Erreur 422 si le permit source n'est ni "
            "geocode ni cadastre_geom (~11% des permits Sitadel)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_pa": {
                    "type": "string",
                    "description": (
                        "Identifiant Sitadel du permit source. Format : "
                        "13 caracteres alphanumeriques (ex "
                        "'0930662500027' = Seine-Saint-Denis 2025). "
                        "Recupere via search_permits ou get_permit_details. "
                        "Le permit doit avoir des coordonnees (lat/lng "
                        "geocodes BAN OU cadastre_geom) pour calculer le "
                        "centroide de recherche."
                    ),
                },
                "radius_m": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 2000,
                    "default": 200,
                    "description": (
                        "Rayon de recherche en metres autour du centroide "
                        "du permit source (10-2000 m, default 200 m). "
                        "Petit rayon 50-100 m = parcelles directement "
                        "contigues (ilot). Rayon moyen 200-500 m = "
                        "quartier proche. Grand rayon 1000-2000 m = "
                        "quartier elargi voire commune entiere si petite."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                    "description": (
                        "Nombre max de parcelles voisines retournees "
                        "(1-50, default 10). Tri par distance croissante. "
                        "Aller au-dela de 20 voisins est rarement utile : "
                        "la pertinence MDB decroit avec la distance, et le "
                        "cout quota augmente lineairement."
                    ),
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
                    "description": (
                        "Liste des lignes à enrichir (1 à 1000). Chaque "
                        "ligne doit avoir un `ref` unique (echo dans la "
                        "response) et au moins UN des 3 modes "
                        "d'identification : `lat+lng` (coordonnées GPS "
                        "WGS84), `adresse` (texte libre, geocodé via BAN), "
                        "ou `commune+section+numero` (référence cadastrale "
                        "DGFiP). Mixage possible : ligne 1 en lat/lng, "
                        "ligne 2 en adresse, ligne 3 en cadastre."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {
                                "type": "string",
                                "description": (
                                    "Identifiant client unique de la ligne "
                                    "(echo dans la response pour matching). "
                                    "Ex `MAISON-001`, `prospect-42`."
                                ),
                            },
                            "lat": {
                                "type": "number",
                                "description": (
                                    "Latitude WGS84 (mode coordonnées GPS). "
                                    "Range -90 à 90, métropole FR ~41-51. "
                                    "Doit être associé à `lng`."
                                ),
                            },
                            "lng": {
                                "type": "number",
                                "description": (
                                    "Longitude WGS84 (mode coordonnées). "
                                    "Range -180 à 180, métropole FR ~-5 à "
                                    "10. Doit être associé à `lat`."
                                ),
                            },
                            "adresse": {
                                "type": "string",
                                "description": (
                                    "Adresse postale libre (mode adresse). "
                                    "Géocodage via API BAN data.gouv.fr. "
                                    "Ex `12 rue de Rivoli 75001 Paris`. "
                                    "Tolère typos et accents oubliés."
                                ),
                            },
                            "commune": {
                                "type": "string",
                                "description": (
                                    "Code INSEE 5 chars de la commune "
                                    "(mode cadastre, ex `75056` pour "
                                    "Paris). Combiner avec `section` et "
                                    "`numero`."
                                ),
                            },
                            "section": {
                                "type": "string",
                                "description": (
                                    "Section cadastrale DGFiP (mode "
                                    "cadastre, ex `AB`, `EH`, `XV`). "
                                    "1 à 2 lettres typiquement."
                                ),
                            },
                            "numero": {
                                "type": "string",
                                "description": (
                                    "Numéro de parcelle DGFiP (mode "
                                    "cadastre, ex `42`, `1318`). 1 à 4 "
                                    "chiffres typiquement."
                                ),
                            },
                        },
                        "required": ["ref"],
                    },
                },
                "radius_m": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 10000,
                    "default": 500,
                    "description": (
                        "Rayon en mètres autour du point résolu pour "
                        "chercher les permis associés. Défaut 500m "
                        "(typique zone résidentielle). Augmenter à 1000-"
                        "2000m pour zone rurale, descendre à 100-200m "
                        "pour zone urbaine dense."
                    ),
                },
                "max_matches": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 3,
                    "description": (
                        "Nombre max de permits retournés par ligne. "
                        "Défaut 3 (les 3 plus proches). Coût quota = "
                        "len(rows) unités (pas multiplié par max_matches)."
                    ),
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
            "plans (avec respect du scope géo). **Coût quota = nombre de "
            "résultats retournés** (max `limit`, min 1). Anti-exfiltration "
            "depuis 2026-05-16 (cohérent avec search_permits). Exemple : "
            "'rue victor hugo paris', 'cours de l ile bordeaux'."
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
                    "description": "Identifiant Sitadel unique (ex 0930662500027).",
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


async def get_score_explanation(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits/{num_pa}/score/explain : transparence Score MDB v0.3.

    Retourne les 11 signaux pondérés avec interprétation FR
    contextualisée + top 3 drivers + top 3 drags + inputs concrets.

    Use case : audit du score "pourquoi ce permis est noté 87 ?",
    due-diligence transparente. Plan Pro+ uniquement.
    Coût composite 2 unités quota (calcul + interprétations).
    """
    num_pa = _validate_num_pa(arguments.get("num_pa"))
    return await _http_get(
        f"/v1/permits/{num_pa}/score/explain", client=client
    )


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


_ID_PARCELLE_REGEX = re.compile(r"^[A-Z0-9]{14}$")


def _validate_id_parcelle(value: Any) -> str:
    """Valide un id_parcelle MCP : str 14 chars alphanumériques majuscules.

    Accepte les inputs en minuscule (uppercase auto) + espaces (strip auto).
    Raise ValueError sinon (transformee en {"error": "validation"} par
    call_tool).
    """
    if not isinstance(value, str):
        raise ValueError(
            "id_parcelle doit etre une string, "
            f"recu {type(value).__name__}"
        )
    s = value.strip().upper()
    if not _ID_PARCELLE_REGEX.match(s):
        raise ValueError(
            f"id_parcelle invalide : {value!r}. Format attendu : 14 "
            "caractères alphanumériques majuscules, ex '75104000AA0123' "
            "(5 commune + 3 prefix + 2 section + 4 numero)."
        )
    return s


async def get_parcelle_by_id(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/parcelles/{id_parcelle} : lookup direct cadastre DGFiP.

    Retourne le contexte complet d'une parcelle : centroïde + contenance +
    commune + permis historiques associés + polygon GeoJSON si dispo +
    bâtiments existants count si polygon dispo. Plan Pro+ uniquement.
    Coût quota : 1 + 1 par permis retourné (composite).
    """
    id_parcelle = _validate_id_parcelle(arguments.get("id_parcelle"))
    return await _http_get(f"/v1/parcelles/{id_parcelle}", client=client)


_COMMUNE_CODE_REGEX = re.compile(r"^[0-9A-Z]{5}$")


def _validate_commune_code(value: Any) -> str:
    """Valide un commune_code INSEE : str 5 chars alphanumériques majuscules."""
    if not isinstance(value, str):
        raise ValueError(
            "commune_code doit etre une string, "
            f"recu {type(value).__name__}"
        )
    s = value.strip().upper()
    if not _COMMUNE_CODE_REGEX.match(s):
        raise ValueError(
            f"commune_code invalide : {value!r}. Format attendu : 5 "
            "caractères alphanumériques majuscules INSEE, ex '75104' "
            "(Paris 4e) ou '2A004' (Ajaccio)."
        )
    return s


async def get_commune_density_stats(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/stats/commune/{code}/density : stats densité urbaine commune.

    Agrège parcelles + bâtiments + permits historiques + Score MDB
    distribution en un seul appel. Plan Business+ uniquement. Coût quota
    composite 3 unités.
    """
    commune_code = _validate_commune_code(arguments.get("commune_code"))
    return await _http_get(
        f"/v1/stats/commune/{commune_code}/density", client=client
    )


async def search_permits_in_polygon(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """POST /v1/permits/inside-polygon : permits dans un polygone GeoJSON.

    Use case foncier : ZAC, ZA, périmètre opération, zone de chasse
    marchand de biens. Plan Business+ uniquement. Coût quota :
    max(2, len(rows)) composite. Limite surface 1000 km² anti-abus.
    """
    polygon = arguments.get("polygon")
    if not isinstance(polygon, dict):
        raise ValueError(
            "polygon est requis et doit etre un objet GeoJSON "
            "(type='Polygon', coordinates=[[[lng, lat], ...]])"
        )

    body: dict[str, Any] = {"polygon": polygon}
    limit = arguments.get("limit")
    if isinstance(limit, int) and 1 <= limit <= 500:
        body["limit"] = limit
    for key in ("dep_code", "permit_type"):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            body[key] = val.strip()
    for key in ("min_an_depot", "max_an_depot", "min_score"):
        val = arguments.get(key)
        if isinstance(val, int):
            body[key] = val

    return await _http_post("/v1/permits/inside-polygon", json=body, client=client)


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


async def get_neighbor_parcels(
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET /v1/permits/{num_pa}/parcelles-voisines : parcelles
    cadastrales voisines + historique permits (Prio 8 sprint 11).

    Use case killer Marchand de Biens : pattern d'activite local autour
    d'un permis identifie. Plan Pro+ uniquement. Cout composite 1 +
    nombre de voisins retournes.
    """
    num_pa = _validate_num_pa(arguments.get("num_pa"))
    params: dict[str, Any] = {}
    radius_m = arguments.get("radius_m")
    if isinstance(radius_m, int) and 10 <= radius_m <= 2000:
        params["radius_m"] = radius_m
    limit = arguments.get("limit")
    if isinstance(limit, int) and 1 <= limit <= 50:
        params["limit"] = limit
    return await _http_get(
        f"/v1/permits/{num_pa}/parcelles-voisines",
        params=params,
        client=client,
    )


# ----------------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------------


TOOL_HANDLERS = {
    "search_permits": search_permits,
    "get_permit_details": get_permit_details,
    "find_dvf_neighbors": find_dvf_neighbors,
    "get_mdb_score": get_mdb_score,
    "get_score_explanation": get_score_explanation,
    "get_plu_zoning": get_plu_zoning,
    "get_risks": get_risks,
    "get_parcelle_geometry": get_parcelle_geometry,
    "get_existing_buildings": get_existing_buildings,
    "get_parcelle_by_id": get_parcelle_by_id,
    "search_permits_in_polygon": search_permits_in_polygon,
    "get_commune_density_stats": get_commune_density_stats,
    "get_neighbor_parcels": get_neighbor_parcels,
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
