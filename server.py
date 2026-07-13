"""ADO MCP Gateway — self-hosted remote MCP for Azure DevOps (passthrough).

OAuth 2.1 + simulated DCR (Entra has no DCR) over ONE static Entra app;
clients receive the real Entra tokens verbatim — the gateway keeps no token
state. Backend = microsoft/azure-devops-mcp (stdio), one subprocess per user
via OBO. Traps & setup → README.md.
"""

import hashlib
import logging
import os
from base64 import b64decode
from collections import OrderedDict
from urllib.parse import quote

import httpx
from azure.identity.aio import OnBehalfOfCredential
from fastmcp.client import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.server.auth.providers.azure import AzureProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.providers.proxy import FastMCPProxy
from fastmcp.utilities.auth import decode_jwt_payload
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger("ado-gateway")

CLIENT_ID = os.environ["ADO_GW_CLIENT_ID"]
CLIENT_SECRET = os.environ["ADO_GW_CLIENT_SECRET"]
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"  # Azure DevOps first-party app
ADO_ORG = os.environ["ADO_GW_ORG"]
BASE_URL = os.environ.get("ADO_GW_BASE_URL", "http://localhost:8090")
# "organizations" = account's HOME tenant (no tenant picker exists). Set a GUID
# when the ADO org lives in a directory where users are guests. Discover it:
#   curl -sI https://dev.azure.com/{org}/_apis/connectionData | grep -i x-vss-resourcetenant
AUTH_TENANT = os.environ.get("ADO_GW_TENANT", "organizations")
# passthrough: Entra tokens verbatim, stateless — but clients that enforce
# RFC 9728 (access-token aud == resource) REJECT them. phantom: gateway-minted
# JWTs whose aud is the gateway URL — required by such clients. See README.
TOKEN_MODE = os.environ.get("ADO_GW_TOKEN_MODE", "passthrough")
# Backend launcher. Local dev defaults to `npx -y @azure-devops/mcp`; the Docker
# image bakes the package and sets these to run `node .../index.js` directly.
BACKEND_CMD = os.environ.get("ADO_GW_BACKEND_CMD", "npx")
BACKEND_PREFIX = (
    [os.environ["ADO_GW_BACKEND_ARG"]] if os.environ.get("ADO_GW_BACKEND_ARG") else ["-y", "@azure-devops/mcp"]
)


def _claims(token: str) -> dict:
    """Unverified payload decode — routing claims only, token already verified."""
    return decode_jwt_payload(token) or {}


