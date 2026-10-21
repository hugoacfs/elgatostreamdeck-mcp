#!/usr/bin/env python3
"""streamdeck-mcp service — the deck tools over authenticated Streamable HTTP.

The stdio server (profile_server.py) is designed to be launched by an MCP
client on the same machine (``streamdeck-mcp`` / ``uvx streamdeck-mcp``).
This module exposes the *same* tool surface over MCP Streamable HTTP behind
a bearer-token gate, for agents that run elsewhere — another computer, a
remote assistant, a CI runner — and need to drive a local Stream Deck.

Runtime configuration (environment variables, e.g. from your service unit):

  DECK_MCP_BIND          "<host>:<port>" to listen on.
                         Default: 127.0.0.1:18802
  DECK_MCP_TOKEN_FILE    Bearer token file. Default: ./.token (working dir).
                         Create it with e.g. ``openssl rand -hex 24`` and
                         ``chmod 600``. The file is re-read on every
                         request, so rotating the token takes effect without
                         a restart.
  DECK_MCP_PROFILES_DIR  Override the Elgato profiles directory
                         (default: auto-detect ProfilesV3, then ProfilesV2).
  DECK_MCP_SCRIPTS_DIR   Directory for scripts created by
                         ``streamdeck_create_action``
                         (default: ~/StreamDeckScripts).
  DECK_MCP_ICONS_DIR     Directory for icons created by
                         ``streamdeck_create_icon(s)``
                         (default: ~/.streamdeck-mcp/generated-icons).
  DECK_MCP_LOG           Optional JSONL audit file: one line per tool call
                         (timestamp, tool, ok/error, duration).

Endpoints:

  GET /health   Unauthenticated liveness probe:
                {"ok": true, "service": "streamdeck-mcp", "version": "...",
                 "app_running": <bool>, "uptime_s": <float>}
  /mcp          MCP Streamable HTTP, bearer-token gated.

Security model: no data leaves the machine except the MCP traffic you
initiate. Bind to loopback or a private-network interface, keep the token
file at 0600, and put a TLS-terminating reverse proxy in front of the
service if it must cross an untrusted network.

This file is part of the elgatostreamdeck-mcp fork (MIT, Hugo Soares). The
profile engine, tool semantics and error style are from
verygoodplugins/streamdeck-mcp (MIT, Jack Arturo / Very Good Plugins).
"""

from __future__ import annotations

import hmac
import importlib.metadata
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from profile_manager import (
    DEFAULT_BG_COLOR,
    DEFAULT_TEXT_COLOR,
    PageNotFoundError,
    ProfileManager,
    ProfileManagerError,
    ProfileNotFoundError,
    ProfileValidationError,
    StreamDeckAppRunningError,
    is_stream_deck_app_running,
)

logger = logging.getLogger("streamdeck-mcp.service")

SERVICE_NAME = "streamdeck-mcp"
DEFAULT_BIND = "127.0.0.1:18802"
DEFAULT_PORT = int(DEFAULT_BIND.rsplit(":", 1)[1])

STARTED_AT = time.time()

# Last tool call, surfaced by deck_status.
LAST_CALL: dict[str, Any] = {
    "tool": None,
    "at": None,
    "ok": None,
    "duration_s": None,
    "error": None,
}


def version() -> str:
    """Package version (falls back to a constant outside an install)."""
    try:
        return importlib.metadata.version("streamdeck-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "0.4.0"


def build_manager(
    profiles_dir: Path | None = None,
    scripts_dir: Path | None = None,
    generated_icons_dir: Path | None = None,
) -> ProfileManager:
    """A ProfileManager honouring the service's env-var overrides."""
    if profiles_dir is None:
        raw = os.environ.get("DECK_MCP_PROFILES_DIR")
        profiles_dir = Path(raw).expanduser() if raw else None
    if scripts_dir is None:
        raw = os.environ.get("DECK_MCP_SCRIPTS_DIR")
        scripts_dir = Path(raw).expanduser() if raw else None
    if generated_icons_dir is None:
        raw = os.environ.get("DECK_MCP_ICONS_DIR")
        generated_icons_dir = Path(raw).expanduser() if raw else None
    return ProfileManager(
        profiles_dir=profiles_dir,
        scripts_dir=scripts_dir,
        generated_icons_dir=generated_icons_dir,
    )


def _dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2)


