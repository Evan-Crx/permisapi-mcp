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


def test_seven_tools_defined():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {
        "search_permits",
        "get_permit_details",
        "find_dvf_neighbors",
        "get_mdb_score",
        "get_plu_zoning",
        "get_risks",
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
