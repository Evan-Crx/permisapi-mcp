"""Provider OAuth 2.0 in-memory pour PermisAPI MCP hosted.

Implémente le Protocol `OAuthAuthorizationServerProvider` du SDK MCP pour
débloquer Claude.ai web hosted MCP (qui ne supporte pas Bearer custom en
V1 BETA, seulement OAuth).

Architecture :
  - Storage in-memory (4 dicts) avec TTL : clients, pending consents,
    auth codes, access tokens, refresh tokens. V1 numReplicas=1 sur Railway,
    donc state survit dans le process. V2 = Redis pour multi-replicas.
  - `authorize()` ne fait PAS de redirect direct. Il génère un consent_id
    et retourne l'URL `/consent?consent_id=...` pour notre page consent
    custom qui demande la clé PermisAPI à l'utilisateur.
  - La page consent valide la clé en faisant `GET /v1/me` sur l'API
    backend. Si valide, elle appelle `complete_consent()` qui génère un
    authorization code et redirige le client OAuth vers son `redirect_uri`.
  - `load_access_token()` accepte aussi les clés API directes
    (`pk_live_*` / `pk_test_*`) pour permettre aux clients qui supportent
    Bearer custom (Cursor, Windsurf, MCP Inspector, ChatGPT) de bypass
    le flow OAuth complet.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl


# ---------------------------------------------------------------------------
# Subclasses pour stocker la cle API PermisAPI dans les tokens / codes
# (FastMCP n'expose pas ces champs au client final, OK pour stocker des
# secrets internes selon la doc SDK).
# ---------------------------------------------------------------------------


class PermisApiAuthorizationCode(AuthorizationCode):
    api_key: str


class PermisApiAccessToken(AccessToken):
    api_key: str


class PermisApiRefreshToken(RefreshToken):
    api_key: str


@dataclass
class PendingConsent:
    """Etat entre `authorize()` et la soumission de la page /consent.

    Quand le client OAuth (ex Claude.ai) demande l'autorisation, on stocke
    les params (PKCE challenge, redirect_uri, etc.) le temps que
    l'utilisateur final colle sa cle API dans notre page consent.
    """

    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float  # epoch seconds


# Defaults TTL (configurables au constructeur)
DEFAULT_CONSENT_TTL = 5 * 60  # 5 min pour completer le consent screen
DEFAULT_CODE_TTL = 5 * 60  # 5 min pour echanger le code contre un token
DEFAULT_ACCESS_TOKEN_TTL = 8 * 3600  # 8 heures (ressaisi consent ensuite)


class PermisApiOAuthProvider:
    """Provider OAuth in-memory.

    Compatible avec le Protocol `OAuthAuthorizationServerProvider` du SDK MCP.
    Le SDK utilise le duck-typing (Protocol), donc pas besoin d'heriter
    explicitement, mais on respecte la signature de chaque methode.
    """

    def __init__(
        self,
        mcp_base_url: str,
        api_backend_url: str = "https://api.permisapi.fr",
        *,
        consent_ttl_seconds: int = DEFAULT_CONSENT_TTL,
        code_ttl_seconds: int = DEFAULT_CODE_TTL,
        access_token_ttl_seconds: int = DEFAULT_ACCESS_TOKEN_TTL,
    ) -> None:
        self.mcp_base_url = mcp_base_url.rstrip("/")
        self.api_backend_url = api_backend_url.rstrip("/")
        self.consent_ttl = consent_ttl_seconds
        self.code_ttl = code_ttl_seconds
        self.access_token_ttl = access_token_ttl_seconds

        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending_consents: dict[str, PendingConsent] = {}
        self._auth_codes: dict[str, PermisApiAuthorizationCode] = {}
        self._access_tokens: dict[str, PermisApiAccessToken] = {}
        self._refresh_tokens: dict[str, PermisApiRefreshToken] = {}

    # ---------------------------------------------------------------------
    # DCR : enregistrement dynamique de clients (RFC 7591)
    # ---------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self._clients.get(client_id)
        if client is None:
            return None
        # On ne fait pas expirer les clients en V1 (Claude.ai re-enregistre
        # un nouveau client a chaque ajout de connecteur, donc OK).
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Le SDK genere deja un client_id et client_secret avant d'appeler
        # cette methode. On stocke juste.
        if client_info.client_id in self._clients:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="Client deja enregistre",
            )
        self._clients[client_info.client_id] = client_info

    # ---------------------------------------------------------------------
    # Authorization : redirection vers notre page consent custom
    # ---------------------------------------------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Genere un consent_id et retourne l'URL de notre page consent.

        Le SDK MCP s'attend a recevoir une URL vers laquelle rediriger
        le browser de l'utilisateur. Notre page consent custom va :
        1. Afficher un formulaire HTML demandant la cle PermisAPI
        2. Valider la cle contre `api.permisapi.fr/v1/me`
        3. Appeler `complete_consent()` ci-dessous pour generer le code
        4. Rediriger vers `params.redirect_uri` (= callback du client OAuth)
           avec ?code=... et ?state=...
        """
        consent_id = token_urlsafe(32)
        self._pending_consents[consent_id] = PendingConsent(
            client=client,
            params=params,
            expires_at=time.time() + self.consent_ttl,
        )
        # URL de notre page consent (servie par sse_server.py)
        return f"{self.mcp_base_url}/consent?consent_id={consent_id}"

    async def complete_consent(
        self,
        consent_id: str,
        api_key: str,
    ) -> tuple[str, str | None, AnyUrl]:
        """Appele par la page /consent apres validation de la cle API.

        Retourne `(auth_code, state, redirect_uri)`. L'appelant (endpoint
        /consent/submit dans sse_server.py) construit le redirect final
        vers le client OAuth.
        """
        pending = self._pending_consents.pop(consent_id, None)
        if pending is None:
            raise ValueError("consent_id invalide ou deja consomme")
        if pending.expires_at < time.time():
            raise ValueError("consent expire, recommencez l'autorisation")

        auth_code_str = token_urlsafe(48)
        self._auth_codes[auth_code_str] = PermisApiAuthorizationCode(
            code=auth_code_str,
            scopes=pending.params.scopes or [],
            expires_at=time.time() + self.code_ttl,
            client_id=pending.client.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            api_key=api_key,
        )
        return auth_code_str, pending.params.state, pending.params.redirect_uri

    async def get_pending_consent(self, consent_id: str) -> PendingConsent | None:
        """Helper pour la page /consent (GET) qui veut afficher le nom du
        client OAuth demandeur (ex 'Claude.ai') avant le submit."""
        pending = self._pending_consents.get(consent_id)
        if pending is None or pending.expires_at < time.time():
            return None
        return pending

    async def validate_api_key(self, api_key: str) -> dict[str, Any] | None:
        """Verifie qu'une cle PermisAPI est valide en appelant /v1/me.

        Retourne le payload /v1/me si OK (avec email, plan, etc.), None
        si la cle est invalide ou si l'API backend est down.
        """
        if not api_key or not api_key.startswith(("pk_live_", "pk_test_")):
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.get(
                    f"{self.api_backend_url}/v1/me",
                    headers={"X-API-Key": api_key},
                )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:  # noqa: BLE001 - we treat any error as invalid key
            return None

    # ---------------------------------------------------------------------
    # Authorization code : load + exchange
    # ---------------------------------------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> PermisApiAuthorizationCode | None:
        code_obj = self._auth_codes.get(authorization_code)
        if code_obj is None:
            return None
        if code_obj.client_id != client.client_id:
            return None
        if code_obj.expires_at < time.time():
            self._auth_codes.pop(authorization_code, None)
            return None
        return code_obj

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: PermisApiAuthorizationCode,
    ) -> OAuthToken:
        # Single-use : on retire le code de la map.
        self._auth_codes.pop(authorization_code.code, None)

        access_token_str = token_urlsafe(48)
        refresh_token_str = token_urlsafe(48)
        now = int(time.time())
        expires_at_int = now + self.access_token_ttl

        self._access_tokens[access_token_str] = PermisApiAccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=expires_at_int,
            api_key=authorization_code.api_key,
        )
        self._refresh_tokens[refresh_token_str] = PermisApiRefreshToken(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=None,
            api_key=authorization_code.api_key,
        )

        return OAuthToken(
            access_token=access_token_str,
            refresh_token=refresh_token_str,
            token_type="bearer",
            expires_in=self.access_token_ttl,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    # ---------------------------------------------------------------------
    # Refresh token : load + exchange
    # ---------------------------------------------------------------------

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> PermisApiRefreshToken | None:
        rt = self._refresh_tokens.get(refresh_token)
        if rt is None or rt.client_id != client.client_id:
            return None
        if rt.expires_at and rt.expires_at < int(time.time()):
            return None
        return rt

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: PermisApiRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotation : on retire l'ancien refresh + on emet une nouvelle paire
        self._refresh_tokens.pop(refresh_token.token, None)

        new_access = token_urlsafe(48)
        new_refresh = token_urlsafe(48)
        now = int(time.time())
        expires_at_int = now + self.access_token_ttl

        self._access_tokens[new_access] = PermisApiAccessToken(
            token=new_access,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=expires_at_int,
            api_key=refresh_token.api_key,
        )
        self._refresh_tokens[new_refresh] = PermisApiRefreshToken(
            token=new_refresh,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=None,
            api_key=refresh_token.api_key,
        )

        return OAuthToken(
            access_token=new_access,
            refresh_token=new_refresh,
            token_type="bearer",
            expires_in=self.access_token_ttl,
            scope=" ".join(scopes or refresh_token.scopes) if (scopes or refresh_token.scopes) else None,
        )

    # ---------------------------------------------------------------------
    # Access token : load + revoke
    # ---------------------------------------------------------------------

    async def load_access_token(self, token: str) -> PermisApiAccessToken | None:
        """Charge un access token.

        Deux modes :
        1. Token OAuth (genere par /token apres flow consent) : lookup dans
           `_access_tokens` avec verification d'expiration.
        2. Cle API PermisAPI directe (`pk_live_*` ou `pk_test_*`) :
           on emballe la cle dans un AccessToken ephemere pour les clients
           qui supportent Bearer header custom (Cursor, Windsurf, MCP
           Inspector, ChatGPT custom GPT). Pas stocke en memoire.
        """
        # Mode 1 : OAuth token
        token_obj = self._access_tokens.get(token)
        if token_obj is not None:
            if token_obj.expires_at and token_obj.expires_at < int(time.time()):
                # Cleanup au passage
                self._access_tokens.pop(token, None)
                return None
            return token_obj

        # Mode 2 : Cle API directe (bypass OAuth pour clients hors Claude.ai web)
        if token.startswith(("pk_live_", "pk_test_")):
            return PermisApiAccessToken(
                token=token,
                client_id="direct-api-key",
                scopes=[],
                expires_at=None,
                api_key=token,
            )

        return None

    async def revoke_token(
        self,
        token: PermisApiAccessToken | PermisApiRefreshToken | AccessToken | RefreshToken,
    ) -> None:
        token_str = token.token
        self._access_tokens.pop(token_str, None)
        self._refresh_tokens.pop(token_str, None)

    # ---------------------------------------------------------------------
    # Helpers internes (pas dans le Protocol)
    # ---------------------------------------------------------------------

    def cleanup_expired(self) -> int:
        """Nettoie les entrees expirees. Retourne le nombre supprimees.

        A appeler periodiquement (background task) pour eviter une fuite
        memoire si le service tourne longtemps sans restart.
        """
        now = time.time()
        removed = 0

        for cid, pc in list(self._pending_consents.items()):
            if pc.expires_at < now:
                self._pending_consents.pop(cid, None)
                removed += 1

        for code, ac in list(self._auth_codes.items()):
            if ac.expires_at < now:
                self._auth_codes.pop(code, None)
                removed += 1

        for tok, at in list(self._access_tokens.items()):
            if at.expires_at and at.expires_at < int(now):
                self._access_tokens.pop(tok, None)
                removed += 1

        return removed