def _audit(tool: str, duration: float, ok: bool, err: str | None) -> None:
    """Per-call audit: JSONL file if DECK_MCP_LOG is set, else a warning log
    line on failure only."""
    path = os.environ.get("DECK_MCP_LOG")
    if not path:
        if not ok:
            logger.warning("call %s failed: %s", tool, err)
        return
    line = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool": tool,
        "ok": ok,
        "duration_s": round(duration, 3),
        "error": err,
    }
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        logger.warning("could not write audit line to %s", path)


def _call(name: str, fn: Callable[[], Any]) -> str:
    """Run a manager call with the stdio server's error semantics, and keep
    the audit trail + LAST_CALL bookkeeping.

    Domain errors come back as the same readable text profile_server.py
    returns (⚠️ for "app is running", ❌ for validation/lookup failures),
    never as a raw stack trace.
    """
    started = time.time()
    err: str | None = None
    try:
        result = fn()
        return _dumps(result) if not isinstance(result, str) else result
    except StreamDeckAppRunningError as exc:
        err = str(exc)
        return f"⚠️ {exc}"
    except (
        ProfileManagerError,
        ProfileNotFoundError,
        PageNotFoundError,
        ProfileValidationError,
    ) as exc:
        err = str(exc)
        return f"❌ {exc}"
    except Exception as exc:
        logger.exception("unexpected error in %s", name)
        return f"❌ Unexpected error: {exc}"
    finally:
        LAST_CALL.update(
            tool=name,
            at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            ok=err is None,
            duration_s=round(time.time() - started, 3),
            error=err,
        )
        _audit(name, time.time() - started, err is None, err)


