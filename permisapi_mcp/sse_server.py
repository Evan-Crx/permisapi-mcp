"""Serveur MCP hosted (Streamable HTTP + SSE legacy + OAuth 2.0).

Expose les 11 outils PermisAPI en HTTP au lieu de stdio, avec 3 modes d'auth :

  1. **OAuth 2.0** (Claude.ai web hosted MCP, ChatGPT desktop, futurs clients) :
     flow standard avec DCR + PKCE. Le user colle sa clé PermisAPI sur la
     page consent custom `/consent`, on génère un access_token opaque qui
     wrap la clé en mémoire.
  2. **Bearer header direct** (Cursor, Windsurf, MCP Inspector, ChatGPT
     custom GPT) : `Authorization: Bearer pk_live_...` directement, on
     bypass le flow OAuth.
  3. **Query param** (`?key=pk_live_...`) : fallback pour clients qui ne
     gèrent ni Bearer custom ni OAuth.

Endpoints :
  - GET  /health                                : probe Railway (no auth)
  - GET  /.well-known/oauth-authorization-server : metadata OAuth (no auth)
  - POST /register                              : DCR RFC 7591 (no auth)
  - GET  /authorize                             : entry flow OAuth (no auth)
  - GET  /consent?consent_id=...                : page HTML form consent (no auth)
  - POST /consent/submit                        : soumission form consent (no auth)
  - POST /token                                 : exchange code/refresh (client auth)
  - POST /revoke                                : revoke un token (client auth)
  - POST /mcp/                                  : MCP Streamable HTTP (requires auth)
  - GET  /sse                                   : MCP SSE legacy (requires auth)
  - POST /messages/{id}                         : MCP SSE legacy messages (requires auth)
"""
from __future__ import annotations

import contextlib
import html
import logging
import os
from typing import AsyncIterator

from mcp.server.auth.provider import construct_redirect_uri
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from permisapi_mcp.oauth_provider import PermisApiOAuthProvider
from permisapi_mcp.tools import TOOL_SCHEMAS, call_tool, use_api_key

logger = logging.getLogger("permisapi-mcp-sse")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-5s [permisapi-mcp-sse] %(message)s",
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _mcp_base_url() -> str:
    """URL publique du serveur MCP, utilisée pour les redirects OAuth.

    En prod : `https://mcp.permisapi.fr`. En local : `http://localhost:8001`.
    """
    return os.environ.get("MCP_BASE_URL", "https://mcp.permisapi.fr").rstrip("/")


def _api_backend_url() -> str:
    """URL de l'API PermisAPI pour valider les clés via /v1/me."""
    return os.environ.get("PERMISAPI_BASE_URL", "https://api.permisapi.fr").rstrip("/")


# ---------------------------------------------------------------------------
# MCP server (low-level, identique au mode stdio)
# ---------------------------------------------------------------------------


def build_mcp_server() -> Server:
    app: Server = Server(
        "permisapi",
        instructions=(
            "Acces aux donnees publiques de permis de construire de France. "
            "1,2 M permis Sitadel 2014-2026 + 4,9 M transactions DVF + cadastre "
            "DGFiP + zonage PLU + risques BRGM. 11 outils pour rechercher, "
            "detailler, scorer et croiser. Le plan Free expose 1 departement "
            "au choix (defaut Paris 75) ; Pro+ couvre la France entiere.\n\n"
            "REGLES IMPORTANTES :\n"
            "- N'INVENTE JAMAIS de donnees. Si tu atteins la limite d'une "
            "  pagination, ou si le quota Free de l'user est consomme, ou si "
            "  un endpoint retourne 402/403, dis-le explicitement a l'user "
            "  plutot que d'inferer des permits / scores / adresses non "
            "  retournes par l'API. Tout num_pa que tu n'as pas obtenu par "
            "  l'API est faux.\n"
            "- COUT QUOTA : `search_permits` et `fuzzy_search_addresses` "
            "  decomptent N unites quota = N permits retournes (pas 1 par "
            "  appel). `get_permit_full_view` coute 6 unites. "
            "  `bulk_enrich_list` coute 1 unite par ligne soumise. Les "
            "  autres outils coutent 1 unite par appel.\n"
            "- Si l'user demande un export massif (>50 lignes), explique-lui "
            "  d'abord le cout quota et propose une approche progressive "
            "  (ex 10 permits + analyse, puis 10 suivants), sauf s'il est "
            "  Business ou Enterprise (quotas larges).\n"
            "- Si l'user demande un format CSV ou export structure, retourne "
            "  uniquement les donnees recuperees reellement par tools, jamais "
            "  un padding par incrementation de num_pa ou autre hallucination."
        ),
    )

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

    return app


