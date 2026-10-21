# streamdeck-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents read and
write **Elgato Stream Deck desktop profiles** — the plain-JSON manifests the
Elgato Stream Deck app uses — plus a small toolkit for generating icons,
wrapping shell commands in script-backed keys, and restarting the app so
changes take effect.

Fork of [verygoodplugins/streamdeck-mcp](https://github.com/verygoodplugins/streamdeck-mcp)
(MIT, Jack Arturo / Very Good Plugins), extended with:

- **`http_service.py`** — the same tool surface over **MCP Streamable HTTP**,
  behind a bearer-token gate, for agents that run on a *different* machine
  (the stdio server assumes its client runs where the deck is).
- **`deck_status`** — a one-call health verdict for remote agents.
- **`examples/`** — a launchd service template, an env template, and a
  read-only end-to-end smoke client.

Ask an agent to "put a key for restarting Docker on the top-left of my
deck" and it edits those manifests — no hunting for settings in the app.

## The one rule that bites

The Elgato desktop app keeps profiles **in memory** and rewrites the
manifest files *from memory* when it quits. Edits made behind the app's
back are clobbered at the next quit. The correct cycle is:

1. stop the app (`streamdeck_write_page` refuses to write while the app is
   running; `auto_quit_app=true` stops it for you),
2. make your manifest edits (one or more `streamdeck_write_page` calls),
3. restart the app (`streamdeck_restart_app`) so it reads your layout.

Always back up the profiles directory before writing.

## Transports

| Transport | When | How |
| --- | --- | --- |
| stdio | the MCP client runs on the deck's machine | the `streamdeck-mcp` CLI is spawned by the client (Claude Desktop, Claude Code, any MCP host) |
| Streamable HTTP | the agent runs elsewhere and needs the deck over the network | `streamdeck-mcp-service` (or `python http_service.py`) behind a bearer token; default bind `127.0.0.1:18802` |

The HTTP service adds one tool on top of the stdio surface: `deck_status`.

## Quick start — stdio

```bash
git clone <this repo>
python3.11 -m venv .venv
.venv/bin/pip install -e .
```

Then point any local MCP host at `.venv/bin/streamdeck-mcp` (or at the
upstream PyPI release if you don't need the HTTP service).

## Quick start — HTTP service

```bash
git clone <this repo>
python3.11 -m venv .venv
.venv/bin/pip install -e .

# bearer token (keep it secret; chmod 600)
openssl rand -hex 24 > .token
chmod 600 .token

.venv/bin/streamdeck-mcp-service   # listens on 127.0.0.1:18802 by default
```

MCP client config (Streamable HTTP):

```json
{
  "mcpServers": {
    "streamdeck": {
      "type": "http",
      "url": "http://127.0.0.1:18802/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

For a remote agent on your private/tailnet network, set `DECK_MCP_BIND` to
a private interface address. The service enforces DNS-rebinding protection
(it only accepts requests whose `Host` matches the bind address or
loopback), so it is not internet-facing by default; put a
TLS-terminating reverse proxy in front of it if the path between client
and deck crosses an untrusted network.

Smoke-test a running service (read-only):

```bash
.venv/bin/python examples/test_client.py http://127.0.0.1:18802/mcp .token
```

On macOS, run it as a launchd service with `examples/launchd.plist.tpl`
(the template comments cover install, stop, and log locations).

### Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `DECK_MCP_BIND` | `127.0.0.1:18802` | `host:port` the HTTP service listens on |
| `DECK_MCP_TOKEN_FILE` | `./.token` | bearer token file; re-read per request, so rotation needs no restart |
| `DECK_MCP_PROFILES_DIR` | auto (`ProfilesV3`, then `ProfilesV2`, under the Elgato app's application-support dir) | override the profiles directory |
| `DECK_MCP_SCRIPTS_DIR` | `~/StreamDeckScripts` | where `streamdeck_create_action` writes scripts |
| `DECK_MCP_ICONS_DIR` | `~/.streamdeck-mcp/generated-icons` | where `streamdeck_create_icon(s)` writes PNGs |
| `DECK_MCP_LOG` | — (warnings to stderr) | JSONL audit file, one line per tool call |

## Tools

| Tool | What it does |
| --- | --- |
| `streamdeck_find_actions` | search installed plugins/actions by text, plugin, controller, or profile usage; each result carries a paste-ready native `action` object |
| `streamdeck_read_profiles` | list profiles (V3/V2), pages, current/default page, device model |
| `streamdeck_read_page` | read one page, button by button, each with its raw native action object |
| `streamdeck_write_page` | create or replace a page manifest; refuses while the app is running unless `auto_quit_app=true` |
| `streamdeck_create_icon` | render one PNG icon from a Material Design Icons glyph and/or text |
| `streamdeck_create_icons` | batch-render icons |
| `streamdeck_create_action` | wrap a shell command in a script-backed `Open` action |
| `streamdeck_read_plugins` | installed plugins with action/settings schemas (for synthesizing actions) |
| `streamdeck_restart_app` | stop + relaunch the Elgato app (macOS) to pick up manifest edits |
| `streamdeck_install_mcp_plugin` | install the bundled in-app `.sdPlugin` |
| `deck_status` *(HTTP only)* | app running? profile inventory? uptime? last tool call? |

### Authoring a key, in preference order

1. **Paste a native action** — copy the `action` object from
   `streamdeck_find_actions`, or a `streamdeck_read_page` button's `raw`
   field, into `streamdeck_write_page`. Best for installed plugins whose
   settings schema is opaque.
2. **Synthesize** — from `streamdeck_read_plugins`: `plugin_uuid` +
   `action_uuid` + `settings`.
3. **Script-backed** — `streamdeck_create_action` writes a shell script
   and returns an `Open` action; the key then runs your command.

## Security model

- The HTTP endpoint is **bearer-token gated**; the token file is re-read on
  every request and compared in constant time, so rotation needs no
  restart.
- `GET /health` is intentionally unauthenticated and returns no profile
  data (liveness only).
- DNS-rebinding protection: only requests whose `Host` header matches the
  bind host or loopback are accepted; anything else is dropped
  fail-closed (the connection is closed without a response).
- The service is not meant to be exposed to the public internet; keep it
  on a private/tailnet interface, behind TLS if the path is untrusted. No
  telemetry, no outbound connections.

## Troubleshooting

| Symptom | Meaning / fix |
| --- | --- |
| `⚠️ ... app is running ...` | the Elgato app holds the manifests in memory; close it (or pass `auto_quit_app=true`), write, then `streamdeck_restart_app` |
| `❌ ... profile not found` | check `DECK_MCP_PROFILES_DIR` / that the profiles directory exists on the deck's machine |
| connection closed with no response | the client's `Host` header is not the bind address or loopback (DNS-rebinding protection) — point the client at the actual bind host:port, not a different alias |
| keys look stale after a write | the app rewrote the manifests from memory on quit; redo the cycle with the app stopped |

## Development

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .[dev]
.venv/bin/pytest            # profile engine, stdio server, HTTP service
.venv/bin/ruff check .
```

Tests run on any platform: the profile engine is pure file manipulation
and the Stream Deck app is stubbed out.

## Layout

```
profile_manager.py   the profile engine (V3/V2 manifests, pages, actions,
                     icons, app control)
profile_server.py    stdio MCP server (9 tools)
server.py            legacy USB/streamdeck-python device server (optional)
http_service.py      Streamable HTTP service (10 tools, bearer-token gated)
install_skill.py     installs the bundled streamdeck-designer skill
streamdeck_assets/   Material Design Icons (Apache-2.0) + designer skill
streamdeck_plugin/   bundled in-app .sdPlugin
tests/               pytest suite (synthetic profile fixtures)
examples/            launchd template, env template, smoke client
```

## Attribution

Forked from [verygoodplugins/streamdeck-mcp](https://github.com/verygoodplugins/streamdeck-mcp)
(MIT, © Jack Arturo / Very Good Plugins). The HTTP service, `deck_status`,
the examples and the fork packaging are © Hugo Soares. The bundled
Material Design Icons font is Apache-2.0 (see
`streamdeck_assets/MDI-LICENSE`).
