# reclaimerr-mcp-server

A minimal [Model Context Protocol](https://modelcontextprotocol.io) server that
connects to [Reclaimerr](https://jessielw.github.io/Reclaimerr/) (automated
media-library cleanup — reclaims disk space by identifying unwatched/low-rated
media across Jellyfin/Plex/Emby and managing deletion through Sonarr/Radarr),
packaged for Docker. It has two independent groups of tools — the **general
tools** (candidates, protections, tasks; scoped API token) and the **rules
tools** (manage cleanup rules; admin login, human approval for anything risky) —
each switched on by its own credentials. See [Tools](#tools) and
[Rules management](#rules-management-admin-login).

It runs as a standing network service (streamable-http transport, not stdio),
so any MCP client on your internal network can connect to
`http://<host>:<port>/mcp` — the container isn't spawned per-client, and
container lifecycle/updates can be handed off to a tool like
[Dockhand](https://dockhand.pro).

## API assumptions

Reclaimerr ships a real, versioned external API at a fixed `/api/v1` prefix
(FastAPI backend, source: [`backend/api/routes/v1/`](https://github.com/jessielw/Reclaimerr/tree/main/backend/api/routes/v1)),
authenticated with scoped Bearer API tokens (`rcl_<prefix>_<secret>`, created
in its own UI). This server was built directly against that source (repo at
[jessielw/Reclaimerr](https://github.com/jessielw/Reclaimerr), backend
version 0.4.7 at the time of writing) since Reclaimerr's docs site doesn't
publish full endpoint-level API reference — **it has not been exercised
against a live instance yet.** Run `GET /ready` (see below) after configuring
`RECLAIMERR_URL`/`RECLAIMERR_API_TOKEN` and, ideally, run the opt-in live
contract tests (`tests/test_live_reclaimerr.py`) once before relying on this
in anything important — a point release could have renamed a field these
tools depend on.

Unlike Sonarr/Radarr/Prowlarr, there's no server-reported API version to
negotiate (no `RECLAIMERR_API_VERSION` env var here) — Reclaimerr's `/api/v1`
prefix is fixed, and `GET /api/v1` itself reports `api_version` informationally.

## Tools

### General tools (need `RECLAIMERR_API_TOKEN`)

| Tool | Description | Required scope |
|---|---|---|
| `list_candidates` | List reclaim candidates (media flagged for possible deletion), optionally filtered by media type or auto-delete state | `candidates:read` |
| `candidate_status` | Full lifecycle detail for one candidate by ID | `candidates:read` |
| `protect_candidate` | Permanently protect a candidate from deletion | `candidates:manage` |
| `postpone_candidate` | Push a candidate's deletion deadline to a later timestamp | `candidates:manage` |
| `cancel_candidate_deletion` | Cancel a pending scheduled deletion | `candidates:manage` |
| `list_protections` | List protected (deletion-exempt) media | `protections:read` |
| `list_tasks` | List background tasks (media sync, candidate scan, cleanup, ...) with schedule/status | `tasks:read` |
| `run_task` | Trigger an immediate run of a background task | `tasks:run` |
| `system_status` | Reclaimerr version, capabilities, last sync/scan times | `system:read` |

`protect_candidate`, `postpone_candidate`, `cancel_candidate_deletion`, and
`run_task` are the tools that change state in Reclaimerr. Everything else is
read-only. Scope your `RECLAIMERR_API_TOKEN` down to just the tools you want
an MCP client to be able to call — e.g. a read-only token (no `:manage` or
`:run` scopes) if you only want visibility, not control.

### Rules tools (need an admin login **and** `MCP_AUTH_TOKEN`)

| Tool | Description | Approval |
|---|---|---|
| `rules_status` | Which tool groups are enabled and, if any aren't, exactly why. **Always listed.** | — |
| `list_rules` / `get_rule` | Read the cleanup rules (use them as templates for `definition`/`action`) | — |
| `preview_rule` | Dry-run a rule definition: how many items it *would* match, with examples. Saves nothing. | — |
| `create_rule` | Create a rule. **Always saved disabled with auto-delete off**, after a dry-run whose result is returned. | none needed (nothing can act until you enable it) |
| `update_rule` | Change a rule's name/description/scope/definition/action. Cannot enable/disable it or change auto-delete. | **prompt if the rule is currently enabled** |
| `delete_rule` | Delete a rule | **always prompts** |

Tools that aren't available are **not listed at all** (rather than listed and
failing), and the server's `instructions` point the AI at `rules_status` to find
out why.

## Health endpoints

Two plain HTTP endpoints, reachable without `MCP_AUTH_TOKEN` (so Docker's
`HEALTHCHECK`, Dockhand, or any other monitor can poll them without the
secret):

| Endpoint | Checks | Healthy | Unhealthy |
|---|---|---|---|
| `GET /health` | The process is up and serving HTTP. Does **not** call Reclaimerr. | `200 {"status": "ok"}` | (doesn't respond) |
| `GET /ready` | `RECLAIMERR_URL` is reachable and `RECLAIMERR_API_TOKEN` is valid with at least the `system:read` scope (via Reclaimerr's `/api/v1/system`). Always includes a `rules` block (see below). | `200 {"status": "ok", "reachable": true, "authenticated": true, "reclaimerr": {...}, "rules": {...}}` | `503 {"status": "error", "reachable": ..., "authenticated": ..., "error": "...", "rules": {...}}` |

`/ready` distinguishes a 401 (bad/revoked token) from a 403 (valid token,
missing the `system:read` scope) in its `error` message — useful when a token
was created with the wrong scopes.

The `rules` block (`{"enabled", "status", "reason", "account"}`) reports the
rules tools' state from the startup check, and **a rules problem never makes
`/ready` unhealthy** — the general tools still work. It never triggers a fresh
login (Reclaimerr rate-limits those). If only the username/password is
configured (no `RECLAIMERR_API_TOKEN`), readiness *is* the rules check.

## Rules management (admin login)

Reclaimerr's versioned `/api/v1` has no rules endpoints, so the rules tools use
the same `/api/rules` routes the Reclaimerr web UI uses. Those authenticate with
a **session cookie**, not an API token, so this server logs in itself.

**Requirements**

- **The account must be an admin.** Every rules route in Reclaimerr requires the
  admin role; a non-admin account gets HTTP 403. Use a dedicated admin account
  rather than your own, so this server's sessions are easy to spot and revoke.
- **`MCP_AUTH_TOKEN` must be set.** The rules tools hold an admin login, so they
  refuse to run on a server that anyone on the network can call.
- These routes are **undocumented and not versioned**; they were read from
  Reclaimerr's source and may change between releases (backend v0.4.x at the time
  of writing). The login flow was verified against a live instance with a
  standalone script, but the tools themselves have not yet been run against one —
  try them on something you can afford to get wrong first.

**Startup check.** On startup the server logs in and checks the account is an
admin and can read `/api/rules`, then lists tools accordingly:

| Outcome | Rules tools | What happens |
|---|---|---|
| Passes | listed | — |
| Missing config, wrong password, disabled account, **not an admin**, no `/api/rules` route | hidden | Stays that way until you fix the settings and **restart the container**. Reason in the log, `/ready`, and `rules_status` |
| Reclaimerr unreachable / login rate-limited / 5xx | hidden | Re-checked every 5 minutes (well under Reclaimerr's 5-logins-a-minute limit) and listed once it passes |

MCP clients read the tool list when they connect, so **reconnect the client**
after a fix.

**Sessions.** The login cookie lasts about 24 hours. On a `401` the server logs
in again and retries the request once (never in a loop — Reclaimerr rate-limits
login), so the expiry is invisible to you. Credentials are held only in the
container's environment; the cookie only in memory.

**Safety policy — enforced in code, not just described to the AI**

- `create_rule` **always saves the rule disabled with `auto_delete_enabled` off**
  whatever the AI passes, after a dry-run whose match count is returned. A
  disabled rule does nothing; you review it and enable it in the Reclaimerr UI. If
  the dry-run fails (e.g. an invalid definition) nothing is created.
- **Enabling a rule, and turning auto-delete on or off, are not possible through
  this server.** `update_rule` has no `enabled` argument and rejects any change to
  `auto_delete_enabled`. `action` updates are merged onto the rule's current action
  because Reclaimerr replaces it wholesale — otherwise omitting a key would
  silently reset it.
- **Updating an enabled rule, and deleting any rule, need your explicit approval**,
  asked via an [MCP elicitation](https://modelcontextprotocol.io) prompt shown by
  your client (a tick-box; unticked or cancelled means no). The prompt comes from
  the server to your client, **not through the AI**, so the AI can't answer it.
- **It fails closed.** If your client can't show approval prompts, the change is
  refused, never applied. Verified with Claude Code 2.1.278; other clients are
  untested, so check yours (`tests/test_approval_flow.py` shows how).

## Authentication

Set `MCP_AUTH_TOKEN` (a random shared secret — `openssl rand -hex 32`) and
every request must carry `Authorization: Bearer <token>` or the server
returns `401`. This is checked by a small Starlette middleware in front of
the MCP app, **not** the `mcp` SDK's built-in OAuth support
(`mcp.server.auth`) — that machinery expects a full OAuth authorization
server (issuer/resource metadata, RFC 8414/8707/9068 discovery), which is
unnecessary complexity for one secret shared by trusted LAN clients. This is
a separate secret from `RECLAIMERR_API_TOKEN` — the latter authenticates
*this server* to Reclaimerr, the former authenticates *MCP clients* to this
server.

Leave `MCP_AUTH_TOKEN` unset and the server runs with **no auth** — anything
that can reach `http://<host>:<port>/mcp` can call every tool, including the
state-changing ones. The server logs a warning on startup when it's running
this way. Either way, the trust boundary is still the network:

- **Do not** publish this port through any reverse proxy, port-forward, or
  anything else reachable from outside your LAN/VLAN — the bearer token
  protects against anyone *on* the network, not against the open internet.
- Bind the compose `ports:` mapping to a specific internal interface (e.g.
  `192.168.1.50:8941:8941`) rather than all interfaces, if you want to be
  stricter about which hosts on your network can reach it at all.

## Configuration

Environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `RECLAIMERR_URL` | yes | — | e.g. `http://192.168.1.50:8000` |
| `RECLAIMERR_API_TOKEN` | one of these two | — | Reclaimerr > Settings > Account > API Tokens (`rcl_...`). Enables the general tools |
| `RECLAIMERR_USERNAME` / `RECLAIMERR_PASSWORD` | one of these two | — | An **admin** Reclaimerr account (username or email). Enables the rules tools. Set both or neither |
| `MCP_HOST` | no | `0.0.0.0` | Interface the server binds to inside the container |
| `MCP_PORT` | no | `8941` | Port the server listens on |
| `MCP_AUTH_TOKEN` | no, but **required for the rules tools** | — | Shared secret required as `Authorization: Bearer <token>`. Unset = no auth (see above), and the rules tools stay disabled |

At least one of `RECLAIMERR_API_TOKEN` or the username/password pair must be set,
otherwise the server exits at startup. Which tools you get:

| Configured | General tools | Rules tools |
|---|---|---|
| `RECLAIMERR_API_TOKEN` only | ✅ | ❌ (`rules_status` says why) |
| username + password + `MCP_AUTH_TOKEN` only | ❌ | ✅ (once the admin check passes) |
| all of the above | ✅ | ✅ |
| username + password, **no** `MCP_AUTH_TOKEN` | per the token | ❌ — refuses to run an admin login on an open server |


## Image

Built and pushed to `ghcr.io/barrow1990/reclaimerr-mcp-server` by
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) on every push to
`main` that passes tests, tagged `:latest` and `:<commit-sha>`.
`docker-compose.yml` pulls `:latest` by default; swap in `build: .` there
instead if you'd rather build locally from the `Dockerfile`.

The image is a three-stage build: `builder` compiles dependencies into
`--target=/deps` (all of them, including `cryptography`'s compiled `cffi`
extension, ship musllinux wheels, so this needs no compiler even on alpine);
`prep` starts fresh from `python:3.12-alpine`, drops pip/setuptools/wheel,
strips stdlib pieces this headless server never touches (`tkinter`,
`idlelib`, `lib2to3`, `ensurepip`, ...), adds the non-root `app` user, and
copies in `/deps` and `server.py`; `runtime` then does a single
`COPY --from=prep / /` onto a `scratch` base — the same build shape used by
the `sonarr-mcp-server`/`radarr-mcp-server` images this one was adapted from,
landing at roughly the same **~98MB**.

## Running with Docker Compose

```bash
cp .env.example .env   # fill in RECLAIMERR_URL and the credentials you want (see Configuration)
docker compose up -d --pull always
```

The server is then reachable at `http://<docker-host>:8941/mcp` from anything
on your internal network.

## Managing with Dockhand

Point Dockhand at `ghcr.io/barrow1990/reclaimerr-mcp-server` and let it track
new tags. **Make the GHCR package public**, or every pull will need `docker
login ghcr.io` with a PAT on each deploy host. Set a restart policy of
`unless-stopped` (already in `docker-compose.yml`). See the `sonarr-mcp-server`/
`radarr-mcp-server` READMEs for the fuller rundown of `.env`/`.env.dockhand`
precedence — this repo follows the same pattern.

## Connecting a client

### Claude Code

```bash
claude mcp add reclaimerr -s user --transport http http://<docker-host>:8941/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```
(Drop the `--header` flag if you're running with `MCP_AUTH_TOKEN` unset.)

### Claude Desktop

Claude Desktop's built-in config expects a locally-spawned `command`, so for
a network server like this you'll need an HTTP-to-stdio bridge such as
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "reclaimerr": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "http://<docker-host>:8941/mcp",
        "--header", "Authorization: Bearer <MCP_AUTH_TOKEN>"
      ]
    }
  }
}
```

## Running without Docker

```bash
pip install -r requirements.txt
RECLAIMERR_URL=http://192.168.1.50:8000 RECLAIMERR_API_TOKEN=rcl_your_token \
MCP_AUTH_TOKEN=your-shared-secret python server.py
# add RECLAIMERR_USERNAME=... RECLAIMERR_PASSWORD=... to enable the rules tools
```

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

- `tests/test_tools.py` — each general tool's logic against a mocked Reclaimerr
  (`httpx.MockTransport`, no extra mocking library needed).
- `tests/test_rules.py` — the rules tools: which tools are listed, the startup
  admin check, login/re-login handling, each tool's safety policy, and the
  approval logic, against a `FakeReclaimerr` in `conftest.py`.
- `tests/test_approval_flow.py` — the human-approval flow end to end through the
  real MCP SDK, with a client callback playing the human (including a client that
  can't show prompts, which must fail closed).
- `tests/test_http.py` — `/health`, `/ready`, and the bearer-auth middleware,
  via `server.build_app()` (the exact app `__main__` runs) through Starlette's
  `TestClient`.
- `tests/test_live_reclaimerr.py` — **opt-in** contract tests against a real
  Reclaimerr instance, to catch drift if an upgrade renames/removes a field
  these tools depend on. Skipped by default (no Reclaimerr in CI); run with:
  ```bash
  RUN_LIVE_RECLAIMERR_TESTS=1 RECLAIMERR_URL=https://reclaimerr.example.com \
  RECLAIMERR_API_TOKEN=<real rcl_... token> python -m pytest tests/test_live_reclaimerr.py -v
  ```

CI (`.github/workflows/ci.yml`) runs the mocked suite on every push/PR; the
GHCR build only runs after it passes.

## License

MIT