# ---------------------------------------------------------------------------
# Auth middleware : OAuth tokens + clés directes + ?key= fallback
# ---------------------------------------------------------------------------


# Paths qui bypass l'auth (servis par les handlers OAuth du SDK ou notre
# page consent, ou le probe Railway).
AUTH_BYPASS_PATHS = (
    "/health",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/authorize",
    "/token",
    "/register",
    "/revoke",
    "/consent",  # GET HTML form + POST submit
)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Middleware d'auth pour les endpoints MCP (/mcp, /sse, /messages/).

    Bypass les paths OAuth et /health. Pour le reste, vérifie le token via
    `provider.load_access_token()` qui gère :
      - OAuth tokens (lookup en mémoire, expirations)
      - Clés API directes `pk_live_*` / `pk_test_*` (mode Cursor/Windsurf/etc.)

    Fallback `?key=` query param pour les clients sans Bearer.

    Si auth valide : injecte la clé dans le ContextVar `_current_api_key`
    de `tools.py` via `use_api_key()`, pour que les handlers MCP la
    forwardent au backend api.permisapi.fr.
    """

    def __init__(self, app, provider: PermisApiOAuthProvider) -> None:
        super().__init__(app)
        self.provider = provider

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(bypass) for bypass in AUTH_BYPASS_PATHS):
            return await call_next(request)

        # Extract token : Bearer header en priorité, sinon ?key= query
        token: str | None = None
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            raw = auth_header.split(" ", 1)[1].strip()
            if raw:
                token = raw

        if not token:
            qk = (request.query_params.get("key") or "").strip()
            if qk:
                token = qk

        if not token:
            return JSONResponse(
                {
                    "error": "missing_credentials",
                    "detail": (
                        "Cle PermisAPI requise. 3 modes acceptes : "
                        "(1) OAuth 2.0 (recommande pour Claude.ai web : ajoute "
                        f"`{_mcp_base_url()}/mcp/` comme connecteur, tu seras "
                        "redirige vers une page pour entrer ta cle). "
                        "(2) Header `Authorization: Bearer pk_live_...` (Cursor, "
                        "Windsurf, MCP Inspector, ChatGPT custom GPT). "
                        "(3) Query param `?key=pk_live_...` dans l'URL "
                        "(fallback). Obtiens une cle gratuite sur "
                        "https://permisapi.fr/#pricing"
                    ),
                },
                status_code=401,
            )

        access = await self.provider.load_access_token(token)
        if access is None:
            return JSONResponse(
                {
                    "error": "invalid_token",
                    "detail": (
                        "Token expire ou invalide. Si tu utilises Claude.ai web "
                        "OAuth, la session a peut-etre expire : retire et reajoute "
                        "le connecteur. Si tu utilises une cle API directe, "
                        "verifie qu'elle est valide sur https://permisapi.fr/dashboard"
                    ),
                },
                status_code=401,
            )

        with use_api_key(access.api_key):
            return await call_next(request)


# ---------------------------------------------------------------------------
# Page consent custom (HTML form + submit handler)
# ---------------------------------------------------------------------------


_CONSENT_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Autoriser l'accès à PermisAPI MCP</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
         max-width: 520px; margin: 60px auto; padding: 0 20px; color: #1a1a1a;
         line-height: 1.5; }}
  h1 {{ font-size: 1.5em; margin-bottom: 8px; }}
  .subtitle {{ color: #6b7280; margin-top: 0; }}
  .client-info {{ background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px;
                 padding: 16px; margin: 24px 0; font-size: 0.95em; }}
  label {{ display: block; font-weight: 500; margin-top: 16px; }}
  input[type=text], input[type=password] {{
    width: 100%; padding: 12px; font-family: ui-monospace, "SF Mono", Consolas, monospace;
    font-size: 0.9em; border: 1px solid #d1d5db; border-radius: 6px; box-sizing: border-box;
    margin-top: 8px;
  }}
  button {{ background: #1a1a1a; color: white; padding: 12px 24px; border: none;
           border-radius: 6px; font-size: 1em; cursor: pointer; margin-top: 16px;
           width: 100%; }}
  button:hover {{ background: #333; }}
  .error {{ background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c;
           border-radius: 6px; padding: 12px; margin: 16px 0; font-size: 0.95em; }}
  .help {{ color: #6b7280; font-size: 0.85em; margin-top: 8px; }}
  .footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #e5e7eb;
            color: #6b7280; font-size: 0.85em; }}
  a {{ color: #1a1a1a; }}
</style>
</head>
<body>
<h1>Autoriser l'accès à PermisAPI MCP</h1>
<p class="subtitle">Connecte ton client MCP à 1,2 M permis de construire de France.</p>

<div class="client-info">
<strong>{client_label}</strong> demande à se connecter à PermisAPI pour interroger
les données de permis, DVF, cadastre, PLU et risques en ton nom.
</div>

{error_html}

<form method="POST" action="/consent/submit">
<input type="hidden" name="consent_id" value="{consent_id}">
<label for="api_key">Ta clé PermisAPI</label>
<input type="password" id="api_key" name="api_key" placeholder="pk_live_..." required autofocus autocomplete="off">
<p class="help">Pas encore de clé ? <a href="https://permisapi.fr/#pricing" target="_blank">Inscris-toi gratuitement</a> (500 requêtes/mois sans carte).</p>
<button type="submit">Autoriser {client_label}</button>
</form>

<div class="footer">
Ta clé reste sur PermisAPI, elle n'est jamais transmise au client OAuth. Flow conforme OAuth 2.0 + PKCE.
Tu peux révoquer l'accès à tout moment en supprimant le connecteur côté client.
</div>
</body>
</html>
"""


