# WeClaude — Agent Setup Guide

> This file is written for a coding agent (Claude Code, Cursor, Codex etc.) to read and execute on behalf of the user. The user only provides 4 variables and a QR scan.

## What this is

WeClaude bridges a WeChat ClawBot to a local Claude Code CLI: user chats with their bot in WeChat, the bot relays to a `claude -p` subprocess in their workdir, replies come back as WeChat messages. Supports text/image/voice inbound, text + buddy-comment outbound.

## Prerequisites the user must already have

1. **macOS** (Linux/Windows: skip step 4, supply your own service manager)
2. **Python 3.11+**
3. **Claude Code CLI** installed; has been run at least once at the target workdir (so `~/.claude/projects/<escaped-cwd>/*.jsonl` exists)
4. **WeChat on phone** + a ClawBot contact to chat with

## Variables the agent collects from the user

| Var | Required | Default | Notes |
|---|---|---|---|
| `WECLAUDE_WORKDIR` | yes | — | absolute path of the user's CC project (drives session lookup) |
| `WECLAUDE_TZ` | no | `Australia/Melbourne` | e.g. `Asia/Shanghai`, `America/New_York` |
| `WECLAUDE_BUDDY_LABEL` | no | `铁锅` | empty string disables buddy bubble; set if you have a different companion |
| `WECLAUDE_DEBUG` | no | unset | `"1"` enables full item dump in logs |

## Steps the agent runs unattended

### 1. Clone & venv

```bash
git clone https://github.com/Jaynechu/WeClaude.git ~/WeClaude
cd ~/WeClaude
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/wechat-claude-bridge && chmod 700 ~/.config/wechat-claude-bridge
mkdir -p ~/Library/Logs
```

### 2. Verify Claude CLI ready

```bash
which claude || { echo "Install Claude Code first"; exit 1; }
ls ~/.claude/projects/ >/dev/null || { echo "Run \`claude\` once at the target workdir"; exit 1; }
```

### 3. First-run QR login (interactive)

User runs in their terminal (agent cannot scan QR for them):

```bash
cd ~/WeClaude && .venv/bin/python bridge.py --workdir "$WECLAUDE_WORKDIR"
```

A QR prints in terminal → user scans with WeChat. Send a test WeChat message to the ClawBot to confirm Claude replies. Ctrl-C when verified.

### 4. Generate launchd plist (auto-start on boot)

```bash
cd ~/WeClaude
sed \
  -e "s|__INSTALL_DIR__|$HOME/WeClaude|g" \
  -e "s|__USER_HOME__|$HOME|g" \
  -e "s|__WORKDIR__|$WECLAUDE_WORKDIR|g" \
  com.weclaude.bridge.plist.template \
  > ~/Library/LaunchAgents/com.weclaude.bridge.plist
```

If user customized env vars, agent inserts inside the existing `<key>EnvironmentVariables</key><dict>` block:

```xml
<key>WECLAUDE_TZ</key>
<string>Asia/Shanghai</string>
<key>WECLAUDE_BUDDY_LABEL</key>
<string></string>
```

### 5. Load & verify

```bash
launchctl load ~/Library/LaunchAgents/com.weclaude.bridge.plist
sleep 5
tail -n 20 ~/Library/Logs/weclaude.err.log
```

Expect lines like `HTTP/1.1 200 OK` from polling. If 401/403 → token expired, repeat step 3.

## Updating later

```bash
cd ~/WeClaude
git pull origin main
.venv/bin/pip install -r requirements.txt --upgrade
launchctl unload ~/Library/LaunchAgents/com.weclaude.bridge.plist
launchctl load ~/Library/LaunchAgents/com.weclaude.bridge.plist
```

## Slash commands (in WeChat)

- `/sessions` or `/ss` — list past Claude sessions
- `/use N` — switch to session N from the last listing
- `/reset` or `/clear` — drop current session binding
- `/help` — full command list

## Caveats

- **`--permission-mode acceptEdits`** in `bridge.py`: Claude auto-approves edits within the workdir. Don't point at sensitive trees.
- **Polling architecture**: if host sleeps, message delivery pauses. Disable Sleep on bridge host or expect gaps.
- **Tests**: `cd ~/WeClaude && .venv/bin/python test_debounce.py` (script form, not pytest).
- **Logs**: `~/Library/Logs/weclaude.{out,err}.log`. INFO = normal; set `WECLAUDE_DEBUG=1` for verbose item dump.
- **Memory dir**: `~/.config/wechat-claude-bridge/memory/MEMORY.md` — bridge-specific memory injected per message. Edit to teach the bot about you.
