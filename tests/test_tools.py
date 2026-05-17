"""Tests des tools MCP : valident la logique metier sans dependre du
package `mcp` (le test du protocole stdio est out-of-scope, c'est de
l'integration avec Anthropic SDK).
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

import httpx
import pytest

from permisapi_mcp.tools import (
    TOOL_HANDLERS,
    TOOL_SCHEMAS,
    PermisapiError,
    _validate_num_pa,
    call_tool,
)


@contextmanager
def env(**kwargs):
    """Set env vars for the duration of a test."""
    old: dict[str, str | None] = {k: os.environ.get(k) for k in kwargs}
    for k, v in kwargs.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _mock_transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ----------------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------------


def test_sixteen_tools_defined():
    """v0.5.6 ajoute get_score_explanation (sprint 14 transparence Score MDB)."""
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {
        "search_permits",
        "get_permit_details",
        "find_dvf_neighbors",
        "get_mdb_score",
        "get_score_explanation",
        "get_plu_zoning",
        "get_risks",
        "get_parcelle_geometry",
        "get_existing_buildings",
        "get_parcelle_by_id",
        "search_permits_in_polygon",
        "get_commune_density_stats",
        "get_neighbor_parcels",
        "fuzzy_search_addresses",
        "bulk_enrich_list",
        "get_permit_full_view",
    }


def test_each_schema_has_required_fields():
    for t in TOOL_SCHEMAS:
        assert t["name"]
        assert t["description"]
        assert "inputSchema" in t
        assert t["inputSchema"]["type"] == "object"


def test_schemas_have_handlers():
    for t in TOOL_SCHEMAS:
        assert t["name"] in TOOL_HANDLERS


# ----------------------------------------------------------------------------
# _validate_num_pa
# ----------------------------------------------------------------------------


def test_validate_num_pa_accepts_valid():
    for ok in [
        "PC07404021K1",
        "PC 075 116 22 B0042",
        "DP_075_108_23_B0012",
        "PA-44-2024-0001",
    ]:
        assert _validate_num_pa(ok) == ok


def test_validate_num_pa_rejects_dangerous():
    for bad in [
        "<script>",
        "PC';DROP",
        "PC\nSubject: x",
        "PC@x.com",
        "PC<>",
    ]:
        with pytest.raises(ValueError):
            _validate_num_pa(bad)


def test_validate_num_pa_rejects_non_string():
    for bad in [None, 42, ["PC1"], {}]:
        with pytest.raises(ValueError):
            _validate_num_pa(bad)


def test_validate_num_pa_rejects_too_long():
    with pytest.raises(ValueError):
        _validate_num_pa("A" * 51)


def test_validate_num_pa_rejects_empty():
    with pytest.raises(ValueError):
        _validate_num_pa("")


# ----------------------------------------------------------------------------
# _validate_id_parcelle (sprint 10 cadastre Prio 4)
# ----------------------------------------------------------------------------


def test_validate_id_parcelle_accepts_valid():
    from permisapi_mcp.tools import _validate_id_parcelle

    assert _validate_id_parcelle("75104000AA0123") == "75104000AA0123"
    assert _validate_id_parcelle("33063000AB0123") == "33063000AB0123"


def test_validate_id_parcelle_normalizes_lowercase_and_spaces():
    """Accepte minuscule + espaces, normalise en uppercase strip."""
    from permisapi_mcp.tools import _validate_id_parcelle

    assert _validate_id_parcelle("  75104000aa0123 ") == "75104000AA0123"


def test_validate_id_parcelle_rejects_wrong_length():
    from permisapi_mcp.tools import _validate_id_parcelle

    with pytest.raises(ValueError):
        _validate_id_parcelle("75104000AA012")  # 13 chars
    with pytest.raises(ValueError):
        _validate_id_parcelle("75104000AA01234")  # 15 chars


def test_validate_id_parcelle_rejects_non_alphanumeric():
    from permisapi_mcp.tools import _validate_id_parcelle

    with pytest.raises(ValueError):
        _validate_id_parcelle("75104-00AA0123")
    with pytest.raises(ValueError):
        _validate_id_parcelle("75104.000AA012")


def test_validate_id_parcelle_rejects_non_string():
    from permisapi_mcp.tools import _validate_id_parcelle

    for bad in [None, 42, ["75104000AA0123"], {}]:
        with pytest.raises(ValueError):
            _validate_id_parcelle(bad)


@pytest.mark.asyncio
async def test_get_parcelle_by_id_calls_correct_url():
    """Dispatch via call_tool fait un GET /v1/parcelles/{id} avec auth."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id_parcelle": "75104000AA0123",
                "commune_code": "75104",
                "commune_name": "Paris 4e Arrondissement",
                "dep_code": "75",
                "contenance_m2": 1247,
                "centroid": {"lat": 48.8566, "lng": 2.3522},
                "section_cadastre": "AA",
                "numero_cadastre": "0123",
                "prefix_cadastre": "000",
                "permits_count": 0,
                "permits": [],
                "polygon_geojson": None,
                "nb_batiments_existants": None,
                "parcelle_nue": None,
                "updated": "2026-04-15",
            },
        )

    with env(PERMISAPI_KEY="pk_test_dummy"):
        async with _mock_transport(handler) as client:
            result = await call_tool(
                "get_parcelle_by_id",
                {"id_parcelle": "75104000AA0123"},
                client=client,
            )

    assert len(captured) == 1
    assert captured[0].url.path == "/v1/parcelles/75104000AA0123"
    assert captured[0].headers["X-API-Key"] == "pk_test_dummy"

    parsed = json.loads(result)
    assert parsed["id_parcelle"] == "75104000AA0123"
    assert parsed["commune_name"] == "Paris 4e Arrondissement"