def _client_label(provider: PermisApiOAuthProvider, consent_id: str) -> str:
    """Friendly label pour le client OAuth (ex 'Claude.ai')."""
    pending = provider._pending_consents.get(consent_id)
    if pending is None:
        return "Le client MCP"
    name = (pending.client.client_name or "Le client MCP").strip()
    return html.escape(name)


def make_consent_get_handler(provider: PermisApiOAuthProvider):
    async def handler(request: Request) -> Response:
        consent_id = request.query_params.get("consent_id", "").strip()
        if not consent_id:
            return HTMLResponse(
                "<h1>Erreur</h1><p>Paramètre consent_id manquant.</p>",
                status_code=400,
            )
        pending = await provider.get_pending_consent(consent_id)
        if pending is None:
            return HTMLResponse(
                "<h1>Lien expiré</h1><p>Le lien d'autorisation a expiré (5 minutes max). "
                "Recommence depuis ton client MCP pour générer un nouveau lien.</p>",
                status_code=410,
            )
        html_body = _CONSENT_HTML_TEMPLATE.format(
            consent_id=html.escape(consent_id),
            client_label=_client_label(provider, consent_id),
            error_html="",
        )
        return HTMLResponse(html_body)
    return handler


def make_consent_submit_handler(provider: PermisApiOAuthProvider):
    async def handler(request: Request) -> Response:
        form = await request.form()
        consent_id = (form.get("consent_id") or "").strip()
        api_key = (form.get("api_key") or "").strip()

        if not consent_id or not api_key:
            return HTMLResponse(
                "<h1>Erreur</h1><p>Champs manquants. <a href=\"javascript:history.back()\">Retour</a></p>",
                status_code=400,
            )

        # Vérification du consent (encore valide ?)
        pending = await provider.get_pending_consent(consent_id)
        if pending is None:
            return HTMLResponse(
                "<h1>Lien expiré</h1><p>Recommence depuis ton client MCP.</p>",
                status_code=410,
            )

        # Validation de la clé contre /v1/me
        me = await provider.validate_api_key(api_key)
        if me is None:
            error_html = (
                '<div class="error">Clé invalide ou révoquée. Vérifie sur '
                '<a href="https://permisapi.fr/dashboard" target="_blank">'
                'permisapi.fr/dashboard</a>.</div>'
            )
            html_body = _CONSENT_HTML_TEMPLATE.format(
                consent_id=html.escape(consent_id),
                client_label=_client_label(provider, consent_id),
                error_html=error_html,
            )
            return HTMLResponse(html_body, status_code=401)

        # Génère le code et redirige vers le client OAuth
        try:
            auth_code, state, redirect_uri = await provider.complete_consent(
                consent_id, api_key
            )
        except ValueError as exc:
            return HTMLResponse(
                f"<h1>Erreur</h1><p>{html.escape(str(exc))}</p>",
                status_code=400,
            )

        redirect_url = construct_redirect_uri(
            str(redirect_uri), code=auth_code, state=state
        )
        logger.info(
            "consent valide pour client=%s email=%s plan=%s -> redirect callback",
            pending.client.client_id,
            me.get("email", "?"),
            me.get("plan", "?"),
        )
        return RedirectResponse(redirect_url, status_code=302)
    return handler


