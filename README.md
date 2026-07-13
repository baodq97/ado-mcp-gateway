# ado-mcp-gateway

A self-hosted **remote MCP server for Azure DevOps** that works with any
OAuth-capable MCP client — Claude Code, Claude Desktop, ChatGPT, custom
agents — today.

Microsoft ships an official remote server at `https://mcp.dev.azure.com/{org}`,
but it only supports VS Code and Visual Studio. Every other client is blocked on
Microsoft Entra ID shipping OAuth **Dynamic Client Registration**
([official FAQ](https://learn.microsoft.com/en-us/azure/devops/mcp-server/remote-mcp-server)),
and Entra DCR is
["not in our roadmap for now"](https://en.ittrip.xyz/saas/entra-id-dcr-mcp).

This gateway bridges the gap with the **OAuth-proxy / simulated-DCR** pattern: it
speaks full MCP OAuth 2.1 (with DCR) to the client, and confidential-client OAuth
to Entra, translating between the two.

```
MCP client ──OAuth 2.1 (simulated DCR + PKCE)──▶ Gateway ──Entra login──▶ user token (aud = gateway app)
                                                    │
                                                    ├── On-Behalf-Of ──▶ ADO-audience token, per user
                                                    └── stdio ──▶ @azure-devops/mcp ──▶ dev.azure.com
```

- **Front door** — [FastMCP](https://gofastmcp.com) `AzureProvider` exposes
  `/register` (RFC 7591), Protected Resource Metadata (RFC 9728), Authorization
  Server Metadata (RFC 8414) and PKCE. Clients self-register against the gateway;
  Entra only ever sees **one static app registration**.
- **Backend** — [microsoft/azure-devops-mcp](https://github.com/microsoft/azure-devops-mcp)
  (stdio) proxied to Streamable HTTP, **one subprocess per user token**, so every
  Azure DevOps call runs under the calling user's identity (real per-user audit
  in ADO).

## Quick start

### 1. Register one Entra application (once)

Choose the sign-in audience to match your use:

- **Internal / single-tenant** — `--sign-in-audience AzureADMyOrg`, and set
  `ADO_GW_TENANT` to your directory GUID. Only accounts in that directory can
  sign in.
- **Multi-tenant** — `--sign-in-audience AzureADMultipleOrgs`, and leave
  `ADO_GW_TENANT=organizations`. Any Entra work/school account can sign in.

```bash
az ad app create --display-name "ADO MCP Gateway" \
  --web-redirect-uris "http://localhost:8090/auth/callback" \
  --sign-in-audience AzureADMyOrg   # or AzureADMultipleOrgs
az ad app credential reset --id <APP_ID> --append          # → client secret
az ad sp create --id <APP_ID>

# Delegated permission: Azure DevOps / user_impersonation
az ad app permission add --id <APP_ID> \
  --api 499b84ac-1321-427f-aa17-267ca6975798 \
  --api-permissions ee69721e-6c3a-468f-a9ec-302d16a4c599=Scope

# Expose the app's OWN scope and force v2 access tokens (see Trap 1)
az rest --method PATCH \
  --url "https://graph.microsoft.com/v1.0/applications/<OBJECT_ID>" \
  --headers 'Content-Type=application/json' --body "{
    \"identifierUris\": [\"api://<APP_ID>\"],
    \"api\": {
      \"requestedAccessTokenVersion\": 2,
      \"oauth2PermissionScopes\": [{
        \"id\": \"$(uuidgen)\", \"value\": \"user_impersonation\", \"type\": \"User\",
        \"isEnabled\": true,
        \"adminConsentDisplayName\": \"Access the ADO MCP gateway\",
        \"adminConsentDescription\": \"Access the ADO MCP gateway on behalf of the signed-in user\",
        \"userConsentDisplayName\": \"Access the ADO MCP gateway\",
        \"userConsentDescription\": \"Access the ADO MCP gateway on your behalf\"
      }]
    }
  }"
```

Optional: `az ad app permission admin-consent --id <APP_ID>` (needs an Entra
admin) grants the app for the whole directory, so users never see a consent
screen.

### 2. Configure

```bash
cp .env.example .env      # fill in client id / secret / org / tenant
```

| Variable | Required | Notes |
|---|---|---|
| `ADO_GW_CLIENT_ID` / `ADO_GW_CLIENT_SECRET` | yes | The Entra app above |
| `ADO_GW_ORG` | yes | Azure DevOps organization (positional arg of the backend) |
| `ADO_GW_TENANT` | no | Login directory. A tenant GUID for single-tenant/internal apps; `organizations` (default) for multi-tenant. See "Login directory" |
| `ADO_GW_BASE_URL` | prod | Public HTTPS URL of the gateway (defaults to `http://localhost:8090`) |
| `ADO_GW_TOKEN_MODE` | no | `passthrough` (default) or `phantom`. See "Token modes" |

### 3. Run

**Local (uv):**

```bash
uv sync
set -a; source .env; set +a
uv run python server.py
```

**Docker:**

```bash
docker build -t ado-mcp-gateway .
docker run -p 8090:8090 --env-file .env ado-mcp-gateway
```

The image bakes `@azure-devops/mcp` and a Node runtime, so no per-request `npx`
download.

### 4. Connect a client

```bash
claude mcp add --transport http ado-gateway http://localhost:8090/mcp
# then: /mcp → ado-gateway → Authenticate
```

Any MCP client that supports the OAuth 2.1 authorization flow works the same way
— point it at `<base-url>/mcp`.

## Token modes

The gateway can hand the client two different kinds of token. Pick with
`ADO_GW_TOKEN_MODE`.

| | `passthrough` (default) | `phantom` |
|---|---|---|
| Client receives | The **real Entra** access + refresh tokens | Gateway-minted JWTs (`aud` = gateway URL) |
| Gateway state | None — restarts don't log anyone out; replicas need no shared store | Token store (needs a fixed `jwt_signing_key` + shared `client_storage` for multi-replica) |
| Revocation | At Entra (revoke sessions / Conditional Access) | At the gateway |
| RFC 9728 `aud == resource` clients | **Rejected** — token `aud` is the Entra app, not the gateway URL | **Accepted** |

**Passthrough** is stateless and delegates the whole token lifecycle to Entra
(revocation) and Azure DevOps (permissions) — the right default when the client
is a trusted developer tool. It overrides four `OAuthProxy` methods, so pin the
`fastmcp` version.

**Phantom** is FastMCP's designed path: the gateway mints its own JWTs and keeps
the upstream tokens server-side. Use it for clients that enforce
[RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) (access-token audience must
equal the resource) — those reject passthrough tokens.

## Login directory (multi-tenant accounts)

There is **no tenant picker**: `organizations` always signs into the account's
*home* tenant. If the ADO organization lives in a directory where your users are
guests, set `ADO_GW_TENANT` to that directory's GUID. Discover which tenant backs
an org without logging in:

```bash
curl -sI https://dev.azure.com/{org}/_apis/connectionData | grep -i x-vss-resourcetenant
```

## Traps worth knowing

Each of these cost real debugging time; they are baked into the code but explained
here so you can adapt it.

1. **Never request the ADO scope as `required_scopes`.** Asking for
   `499b84ac-.../user_impersonation` directly makes Entra mint a **v1 token with
   `aud = Azure DevOps`** (`iss = sts.windows.net/...`) that the gateway cannot
   validate. Instead the app exposes its **own** `api://{app_id}/user_impersonation`
   scope (with `requestedAccessTokenVersion: 2`); the ADO token is obtained later
   via On-Behalf-Of at point of use. The ADO scope goes in
   `additional_authorize_scopes` only, which records the consent OBO needs without
   changing the issued token's audience.

2. **Multi-tenant issuer validation.** v2 tokens carry
   `iss = login.microsoftonline.com/{tenantGUID}/v2.0`, which never equals the
   literal `.../organizations/v2.0` the provider derives, so the gateway disables
   the issuer check (the same thing `AzureProvider.from_b2c` does) — audience
   validation still rejects foreign tokens. OBO must likewise target the **user's
   tenant** (`tid` claim), not `/organizations`, or Entra rejects the
   confidential-client grant.

3. **ESTS propagation lag.** After PATCHing `identifierUris` /
   `oauth2PermissionScopes`, Microsoft Graph shows the scope immediately but the
   token service keeps returning `AADSTS65005` for ~2–15 minutes. Don't re-debug
   config that is already correct — probe the authorize URL until the error clears.

**Bonus:** the async `OnBehalfOfCredential` needs the `aiohttp` extra
(`azure.identity.aio` fails at runtime without it, surfacing as a cryptic
JSON-RPC `-32602`), and `@azure-devops/mcp` requires the org positionally
(`demandOption: true`), so omitting it kills the backend at spawn.

## HTTP Basic client auth

Some clients send client credentials as an `Authorization: Basic` header on
`/token` (`client_secret_basic`). RFC 6749 §2.3.1 requires token endpoints to
accept that, but the MCP SDK's handler only reads the form body. A small ASGI
shim (`BasicAuthToFormShim`) decodes the header into form fields so both styles
work.

## Production checklist

- [ ] `ADO_GW_BASE_URL` set to the public HTTPS URL; add `https://<host>/auth/callback`
      to the app's redirect URIs.
- [ ] Phantom mode + multiple replicas: set a fixed `jwt_signing_key` and a shared
      `client_storage` (e.g. Redis) wrapped in `FernetEncryptionWrapper`. Passthrough
      mode needs none of this.
- [ ] Replace the client secret with a **managed-identity federated credential**
      (OBO supports client assertions), or at least store it in a vault.
- [ ] Tighten `allowed_client_redirect_uris` (currently allow-all).
- [ ] Budget ~150 MB per concurrent user (one Node subprocess each, LRU-capped).

## How it works

`server.py` is the whole gateway (~250 lines):

- `PassthroughAzureProvider` — the four `OAuthProxy` overrides for passthrough mode.
- `build_auth()` — configures the FastMCP `AzureProvider` (or the passthrough
  subclass) and disables issuer validation for the multi-tenant case.
- `PerUserAdoBackends` — per-user OBO exchange plus an LRU pool of
  `@azure-devops/mcp` subprocesses, one per ADO token.
- `BasicAuthToFormShim` — the RFC 6749 §2.3.1 client-auth shim.

## License

MIT.