@pytest.mark.asyncio
async def test_get_parcelle_by_id_validation_error_in_result():
    """Format invalide -> call_tool retourne {error: validation}."""
    with env(PERMISAPI_KEY="pk_test_dummy"):
        result = await call_tool(
            "get_parcelle_by_id", {"id_parcelle": "invalid"}
        )
    parsed = json.loads(result)
    assert parsed["error"] == "validation"
    assert "id_parcelle" in parsed["detail"]


# ----------------------------------------------------------------------------
# search_permits_in_polygon (sprint 10 cadastre Prio 5)
# ----------------------------------------------------------------------------


VALID_PARIS_POLYGON_FOR_TOOL = {
    "type": "Polygon",
    "coordinates": [
        [
            [2.34, 48.85],
            [2.36, 48.85],
            [2.36, 48.87],
            [2.34, 48.87],
            [2.34, 48.85],
        ]
    ],
}


@pytest.mark.asyncio
async def test_search_permits_in_polygon_calls_post_with_body():
    """Dispatch via call_tool fait un POST /v1/permits/inside-polygon."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "items": [],
                "count": 0,
                "polygon_surface_km2": 0.04,
                "polygon_center": {"lat": 48.86, "lng": 2.35},
                "filters_applied": {"limit": 100},
            },
        )

    with env(PERMISAPI_KEY="pk_test_dummy"):
        async with _mock_transport(handler) as client:
            result = await call_tool(
                "search_permits_in_polygon",
                {
                    "polygon": VALID_PARIS_POLYGON_FOR_TOOL,
                    "limit": 50,
                    "dep_code": "75",
                    "min_score": 70,
                },
                client=client,
            )

    assert len(captured) == 1
    assert captured[0].method == "POST"
    assert captured[0].url.path == "/v1/permits/inside-polygon"
    assert captured[0].headers["X-API-Key"] == "pk_test_dummy"
    # Body inclut polygon + filters
    body = json.loads(captured[0].content)
    assert body["polygon"]["type"] == "Polygon"
    assert body["limit"] == 50
    assert body["dep_code"] == "75"
    assert body["min_score"] == 70

    parsed = json.loads(result)
    assert parsed["count"] == 0
    assert parsed["polygon_surface_km2"] == 0.04


@pytest.mark.asyncio
async def test_search_permits_in_polygon_validation_missing_polygon():
    """Sans polygon -> call_tool retourne {error: validation}."""
    with env(PERMISAPI_KEY="pk_test_dummy"):
        result = await call_tool("search_permits_in_polygon", {})
    parsed = json.loads(result)
    assert parsed["error"] == "validation"
    assert "polygon" in parsed["detail"].lower()


@pytest.mark.asyncio
async def test_search_permits_in_polygon_validation_polygon_not_dict():
    """Polygon en string raw -> {error: validation}."""
    with env(PERMISAPI_KEY="pk_test_dummy"):
        result = await call_tool(
            "search_permits_in_polygon", {"polygon": "not a dict"}
        )
    parsed = json.loads(result)
    assert parsed["error"] == "validation"


# ----------------------------------------------------------------------------
# get_commune_density_stats (sprint 10 cadastre Prio 7)
# ----------------------------------------------------------------------------


def test_validate_commune_code_accepts_valid():
    from permisapi_mcp.tools import _validate_commune_code

    assert _validate_commune_code("75104") == "75104"
    assert _validate_commune_code("33063") == "33063"
    assert _validate_commune_code("2A004") == "2A004"


def test_validate_commune_code_normalizes_lowercase():
    from permisapi_mcp.tools import _validate_commune_code

    assert _validate_commune_code("2a004") == "2A004"
    assert _validate_commune_code("  75104  ") == "75104"


def test_validate_commune_code_rejects_wrong_length():
    from permisapi_mcp.tools import _validate_commune_code

    with pytest.raises(ValueError):
        _validate_commune_code("7510")  # 4 chars
    with pytest.raises(ValueError):
        _validate_commune_code("751041")  # 6 chars


def test_validate_commune_code_rejects_non_alphanumeric():
    from permisapi_mcp.tools import _validate_commune_code

    with pytest.raises(ValueError):
        _validate_commune_code("75-10")
    with pytest.raises(ValueError):
        _validate_commune_code("751 4")


@pytest.mark.asyncio
async def test_get_commune_density_stats_calls_correct_url():
    """Dispatch via call_tool fait GET /v1/stats/commune/{code}/density."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "commune_code": "75104",
                "commune_name": "Paris 4e Arrondissement",
                "dep_code": "75",
                "parcelles": {
                    "count": 1247,
                    "surface_total_m2": 1457000,
                    "surface_avg_m2": 1169,
                },
                "batiments": {
                    "count": 3892,
                    "count_dur": 3204,
                    "count_leger": 488,
                    "count_autres": 200,
                },
                "permits": {
                    "count_total": 587,
                    "count_by_year": {"2024": 42, "2025": 38},
                    "count_by_type": {"PC_LOGEMENT": 120},
                    "score_mdb_distribution": {
                        "low": 0,
                        "medium": 0,
                        "high": 1,
                        "premium": 0,
                    },
                },
                "density": {
                    "batiments_per_km2": 2672.3,
                    "tier": "dense",
                },
                "plm_mapping": {
                    "arrondissement": "75104",
                    "ville_mere": "75056",
                },
                "computed_at": "2026-05-16T19:30:00+00:00",
            },
        )

    with env(PERMISAPI_KEY="pk_test_dummy"):
        async with _mock_transport(handler) as client:
            result = await call_tool(
                "get_commune_density_stats",
                {"commune_code": "75104"},
                client=client,
            )

    assert len(captured) == 1
    assert captured[0].url.path == "/v1/stats/commune/75104/density"
    parsed = json.loads(result)
    assert parsed["commune_name"] == "Paris 4e Arrondissement"
    assert parsed["density"]["tier"] == "dense"


