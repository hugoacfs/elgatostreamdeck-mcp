<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  launchd template for the streamdeck-mcp HTTP service (macOS).

  1. Replace every {{...}} placeholder (sed or by hand). Make the Label
     unique to your machine (com.<yourname>.streamdeck-mcp).
  2. Install:
       cp launchd.plist.tpl ~/Library/LaunchAgents/com.example.streamdeck-mcp.plist
  3. Load:
       mkdir -p {{REPO_DIR}}/logs
       launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.streamdeck-mcp.plist
     Stop:
       launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.example.streamdeck-mcp.plist
  4. Logs:
       tail -f {{REPO_DIR}}/logs/stderr.log
  5. Smoke-test:
       .venv/bin/python examples/test_client.py http://{{BIND_IP}}:18802/mcp {{REPO_DIR}}/.token
-->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.streamdeck-mcp</string>

    <key>ProgramArguments</key>
    <array>
        <string>{{PYTHON}}</string>
        <string>{{REPO_DIR}}/http_service.py</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>DECK_MCP_BIND</key>
        <string>{{BIND_IP}}:18802</string>
        <key>DECK_MCP_TOKEN_FILE</key>
        <string>{{REPO_DIR}}/.token</string>
        <key>HOME</key>
        <string>{{HOME}}</string>
        <key>PATH</key>
        <string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>{{REPO_DIR}}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{{REPO_DIR}}/logs/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>{{REPO_DIR}}/logs/stderr.log</string>
</dict>
</plist>