class PassthroughAzureProvider(AzureProvider):
    """Returns upstream Entra tokens to the client verbatim (no gateway JWTs)."""

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        code_model = await self._code_store.get(key=authorization_code.code)
        if not code_model:
            raise TokenError("invalid_grant", "Authorization code not found")
        idp = code_model.idp_tokens
        await self._code_store.delete(key=authorization_code.code)  # one-time use
        granted = self._translate_scopes_from_idp(
            idp["scope"].split() if "scope" in idp else list(authorization_code.scopes)
        )
        return OAuthToken(
            access_token=idp["access_token"],
            token_type="Bearer",
            expires_in=int(idp.get("expires_in", 3600)),
            refresh_token=idp.get("refresh_token"),
            scope=" ".join(granted),
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        # The opaque Entra refresh token, as-is (it is not our JWT)
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id or "",
            scopes=list(self.required_scopes or []),
            expires_at=None,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Forward the grant to Entra with the app secret the public client lacks
        http = getattr(self, "_refresh_http", None)
        if http is None or http.is_closed:
            http = self._refresh_http = httpx.AsyncClient(timeout=15, follow_redirects=False)
        wire_scopes = self._prepare_scopes_for_upstream_refresh(scopes or list(self.required_scopes or []))
        resp = await http.post(
            self._upstream_token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token.token,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": " ".join(wire_scopes),
            },
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            logger.warning("upstream refresh failed status=%d", resp.status_code)
            raise TokenError("invalid_grant", "Upstream refresh failed")
        data = resp.json()
        return OAuthToken(
            access_token=data["access_token"],
            token_type="Bearer",
            expires_in=int(data.get("expires_in", 3600)),
            refresh_token=data.get("refresh_token", refresh_token.token),  # Entra rotates
            scope=data.get("scope", " ".join(scopes)),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Bearer IS the Entra JWT — validate directly against Entra JWKS
        return await self._token_validator.verify_token(token)


def build_auth() -> AzureProvider:
    cls = AzureProvider if TOKEN_MODE == "phantom" else PassthroughAzureProvider
    auth = cls(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        tenant_id=AUTH_TENANT,
        base_url=BASE_URL,
        # Our app's OWN scope (api://{client_id}/user_impersonation) — NEVER the
        # ADO scope directly (yields a v1 ADO-audience token we can't validate).
        # ADO access happens via OBO; its scope below only records CONSENT.
        required_scopes=["user_impersonation"],
        additional_authorize_scopes=[f"{ADO_RESOURCE}/user_impersonation"],
        allowed_client_redirect_uris=None,  # PoC; tighten in prod
    )
    # Multi-tenant (organizations/common/consumers): the token's iss is the
    # per-tenant GUID, never the literal ".../{AUTH_TENANT}/v2.0" the provider
    # derives — disable issuer check (same trick as from_b2c); audience check
    # still rejects foreign tokens. Single-tenant (a GUID) needs no such waiver:
    # the derived issuer matches, so keep it enforced.
    if AUTH_TENANT in {"organizations", "common", "consumers"}:
        auth._token_validator.issuer = None
    return auth


class PerUserAdoBackends:
    """One azure-devops-mcp subprocess per ADO token; OBO targets the token's tid
    (Entra rejects confidential OBO on /organizations). Both caches LRU-bounded —
    hourly token rotation would otherwise leak credentials/subprocesses."""

    def __init__(self, max_backends: int = 32):
        self._clients: OrderedDict[str, Client] = OrderedDict()
        self._obo: OrderedDict[str, OnBehalfOfCredential] = OrderedDict()
        self._max = max_backends

    async def _evict(self, cache: OrderedDict) -> None:
        while len(cache) > self._max:
            _, evicted = cache.popitem(last=False)
            try:
                await evicted.close()
            except Exception:
                logger.debug("evict close failed", exc_info=True)

    async def _ado_token(self, user_token: str) -> str:
        tid = _claims(user_token)["tid"]
        key = hashlib.sha256(f"{tid}:{user_token}".encode()).hexdigest()
        cred = self._obo.get(key)
        if cred is None:
            cred = self._obo[key] = OnBehalfOfCredential(
                tenant_id=tid, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, user_assertion=user_token
            )
            await self._evict(self._obo)
        else:
            self._obo.move_to_end(key)
        return (await cred.get_token(f"{ADO_RESOURCE}/.default")).token

    async def client_for_request(self) -> Client:
        access = get_access_token()
        if access is None:
            raise RuntimeError("No access token in request context")
        ado_token = await self._ado_token(access.token)
        key = hashlib.sha256(ado_token.encode()).hexdigest()
        client = self._clients.get(key)
        if client is not None:
            self._clients.move_to_end(key)
            return client
        logger.info("Spawning ADO backend for user=%s", _claims(ado_token).get("upn", "?"))
        client = self._clients[key] = Client(
            StdioTransport(
                command=BACKEND_CMD,
                args=[*BACKEND_PREFIX, ADO_ORG, "--authentication", "envvar"],
                env={**os.environ, "ADO_MCP_AUTH_TOKEN": ado_token},
            )
        )
        await self._evict(self._clients)
        return client


class BasicAuthToFormShim:
    """POST /token: copy HTTP Basic client creds into the form body — RFC 6749
    §2.3.1 requires Basic support but the MCP SDK only reads the body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/token" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        auth_header = dict(scope["headers"]).get(b"authorization", b"")
        if not auth_header.lower().startswith(b"basic "):
            return await self.app(scope, receive, send)

        body, more = b"", True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        try:
            client_id, _, client_secret = b64decode(auth_header[6:].strip()).decode().partition(":")
            if b"client_id=" not in body:
                extra = f"client_id={quote(client_id, safe='')}"
                if client_secret:
                    extra += f"&client_secret={quote(client_secret, safe='')}"
                body += (b"&" if body else b"") + extra.encode()
                logger.info("token.basic_auth_translated client_id=%s", client_id)
        except Exception:
            logger.warning("token.basic_auth_decode_failed", exc_info=True)

        headers = [(k, v) for k, v in scope["headers"] if k not in (b"authorization", b"content-length")]
        headers.append((b"content-length", str(len(body)).encode()))
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return await self.app({**scope, "headers": headers}, replay, send)


def build_server() -> FastMCPProxy:
    return FastMCPProxy(
        name="ado-mcp-gateway",
        client_factory=PerUserAdoBackends().client_for_request,
        auth=build_auth(),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        BasicAuthToFormShim(build_server().http_app()),
        host=os.environ.get("ADO_GW_HOST", "127.0.0.1"),
        port=int(os.environ.get("ADO_GW_PORT", "8090")),
    )