@pytest.mark.asyncio
async def test_get_commune_density_stats_validation_error():
    """Code invalide -> {error: validation}."""
    with env(PERMISAPI_KEY="pk_test_dummy"):
        result = await call_tool(
            "get_commune_density_stats", {"commune_code": "invalid"}
        )
    parsed = json.loads(result)
    assert parsed["error"] == "validation"
    assert "commune_code" in parsed["detail"]


# ----------------------------------------------------------------------------
# call_tool : auth requise
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_requires_api_key():
    with env(PERMISAPI_KEY=None):
        result = await call_tool("get_permit_details", {"num_pa": "PC1"})
    parsed = json.loads(result)
    assert parsed["error"] == "config"
    assert "PERMISAPI_KEY" in parsed["detail"]


@pytest.mark.asyncio
async def test_call_tool_unknown_tool_returns_error():
    with env(PERMISAPI_KEY="pk_test_x"):
        result = await call_tool("unknown_tool", {})
    parsed = json.loads(result)
    assert "error" in parsed
    assert "unknown_tool" in parsed["error"]


# ----------------------------------------------------------------------------
# call_tool : forwards to PermisAPI correctly
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_permits_calls_correct_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": [], "pagination": {}})

    with env(PERMISAPI_KEY="pk_test_xyz", PERMISAPI_BASE_URL="https://api.test"):
        async with _mock_transport(handler) as client:
            from permisapi_mcp.tools import search_permits

            await search_permits(
                {"dep_code": "75", "limit": 5}, client=client
            )
    assert "/v1/permits" in captured["url"]
    assert "dep_code=75" in captured["url"]
    assert "limit=5" in captured["url"]
    assert captured["headers"]["x-api-key"] == "pk_test_xyz"