def build_server(
    profiles_dir: Path | None = None,
    scripts_dir: Path | None = None,
    generated_icons_dir: Path | None = None,
) -> tuple[FastMCP, dict[str, Callable[..., str]]]:
    """The deck tools on a FastMCP server.

    Returns ``(server, handlers)``; ``handlers`` maps tool name to the
    plain Python function (tests call these directly; the server is what
    main() serves). Env-var overrides (see module docstring) configure the
    manager unless explicit paths are given.
    """
    manager = build_manager(profiles_dir, scripts_dir, generated_icons_dir)
    mcp = FastMCP("deck")
    handlers: dict[str, Callable[..., str]] = {}

    def streamdeck_read_profiles() -> str:
        """List Stream Deck desktop profiles from the active ProfilesV3 (or
        ProfilesV2) directory: profile id, name, device model, pages (with
        which is current/default), and page counts."""
        return _call("streamdeck_read_profiles", lambda: manager.list_profiles())

    def streamdeck_read_page(
        profile_name: str | None = None,
        profile_id: str | None = None,
        page_index: int | None = None,
        directory_id: str | None = None,
    ) -> str:
        """Read one profile page, button by button (title, icon, plugin,
        settings). Each button carries a 'raw' native action object — copy
        it into streamdeck_write_page's button 'action' field to reuse a
        fully configured plugin action without re-deriving its settings.
        For searching across many pages, prefer streamdeck_find_actions."""
        return _call(
            "streamdeck_read_page",
            lambda: manager.read_page(
                profile_name=profile_name,
                profile_id=profile_id,
                page_index=page_index,
                directory_id=directory_id,
            ),
        )

    def streamdeck_write_page(
        profile_name: str | None = None,
        profile_id: str | None = None,
        page_index: int | None = None,
        directory_id: str | None = None,
        page_name: str | None = None,
        buttons: list[dict[str, Any]] | None = None,
        clear_existing: bool = True,
        create_new: bool = False,
        make_current: bool = False,
        auto_quit_app: bool = False,
    ) -> str:
        """Create a new page, or replace/update an existing Stream Deck page
        manifest. Authoring preference: (1) paste a native 'action' object
        from streamdeck_find_actions or a streamdeck_read_page button's
        'raw' field — best for installed plugins with private settings;
        (2) synthesize plugin_uuid + action_uuid + settings from
        streamdeck_read_plugins; (3) a script-backed Open action from
        streamdeck_create_action. Each button: 'position' ('col,row' on the
        keypad, or a key index), 'title', 'icon' or 'icon_path', 'action',
        and optional 'states'/'settings'. IMPORTANT: the Elgato desktop app
        overwrites profile manifests from its in-memory state when it quits,
        so this tool refuses to write while the app is running unless
        auto_quit_app=true. Back up the profile directory first, and call
        streamdeck_restart_app once your edits are complete."""
        return _call(
            "streamdeck_write_page",
            lambda: manager.write_page(
                profile_name=profile_name,
                profile_id=profile_id,
                page_index=page_index,
                directory_id=directory_id,
                page_name=page_name,
                buttons=buttons,
                clear_existing=clear_existing,
                create_new=create_new,
                make_current=make_current,
                auto_quit_app=auto_quit_app,
            ),
        )

    def streamdeck_find_actions(
        query: str | None = None,
        plugin_uuid: str | None = None,
        action_uuid: str | None = None,
        plugin_name: str | None = None,
        controller: str | None = None,
        profile_name: str | None = None,
        profile_id: str | None = None,
        limit: int = 50,
    ) -> str:
        """Search installed Stream Deck plugins and actions by text
        (matches plugin/action names), with optional narrowing: plugin_uuid,
        action_uuid, plugin_name, controller (keypad/key/button/encoder/
        dial), or usage inside one profile. Each result includes a native
        'action' object, paste-ready for streamdeck_write_page."""
        return _call(
            "streamdeck_find_actions",
            lambda: manager.find_actions(
                query=query,
                plugin_uuid=plugin_uuid,
                action_uuid=action_uuid,
                plugin_name=plugin_name,
                controller=controller,
                profile_name=profile_name,
                profile_id=profile_id,
                limit=limit,
            ),
        )

    def streamdeck_read_plugins(
        plugin_id: str | None = None,
        include_raw_manifest: bool = False,
        include_settings_schema: bool = True,
    ) -> str:
        """List installed Stream Deck plugins and their actions, including
        settings schemas. Use to synthesize a button action
        (plugin_uuid + action_uuid + settings) when there is no existing
        raw action to copy."""
        return _call(
            "streamdeck_read_plugins",
            lambda: manager.list_plugins(
                plugin_id=plugin_id,
                include_raw_manifest=include_raw_manifest,
                include_settings_schema=include_settings_schema,
            ),
        )

    def streamdeck_create_icon(
        text: str | None = None,
        icon: str | None = None,
        icon_color: str | None = None,
        icon_scale: float = 1.0,
        bg_color: str = DEFAULT_BG_COLOR,
        text_color: str = DEFAULT_TEXT_COLOR,
        font_size: int = 18,
        filename: str | None = None,
        shape: str = "button",
        transparent_bg: bool = False,
    ) -> str:
        """Render one Stream Deck icon: either a Material Design Icons
        glyph (e.g. 'mdi:cpu-64-bit'; a few thousand bundled) or centered
        text — not both — with colours, scale and shape ('button' or
        'touchstrip'). Returns {path, ...}; use path as a button's
        icon_path. For a labelled key, render the glyph and set the
        button's title instead of baking in text."""
        return _call(
            "streamdeck_create_icon",
            lambda: manager.create_icon(
                text=text,
                icon=icon,
                icon_color=icon_color,
                icon_scale=icon_scale,
                bg_color=bg_color,
                text_color=text_color,
                font_size=font_size,
                filename=filename,
                shape=shape,
                transparent_bg=transparent_bg,
            ),
        )

    def streamdeck_create_icons(specs: list[dict[str, Any]]) -> str:
        """Batch-render icons. Each spec has the same fields as
        streamdeck_create_icon (text, icon, colours, font_size, filename,
        shape, transparent_bg). Returns {'icons': [per-spec result]}; a
        failed spec carries an 'error' key and the rest still render."""
        return _call("streamdeck_create_icons", lambda: manager.create_icons(specs))

    def streamdeck_create_action(
        name: str,
        command: str,
        working_directory: str | None = None,
        filename: str | None = None,
    ) -> str:
        """Wrap a shell command in a script-backed 'Open' action: writes
        the script into the scripts directory and returns a ready-to-paste
        action block for streamdeck_write_page. macOS only."""
        return _call(
            "streamdeck_create_action",
            lambda: manager.create_action(
                name=name,
                command=command,
                working_directory=working_directory,
                filename=filename,
            ),
        )

    def streamdeck_restart_app() -> str:
        """Stop and relaunch the Elgato Stream Deck desktop app (macOS) so
        manifest edits take effect. Call once after a batch of writes."""
        return _call("streamdeck_restart_app", manager.restart_app)

    def streamdeck_install_mcp_plugin(force: bool = False) -> str:
        """Install the bundled in-app Stream Deck MCP plugin (.sdPlugin)
        into the user's Stream Deck plugins directory."""
        return _call(
            "streamdeck_install_mcp_plugin",
            lambda: manager.install_mcp_plugin(force=force),
        )

    def deck_status() -> str:
        """One-call health verdict: whether the Elgato desktop app is
        running, the profile inventory (id, name, device model, page
        counts, current page), this service's uptime, and the last tool
        call it handled. Call this first after (re)connecting."""
        return _call(
            "deck_status",
            lambda: {
                "ok": True,
                "service": SERVICE_NAME,
                "version": version(),
                "app_running": is_stream_deck_app_running(),
                "uptime_s": round(time.time() - STARTED_AT, 1),
                "last_call": LAST_CALL,
                "profiles": [
                    {
                        "profile_id": p["profile_id"],
                        "name": p["name"],
                        "device_model": (p.get("device") or {}).get("Model"),
                        "device_model_name": (p.get("device") or {}).get("ModelName"),
                        "page_count": p.get("page_count"),
                        "current_page": p.get("current_page_uuid"),
                    }
                    for p in manager.list_profiles()
                ],
            },
        )

    for tool in (
        streamdeck_read_profiles,
        streamdeck_read_page,
        streamdeck_write_page,
        streamdeck_find_actions,
        streamdeck_read_plugins,
        streamdeck_create_icon,
        streamdeck_create_icons,
        streamdeck_create_action,
        streamdeck_restart_app,
        streamdeck_install_mcp_plugin,
        deck_status,
    ):
        mcp.tool()(tool)
        handlers[tool.__name__] = tool

    return mcp, handlers


