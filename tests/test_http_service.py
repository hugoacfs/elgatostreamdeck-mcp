"""Tests for the Streamable HTTP service layer.

Covers the bearer-token gate (missing file / missing token / wrong token /
valid token), the unauthenticated /health endpoint, and the tool wiring
against the synthetic ProfilesV3 fixture. The wire-level Streamable HTTP
handshake itself is the official MCP SDK's; a live handshake is covered by
examples/test_client.py against a running service.
"""

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import http_service
from http_service import SERVICE_NAME, build_auth_app, build_server

# pytest fixture, resolved by name in this module
from tests.test_profile_manager import sample_profiles_v3  # noqa: F401

VALID_BUTTON = {"key": 0, "action_type": "next_page", "title": "Next"}


@pytest.fixture(autouse=True)
def _stub_stream_deck_app_not_running():
    with patch("profile_manager.is_stream_deck_app_running", return_value=False):
        yield


@pytest.fixture(autouse=True)
def _reset_last_call():
    http_service.LAST_CALL.update(
        tool=None, at=None, ok=None, duration_s=None, error=None
    )
    yield


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    p = tmp_path / ".token"
    p.write_text("test-token-0123456789abcdef\n")
    return p


class _DummyApp:
    """Stands in for the FastMCP streamable-HTTP ASGI app: anything that
    reaches it means the token check passed."""

    async def __call__(self, scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"2")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})


@pytest.fixture
def http_app(token_file: Path) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=build_auth_app(_DummyApp(), token_file))


@pytest.fixture
def fixture_server(sample_profiles_v3: Path, tmp_path: Path):  # noqa: F811
    _, handlers = build_server(
        profiles_dir=sample_profiles_v3,
        scripts_dir=tmp_path / "scripts",
        generated_icons_dir=tmp_path / "icons",
    )
    return handlers


def _load(handlers, tool, **kwargs):
    return json.loads(handlers[tool](**kwargs))


# --- auth gate -----------------------------------------------------------


async def test_health_is_open_and_reveals_nothing_sensitive(http_app):
    async with httpx.AsyncClient(
        transport=http_app, base_url="http://127.0.0.1:18802"
    ) as client:
        r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == SERVICE_NAME
    assert body["version"]
    assert isinstance(body["app_running"], bool)
    assert isinstance(body["uptime_s"], float)
    for sensitive in ("token", "token_file", "host", "port", "ip", "address"):
        assert sensitive not in body


async def test_health_reflects_app_state(http_app, monkeypatch):
    monkeypatch.setattr(http_service, "is_stream_deck_app_running", lambda: True)
    async with httpx.AsyncClient(
        transport=http_app, base_url="http://127.0.0.1:18802"
    ) as client:
        body = (await client.get("/health")).json()
    assert body["app_running"] is True


async def test_mcp_path_requires_valid_token(http_app, token_file):
    async with httpx.AsyncClient(
        transport=http_app, base_url="http://127.0.0.1:18802"
    ) as client:
        assert (await client.post("/mcp", json={})).status_code == 401
        assert (
            await client.post(
                "/mcp", headers={"Authorization": "Bearer wrong"}, json={}
            )
        ).status_code == 401
        good = f"Bearer {token_file.read_text().strip()}"
        r = await client.post("/mcp", headers={"Authorization": good}, json={})
        assert r.status_code == 200
        assert r.text == "ok"


async def test_missing_token_file_denies_mcp_but_serves_health(tmp_path: Path):
    transport = httpx.ASGITransport(
        app=build_auth_app(_DummyApp(), tmp_path / "nope.token")
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:18802"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.post("/mcp", json={})).status_code == 401


# --- tool wiring ---------------------------------------------------------


def test_read_profiles(fixture_server):
    data = _load(fixture_server, "streamdeck_read_profiles")
    assert isinstance(data, list)
    assert len(data) == 1
    p = data[0]
    assert p["name"] == "Default Profile"
    assert p["profile_id"] == "PROFILE-ONE"
    assert p["version"] == "3.0"
    assert p["device"]["Model"] == "20GBA9901"
    assert p["page_count"] == 3


def test_read_page_returns_buttons_with_raw_action(fixture_server):
    data = _load(
        fixture_server,
        "streamdeck_read_page",
        profile_name="Default Profile",
        page_index=1,
    )
    assert data["page"]["name"] == "Work"
    assert data["buttons"][0]["key"] == 0
    assert data["buttons"][0]["title"] == "Deploy"
    assert data["buttons"][0]["plugin_uuid"] == "com.elgato.streamdeck.system.open"
    assert data["buttons"][0]["raw"]["UUID"] == "com.elgato.streamdeck.system.open"


def test_write_page_creates_new_page(fixture_server):
    data = _load(
        fixture_server,
        "streamdeck_write_page",
        profile_id="PROFILE-ONE",
        create_new=True,
        page_name="Fork Test",
        make_current=True,
        buttons=[VALID_BUTTON],
    )
    assert data["created"] is True
    assert data["page_uuid"]
    profiles = _load(fixture_server, "streamdeck_read_profiles")
    assert profiles[0]["page_count"] == 4
    assert profiles[0]["current_page_uuid"] == data["page_uuid"]


def test_write_page_refuses_while_app_running(fixture_server, monkeypatch):
    monkeypatch.setattr(
        "profile_manager.is_stream_deck_app_running", lambda: True
    )
    out = fixture_server["streamdeck_write_page"](
        profile_id="PROFILE-ONE",
        create_new=True,
        page_name="Nope",
        buttons=[VALID_BUTTON],
    )
    assert out.startswith("⚠️")


def test_write_page_unknown_profile_is_domain_error(fixture_server):
    out = fixture_server["streamdeck_write_page"](
        profile_name="No Such Profile",
        buttons=[VALID_BUTTON],
    )
    assert out.startswith("❌")


def test_create_icon_writes_file(fixture_server):
    data = _load(fixture_server, "streamdeck_create_icon", icon="mdi:cpu-64-bit")
    path = Path(data["path"])
    assert path.exists()
    assert path.stat().st_size > 0


def test_create_icon_rejects_text_and_icon_together(fixture_server):
    out = fixture_server["streamdeck_create_icon"](text="Hi", icon="mdi:cpu-64-bit")
    assert out.startswith("❌")


def test_create_action_writes_script_and_action(fixture_server):
    data = _load(
        fixture_server,
        "streamdeck_create_action",
        name="hello",
        command="echo hello",
    )
    script = Path(data["script_path"])
    assert script.exists()
    assert script.stat().st_mode & 0o111 == 0o111
    assert "echo hello" in script.read_text()
    assert data["action"]["UUID"] == "com.elgato.streamdeck.system.open"
    assert json.loads(data["action"]["Settings"]["path"]) == str(script)


def test_deck_status(fixture_server):
    data = _load(fixture_server, "deck_status")
    assert data["ok"] is True
    assert data["service"] == SERVICE_NAME
    assert data["app_running"] is False
    assert isinstance(data["uptime_s"], float)
    assert data["last_call"]["tool"] is None
    assert len(data["profiles"]) == 1
    assert data["profiles"][0]["name"] == "Default Profile"
    assert data["profiles"][0]["device_model"] == "20GBA9901"
    assert data["profiles"][0]["page_count"] == 3


def test_deck_status_reports_previous_call(fixture_server):
    out = fixture_server["streamdeck_read_page"](profile_name="Nope")
    assert out.startswith("❌")
    data = _load(fixture_server, "deck_status")
    assert data["last_call"]["tool"] == "streamdeck_read_page"
    assert data["last_call"]["ok"] is False
    assert data["last_call"]["error"]