@pytest.mark.asyncio
async def test_search_permits_forwards_sort():
    """`sort` argument propagates to the /v1/permits query string."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"data": [], "pagination": {}})

    with env(PERMISAPI_KEY="pk_test_xyz", PERMISAPI_BASE_URL="https://api.test"):
        async with _mock_transport(handler) as client:
            from permisapi_mcp.tools import search_permits

            await search_permits(
                {"dep_code": "33", "sort": "-superficie_terrain", "limit": 10},
                client=client,
            )
    assert "sort=-superficie_terrain" in captured["url"]
    assert "dep_code=33" in captured["url"]


@pytest.mark.asyncio
async def test_get_permit_details_calls_correct_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"num_pa": "PC1"})

    with env(PERMISAPI_KEY="pk_test_xyz", PERMISAPI_BASE_URL="https://api.test"):
        async with _mock_transport(handler) as client:
            from permisapi_mcp.tools import get_permit_details

            await get_permit_details({"num_pa": "PC07404021K1"}, client=client)
    assert "/v1/permits/PC07404021K1" in captured["url"]


@pytest.mark.asyncio
async def test_find_dvf_neighbors_calls_correct_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"matches": []})

    with env(PERMISAPI_KEY="pk_test_xyz", PERMISAPI_BASE_URL="https://api.test"):
        async with _mock_transport(handler) as client:
            from permisapi_mcp.tools import find_dvf_neighbors

            await find_dvf_neighbors(
                {"num_pa": "PC1", "limit": 5, "min_year": 2023},
                client=client,
            )
    assert "/v1/permits/PC1/dvf" in captured["url"]
    assert "limit=5" in captured["url"]
    assert "min_year=2023" in captured["url"]


@pytest.mark.asyncio
async def test_get_mdb_score_calls_correct_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"score": 78})

    with env(PERMISAPI_KEY="pk_test_xyz", PERMISAPI_BASE_URL="https://api.test"):
        async with _mock_transport(handler) as client:
            from permisapi_mcp.tools import get_mdb_score

            await get_mdb_score({"num_pa": "PC1"}, client=client)
    assert "/v1/permits/PC1/score" in captured["url"]


@pytest.mark.asyncio
async def test_get_plu_zoning_calls_correct_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"has_plu": True})

    with env(PERMISAPI_KEY="pk_test_xyz", PERMISAPI_BASE_URL="https://api.test"):
        async with _mock_transport(handler) as client:
            from permisapi_mcp.tools import get_plu_zoning

            await get_plu_zoning({"num_pa": "PC1"}, client=client)
    assert "/v1/permits/PC1/plu" in captured["url"]


@pytest.mark.asyncio
async def test_get_risks_calls_correct_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"risk_score": 25})

    with env(PERMISAPI_KEY="pk_test_xyz", PERMISAPI_BASE_URL="https://api.test"):
        async with _mock_transport(handler) as client:
            from permisapi_mcp.tools import get_risks

            await get_risks({"num_pa": "PC1"}, client=client)
    assert "/v1/permits/PC1/risks" in captured["url"]


@pytest.mark.asyncio
async def test_get_permit_full_view_calls_correct_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "num_pa": "PC1",
                "detail": {"num_pa": "PC1"},
                "sirene": None,
                "dvf": None,
                "score": None,
                "plu": None,
                "risks": None,
                "fetch_errors": [],
                "subfetches_billed": 6,
            },
        )

    with env(PERMISAPI_KEY="pk_test_xyz", PERMISAPI_BASE_URL="https://api.test"):
        async with _mock_transport(handler) as client:
            from permisapi_mcp.tools import get_permit_full_view

            result = await get_permit_full_view({"num_pa": "PC1"}, client=client)
    assert "/v1/permits/PC1/360" in captured["url"]
    assert result["subfetches_billed"] == 6


@pytest.mark.asyncio
async def test_get_permit_full_view_validates_num_pa():
    """Defense en profondeur : meme regex de num_pa que les autres tools."""
    with env(PERMISAPI_KEY="pk_test_xyz"):
        result = await call_tool(
            "get_permit_full_view", {"num_pa": "<script>alert(1)</script>"}
        )
    parsed = json.loads(result)
    assert parsed["error"] == "validation"


# ----------------------------------------------------------------------------
# Error handling
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permisapi_4xx_raises_permisapierror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "Plan Pro requis"})

    with env(PERMISAPI_KEY="pk_test_xyz", PERMISAPI_BASE_URL="https://api.test"):
        async with _mock_transport(handler) as client:
            from permisapi_mcp.tools import get_mdb_score

            with pytest.raises(PermisapiError) as exc_info:
                await get_mdb_score({"num_pa": "PC1"}, client=client)
            assert exc_info.value.status_code == 402


@pytest.mark.asyncio
async def test_call_tool_handles_402_and_returns_error_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "Plan Pro requis"})

    # call_tool ne prend pas client, on monkeypatch via env
    with env(PERMISAPI_KEY="pk_test_xyz", PERMISAPI_BASE_URL="https://api.test"):
        # Le call_tool va creer son propre client httpx, donc on ne peut pas
        # facilement injecter le mock. On test plutot via le handler.
        # Pour ce test, on test le format d'erreur via une exception simulee
        from permisapi_mcp.tools import call_tool as ct

        # Force une PermisapiError en patchant le handler avec un client
        # qui rejette
        result = await ct(
            "get_permit_details",
            {"num_pa": "PC1"},
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    parsed = json.loads(result)
    assert parsed["error"] == "permisapi_error"
    assert parsed["status_code"] == 402


@pytest.mark.asyncio
async def test_call_tool_handles_validation_error():
    with env(PERMISAPI_KEY="pk_test_xyz"):
        result = await call_tool(
            "get_permit_details", {"num_pa": "<script>alert(1)</script>"}
        )
    parsed = json.loads(result)
    assert parsed["error"] == "validation"


@pytest.mark.asyncio
async def test_call_tool_returns_json_text_on_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"num_pa": "PC1", "etat_pa": 1})

    with env(PERMISAPI_KEY="pk_test_xyz", PERMISAPI_BASE_URL="https://api.test"):
        result = await call_tool(
            "get_permit_details",
            {"num_pa": "PC1"},
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    parsed = json.loads(result)
    assert parsed["num_pa"] == "PC1"
    assert parsed["etat_pa"] == 1


# ----------------------------------------------------------------------------
# use_api_key context manager (mode SSE hosted)
# ----------------------------------------------------------------------------


def test_use_api_key_overrides_env():
    """ContextVar prend priorité sur env var."""
    from permisapi_mcp.tools import _api_key, use_api_key

    with env(PERMISAPI_KEY="pk_from_env"):
        assert _api_key() == "pk_from_env"
        with use_api_key("pk_from_context"):
            assert _api_key() == "pk_from_context"
        # Après le with, on retombe sur l'env.
        assert _api_key() == "pk_from_env"


def test_use_api_key_works_without_env():
    """ContextVar fonctionne même si PERMISAPI_KEY n'est pas en env."""
    from permisapi_mcp.tools import _api_key, use_api_key

    with env(PERMISAPI_KEY=None):
        # Sans clé : raise.
        with pytest.raises(RuntimeError, match="PERMISAPI_KEY"):
            _api_key()
        # Avec context : OK.
        with use_api_key("pk_request_scoped"):
            assert _api_key() == "pk_request_scoped"
        # Après : redevient unconfigured.
        with pytest.raises(RuntimeError):
            _api_key()