# --- transport layer -----------------------------------------------------


def _read_token(token_file: Path) -> str | None:
    """Read the bearer token from disk. The file is re-read on every
    request, so rotating the token takes effect without a restart."""
    try:
        return token_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _safe_app_running() -> bool:
    try:
        return is_stream_deck_app_running()
    except Exception:
        return False


def _health_body() -> bytes:
    payload = {
        "ok": True,
        "service": SERVICE_NAME,
        "version": version(),
        "app_running": _safe_app_running(),
        "uptime_s": round(time.time() - STARTED_AT, 1),
    }
    return json.dumps(payload).encode("utf-8")


async def _send_json(send: Callable, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build_auth_app(mcp_app: Any, token_file: Path) -> Any:
    """ASGI wrapper: GET /health is open (liveness only, no profile data);
    every other path requires a valid bearer token before reaching the
    FastMCP streamable-HTTP app. Constant-time comparison; the token file
    is re-read per request."""

    async def app(scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await mcp_app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")

        if method == "GET" and path == "/health":
            await _send_json(send, 200, _health_body())
            return

        headers = {k: v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode("latin-1")
        expected = _read_token(token_file)
        provided = auth[len("Bearer ") :] if auth.startswith("Bearer ") else None
        if not expected or provided is None or not hmac.compare_digest(provided, expected):
            await _send_json(send, 401, b'{"error": "unauthorized"}')
            return

        await mcp_app(scope, receive, send)

    return app


def main() -> None:
    """Entry point: streamdeck-mcp-service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    bind = os.environ.get("DECK_MCP_BIND", DEFAULT_BIND)
    host, _, port = bind.partition(":")
    port = int(port or DEFAULT_PORT)
    token_file = Path(os.environ.get("DECK_MCP_TOKEN_FILE", ".token")).expanduser()

    mcp, _handlers = build_server()

    # The SDK's default allowed_hosts list is loopback-only: with a
    # non-loopback bind every request fails the Host check with HTTP 421
    # unless the bind host is on the allowlist.
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            f"{host}:*",
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ],
    )

    app = build_auth_app(mcp.streamable_http_app(), token_file)
    logger.info(
        "%s service listening on %s:%s (token file: %s)",
        SERVICE_NAME,
        host,
        port,
        token_file,
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