# ---------------------------------------------------------------------------
# /health endpoint (bypass auth)
# ---------------------------------------------------------------------------


async def health_endpoint(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "permisapi-mcp-sse",
            "tools_count": len(TOOL_SCHEMAS),
            "auth_modes": ["oauth2", "bearer_direct", "query_key"],
        }
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> Starlette:
    mcp_server = build_mcp_server()
    provider = PermisApiOAuthProvider(
        mcp_base_url=_mcp_base_url(),
        api_backend_url=_api_backend_url(),
    )

    # --- Streamable HTTP (spec actuelle MCP) ---
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=False,
        stateless=True,
    )

    async def handle_streamable_http(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    # Wrapper Starlette pour servir l'ASGI handler depuis une `Route`
    # (au lieu d'un `Mount`). Permet de matcher /mcp ET /mcp/ exactement
    # sans declencher le 307 redirect automatique que Mount() genere quand
    # le path n'a pas le slash final.
    #
    # Avant ce fix : POST /mcp -> 307 Location: /mcp/ -> client retape la
    # requete -> 200. Soit 2 RTT = 100 a 400ms de latence en plus par tool
    # call. Apres : POST /mcp marche directement (1 RTT). Gain mesurable
    # sur les sessions Claude.ai qui font 10 a 20 tool calls.
    class _NoOpResponse:
        """Response wrapper qui ne fait rien quand Starlette tente de
        l'envoyer. session_manager.handle_request a deja envoye la vraie
        response via `_send` directement, on a juste besoin de satisfaire
        le contrat de Starlette `request_response` (qui appelle response()
        apres le return du endpoint)."""

        async def __call__(self, scope, receive, send):
            return  # noop : la response a deja ete envoyee par session_manager

    async def streamable_http_endpoint(request: Request) -> Response:
        await session_manager.handle_request(
            request.scope, request.receive, request._send
        )
        return _NoOpResponse()  # type: ignore[return-value]

    # --- SSE legacy (backward compat) ---
    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> Response:
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )
        return Response()

    # --- Routes OAuth du SDK MCP ---
    base = _mcp_base_url()
    oauth_routes = create_auth_routes(
        provider=provider,
        issuer_url=AnyHttpUrl(base),
        service_documentation_url=AnyHttpUrl("https://permisapi.fr/mcp"),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            client_secret_expiry_seconds=None,
            valid_scopes=None,
            default_scopes=None,
        ),
        revocation_options=RevocationOptions(enabled=True),
    )

    # --- Routes custom (consent + health + transports MCP) ---
    custom_routes = [
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/consent", make_consent_get_handler(provider), methods=["GET"]),
        Route(
            "/consent/submit",
            make_consent_submit_handler(provider),
            methods=["POST"],
        ),
        Route("/sse", handle_sse, methods=["GET"]),
        Mount("/messages/", app=sse_transport.handle_post_message),
        # 2 routes explicites pour /mcp et /mcp/ : evite le 307 redirect
        # automatique de Mount() qui couterait 1 RTT (50-200ms) par tool call.
        Route(
            "/mcp",
            streamable_http_endpoint,
            methods=["GET", "POST", "DELETE"],
        ),
        Route(
            "/mcp/",
            streamable_http_endpoint,
            methods=["GET", "POST", "DELETE"],
        ),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            logger.info(
                "permisapi-mcp-sse pret. Transports : OAuth 2.0 + Streamable HTTP "
                "(/mcp) + SSE legacy (/sse + /messages/). 11 outils exposes. "
                "Base URL : %s",
                base,
            )
            yield
            logger.info("permisapi-mcp-sse arrete proprement.")

    return Starlette(
        debug=False,
        routes=oauth_routes + custom_routes,
        middleware=[Middleware(BearerAuthMiddleware, provider=provider)],
        lifespan=lifespan,
    )


# Module-level app pour uvicorn factory pattern
app = create_app()


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8001"))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(
        "permisapi_mcp.sse_server:app",
        host=host,
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