def test_use_api_key_nested_scopes_restore_lifo():
    """Imbrications de with : la valeur extérieure est restaurée à la sortie de l'intérieure."""
    from permisapi_mcp.tools import _api_key, use_api_key

    with env(PERMISAPI_KEY=None):
        with use_api_key("pk_outer"):
            assert _api_key() == "pk_outer"
            with use_api_key("pk_inner"):
                assert _api_key() == "pk_inner"
            assert _api_key() == "pk_outer"


@pytest.mark.asyncio
async def test_use_api_key_propagates_to_http_header():
    """Le header X-API-Key envoyé à PermisAPI doit refléter la clé du ContextVar."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"num_pa": "PC1"})

    from permisapi_mcp.tools import get_permit_details, use_api_key

    with env(PERMISAPI_KEY=None, PERMISAPI_BASE_URL="https://api.test"):
        async with _mock_transport(handler) as client:
            with use_api_key("pk_scoped_to_request"):
                await get_permit_details({"num_pa": "PC1"}, client=client)

    assert captured["headers"]["x-api-key"] == "pk_scoped_to_request"


@pytest.mark.asyncio
async def test_concurrent_use_api_key_isolated():
    """Deux coroutines avec des clés différentes ne se polluent pas (ContextVar async-safe)."""
    import asyncio

    captured_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_keys.append(request.headers["x-api-key"])
        return httpx.Response(200, json={"num_pa": request.url.path.split("/")[-1]})

    from permisapi_mcp.tools import get_permit_details, use_api_key

    async def call_with(key: str, num_pa: str, client: httpx.AsyncClient):
        with use_api_key(key):
            await get_permit_details({"num_pa": num_pa}, client=client)

    with env(PERMISAPI_KEY=None, PERMISAPI_BASE_URL="https://api.test"):
        async with _mock_transport(handler) as client:
            await asyncio.gather(
                call_with("pk_alice", "PC1", client),
                call_with("pk_bob", "PC2", client),
                call_with("pk_carol", "PC3", client),
            )

    assert sorted(captured_keys) == ["pk_alice", "pk_bob", "pk_carol"]
