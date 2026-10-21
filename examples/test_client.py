#!/usr/bin/env python3
"""End-to-end smoke client for the streamdeck-mcp HTTP service.

Uses the official MCP Python SDK client — the same protocol stack a remote
agent uses. Read-only: it initializes, lists tools and calls deck_status
and streamdeck_read_profiles; it never writes a page.

Usage:
  test_client.py [url] [token_file]
    url         default http://127.0.0.1:18802/mcp
    token_file  default ./.token

Exit 0 = ALL CHECKS PASSED; 1 = failures listed.
"""

from __future__ import annotations

import asyncio
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = (
    "streamdeck_find_actions",
    "streamdeck_read_profiles",
    "streamdeck_read_page",
    "streamdeck_write_page",
    "streamdeck_create_icon",
    "streamdeck_create_icons",
    "streamdeck_create_action",
    "streamdeck_read_plugins",
    "streamdeck_restart_app",
    "streamdeck_install_mcp_plugin",
    "deck_status",
)


async def main(url: str, token_file: str) -> int:
    with open(token_file, encoding="utf-8") as f:
        token = f.read().strip()
    failures: list[str] = []

    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    async with streamable_http_client(url, http_client=http) as (read, write, _):
        async with ClientSession(read, write) as s:
            info = await s.initialize()
            print("server:", info.serverInfo)

            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools:", names)
            for want in EXPECTED_TOOLS:
                if want not in names:
                    failures.append(f"missing tool: {want}")

            r = await s.call_tool("deck_status", {})
            print("--- deck_status ---")
            print(r.content[0].text)

            r = await s.call_tool("streamdeck_read_profiles", {})
            print("--- streamdeck_read_profiles (first 800 chars) ---")
            print(r.content[0].text[:800])

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18802/mcp"
    tok = sys.argv[2] if len(sys.argv) > 2 else ".token"
    raise SystemExit(asyncio.run(main(url, tok)))
