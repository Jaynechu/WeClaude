#!/usr/bin/env python3
"""WeChat-Claude Code Bridge.

Bridges WeChat ClawBot messages to Claude Code and other AI agent CLIs.
No OpenClaw needed — directly uses iLink API.

Usage:
    python bridge.py              # Login and start bridge (default: claude)
    python bridge.py -w /path     # Set working directory
    python bridge.py --logout     # Clear credentials
"""

import argparse
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ilink_client import ILinkClient
from memory_store import MemoryStore
from scheduler import Scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "wechat-claude-bridge"
SESSION_FILE = CONFIG_DIR / "sessions.json"
MEDIA_DIR = CONFIG_DIR / "media"
PERSONA_FILE = CONFIG_DIR / "persona.json"

MAX_LEN = 200
TZ = os.environ.get("WECLAUDE_TZ", "Australia/Melbourne")
BUDDY_LABEL = os.environ.get("WECLAUDE_BUDDY_LABEL", "铁锅")

_buddy_rule = (
    f'\n- {BUDDY_LABEL} buddy 注释（<!-- buddy: --> ）微信端独立成"{BUDDY_LABEL}：xxx"气泡，1-25 字，无 *动作*'
    if BUDDY_LABEL else ""
)
WECHAT_PROMPT = (
    "WeChat output rules:\n"
    "- 多段用空行（\\n\\n）分隔，bridge 拆成气泡，每次1-8段随机不固定\n"
    "- 每段以短句为主，复杂内容可长但每段 ≤ 200 字\n"
    "- 不用 markdown 列表/标题/粗体/分隔线，纯文本"
    + _buddy_rule + "\n"
)


def _now_string() -> str:
    """Inject current time so Claude knows it without running `date`."""
    return datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %a %H:%M")

# Per-user state
_sessions: dict[str, str] = {}  # user_id -> session_id
_user_agent: dict[str, str] = {}  # user_id -> agent_key
_sessions_lock = threading.Lock()
# Snapshot of the last /sessions listing shown to each user, so that
# /use <n> remains consistent even if session mtimes change in between.
_last_listed: dict[str, list[dict]] = {}
_last_listed_lock = threading.Lock()

# Runtime mutable working directory
_working_dir: str | None = None
_workdir_lock = threading.Lock()

_executor = ThreadPoolExecutor(max_workers=8)

# OpenClaw-inspired subsystems
_memory = MemoryStore()
_scheduler = Scheduler()
_personas: dict[str, str] = {}  # user_id -> persona string

# Input debounce: merge rapid messages into one call_agent
DEBOUNCE_S = 5
_msg_buffer: dict[str, list[dict]] = {}  # user_id -> [{text, images, ctx}, ...]
_msg_timer: dict[str, "threading.Timer"] = {}
_buffer_lock = threading.Lock()
_inflight: set[str] = set()


def _load_personas() -> None:
    try:
        _personas.update(json.loads(PERSONA_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _save_personas() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PERSONA_FILE.write_text(json.dumps(_personas, indent=2, ensure_ascii=False))
    try:
        os.chmod(PERSONA_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ── Agent Definitions ───────────────────────────────────────────

AGENTS: dict[str, dict] = {
    "claude": {
        "name": "Claude Code",
        "binary": "claude",
        "build_cmd": lambda msg, sid: _build_claude_cmd(msg, sid),
        "use_stdin": True,
        "parse_output": lambda stdout, uid: _parse_claude_output(stdout, uid),
    },
    "codex": {
        "name": "Codex CLI",
        "binary": "codex",
        "build_cmd": lambda msg, sid: ["codex", "-q", msg],
        "use_stdin": False,
        "parse_output": lambda stdout, uid: stdout.strip() or "[No response]",
    },
    "gemini": {
        "name": "Gemini CLI",
        "binary": "gemini",
        "build_cmd": lambda msg, sid: ["gemini", "-p", msg],
        "use_stdin": False,
        "parse_output": lambda stdout, uid: stdout.strip() or "[No response]",
    },
    "aider": {
        "name": "Aider",
        "binary": "aider",
        "build_cmd": lambda msg, sid: ["aider", "--message", msg, "--yes"],
        "use_stdin": False,
        "parse_output": lambda stdout, uid: stdout.strip() or "[No response]",
    },
}


def _build_claude_cmd(message: str, session_id: str | None) -> list[str]:
    """Build Claude Code CLI command."""
    cmd = ["claude", "-p", "--output-format", "json", "--permission-mode", "acceptEdits"]
    if session_id:
        cmd.extend(["--resume", session_id])
    return cmd


def _get_user_agent(user_id: str) -> str:
    return _user_agent.get(user_id, "claude")


def _find_binary(name: str) -> str | None:
    """Check if a CLI binary exists on PATH."""
    return shutil.which(name)


# ── Session Persistence ─────────────────────────────────────────


def _load_sessions() -> None:
    try:
        with _sessions_lock:
            _sessions.update(json.loads(SESSION_FILE.read_text()))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load sessions: %s", e)


def _save_sessions() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _sessions_lock:
        SESSION_FILE.write_text(json.dumps(_sessions, indent=2))
    try:
        os.chmod(SESSION_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ── Markdown → Plain Text ───────────────────────────────────────


def extract_buddy(text: str) -> tuple[str, str | None]:
    """Strip <!-- buddy: ... --> from main text, return (main, buddy_chunk)."""
    buddy_chunk: str | None = None

    def grab(m: "re.Match[str]") -> str:
        nonlocal buddy_chunk
        body = m.group(1).strip()
        body = re.sub(r"\*[^*]+\*", "", body)
        body = re.sub(r"\s+", " ", body).strip()
        if body:
            buddy_chunk = f"{BUDDY_LABEL}：{body}" if BUDDY_LABEL else body
        return ""

    main = re.sub(r"<!--\s*buddy:\s*(.+?)\s*-->", grab, text, flags=re.DOTALL)
    return main.strip(), buddy_chunk


def md_to_plain(text: str) -> str:
    """Convert markdown to WeChat-friendly plain text."""
    # Strip any leftover HTML comments (buddy already extracted upstream).
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"```\w*\n?", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*_]{3,}\s*$", "--------", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_msg(text: str, max_len: int = MAX_LEN) -> list[str]:
    """Split into bubbles: each \\n\\n is a bubble boundary; hard-cut on overflow."""
    if not text:
        return []
    chunks: list[str] = []
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_len:
            chunks.append(para)
            continue
        rest = para
        while rest:
            if len(rest) <= max_len:
                chunks.append(rest)
                break
            line = rest.rfind("\n", 0, max_len)
            at = line + 1 if line > 0 else max_len
            piece = rest[:at].strip()
            if piece:
                chunks.append(piece)
            rest = rest[at:].lstrip()
    return chunks


# ── Continuous Typing Indicator ─────────────────────────────────


def _typing_loop(
    client: ILinkClient,
    to_user: str,
    context_token: str,
    stop_event: threading.Event,
) -> None:
    """Send typing indicator every 5 seconds until stop_event is set."""
    while not stop_event.is_set():
        try:
            client.send_typing(to_user, context_token)
        except Exception:
            break
        stop_event.wait(5)


# ── Claude Code Output Parsing ──────────────────────────────────


def _parse_claude_output(stdout: str, user_id: str) -> str:
    """Parse Claude CLI JSON output, extract text and session_id."""
    if not stdout.strip():
        return "[No response from Claude Code]"

    try:
        data = json.loads(stdout)

        # Current Claude Code CLI emits a JSON array of event objects.
        # The final {"type":"result", ...} entry carries the text and session_id.
        if isinstance(data, list):
            for obj in reversed(data):
                if isinstance(obj, dict) and obj.get("type") == "result":
                    sid = obj.get("session_id")
                    if sid:
                        with _sessions_lock:
                            _sessions[user_id] = sid
                        _save_sessions()
                    result_text = obj.get("result", "")
                    return result_text if result_text else "[Empty response]"
            return "[No result event in Claude output]"

        session_id = data.get("session_id")
        if session_id:
            with _sessions_lock:
                _sessions[user_id] = session_id
            _save_sessions()

        result_text = data.get("result", "")
        if not result_text:
            result_text = data.get("text", data.get("content", str(data)))
        return result_text if result_text else "[Empty response]"
    except json.JSONDecodeError:
        # NDJSON fallback (streaming output)
        lines = stdout.strip().splitlines()
        text_parts = []
        for line in lines:
            try:
                obj = json.loads(line)
                if obj.get("type") == "result":
                    sid = obj.get("session_id")
                    if sid:
                        with _sessions_lock:
                            _sessions[user_id] = sid
                        _save_sessions()
                    text_parts.append(obj.get("result", ""))
                elif obj.get("type") == "assistant" and "content" in obj:
                    for block in obj["content"]:
                        if block.get("type") == "text":
                            text_parts.append(block["text"])
            except (json.JSONDecodeError, TypeError, KeyError):
                text_parts.append(line)
        return "\n".join(text_parts) if text_parts else stdout.strip()


# ── Agent Invocation ────────────────────────────────────────────


def call_agent(
    message: str,
    user_id: str,
    working_dir: str | None = None,
    image_paths: list[Path] | None = None,
) -> str:
    """Call the user's selected AI agent CLI and return the response."""
    agent_key = _get_user_agent(user_id)
    agent = AGENTS.get(agent_key)
    if not agent:
        return f"[Unknown agent: {agent_key}]"

    binary = _find_binary(agent["binary"])
    if not binary:
        return f"[{agent['name']} not found. Install it first.]"

    with _sessions_lock:
        session_id = _sessions.get(user_id) if agent_key == "claude" else None

    if agent_key == "claude":
        logger.debug("Claude session: %s", session_id[:12] if session_id else "new")

    # Append image instructions for Claude to read the files
    if image_paths:
        paths_str = ", ".join(str(p) for p in image_paths)
        img_note = (
            f"\n\nThe user sent {len(image_paths)} image(s). "
            f"Use the Read tool to view: {paths_str}\n"
            f"Describe what you see and respond to the user's message."
        )
        message += img_note

    cmd = agent["build_cmd"](message, session_id)
    # Replace binary name with full path
    cmd[0] = binary

    # Build system prompt with memory + persona + wechat channel
    if agent_key == "claude":
        sys_parts = [f"Current time: {_now_string()} ({TZ})", WECHAT_PROMPT]
        persona = _personas.get(user_id, "")
        if persona:
            sys_parts.append(f"Persona: {persona}")
        mem_ctx = _memory.get_context()
        if mem_ctx:
            sys_parts.append(mem_ctx)
        cmd.extend(["--append-system-prompt", "\n\n".join(sys_parts)])

    # Pass user message via stdin (clean, no context mixing)
    stdin_data = None
    if agent["use_stdin"]:
        stdin_data = message

    try:
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=1800,
            cwd=working_dir,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Claude session expired -> retry
            if (
                agent_key == "claude"
                and session_id
                and ("session" in stderr.lower() and "not found" in stderr.lower())
            ):
                logger.warning("Session expired, starting fresh.")
                with _sessions_lock:
                    _sessions.pop(user_id, None)
                _save_sessions()
                retry_cmd = _build_claude_cmd(message, None)
                retry_cmd[0] = binary
                # Re-add system prompt context
                sys_parts = [f"Current time: {_now_string()} ({TZ})", WECHAT_PROMPT]
                persona = _personas.get(user_id, "")
                if persona:
                    sys_parts.append(f"Persona: {persona}")
                mem_ctx = _memory.get_context()
                if mem_ctx:
                    sys_parts.append(mem_ctx)
                retry_cmd.extend(["--append-system-prompt", "\n\n".join(sys_parts)])
                result = subprocess.run(
                    retry_cmd,
                    input=stdin_data,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    cwd=working_dir,
                )

            if result.returncode != 0:
                logger.error("%s error (exit %d)", agent["name"], result.returncode)
                if stderr:
                    logger.error("%s stderr: %s", agent["name"], stderr[:500])
                return f"[{agent['name']} error. Check bridge logs for details.]"

        return agent["parse_output"](result.stdout, user_id)

    except subprocess.TimeoutExpired:
        return f"[{agent['name']} timed out after 30 minutes]"
    except FileNotFoundError:
        return f"[{agent['name']} CLI not found]"


# ── Image Handling ──────────────────────────────────────────────


def _handle_images(client: ILinkClient, message: dict) -> list[Path]:
    """Download images from message, return list of saved file paths."""
    media_items = client.extract_media(message)
    saved: list[Path] = []
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    for item in media_items:
        if item["type"] == "image" and (
            item.get("cdn_url") or item.get("encrypt_query_param")
        ):
            ext = ".jpg"
            path = MEDIA_DIR / f"img_{uuid.uuid4().hex[:12]}{ext}"
            if client.download_media(
                item.get("cdn_url", ""),
                item.get("aes_key", ""),
                path,
                encrypt_query_param=item.get("encrypt_query_param", ""),
            ):
                saved.append(path)
                logger.info(
                    "Downloaded image: %s (%d bytes)", path.name, path.stat().st_size
                )
        elif item["type"] == "file" and item.get("cdn_url"):
            raw_name = item.get("filename", f"file_{int(time.time())}")
            safe_name = Path(raw_name).name  # strip path components
            if not safe_name or safe_name.startswith("."):
                safe_name = f"file_{int(time.time() * 1000)}"
            path = MEDIA_DIR / safe_name
            if client.download_media(item["cdn_url"], item.get("aes_key", ""), path):
                saved.append(path)
                logger.info("Downloaded file: %s", path.name)

    return saved


# ── Session Management ──────────────────────────────────────────


def _scan_project_sessions(working_dir: str | None = None) -> list[dict]:
    """Scan ~/.claude/projects/<escaped-cwd>/*.jsonl for Claude Code sessions.

    Current Claude Code CLI stores each session as <session_id>.jsonl under a
    project folder derived from the cwd (slashes replaced with dashes).
    """
    cwd = Path(working_dir).resolve() if working_dir else Path.cwd()
    escaped = str(cwd).replace("/", "-")
    proj_dir = Path.home() / ".claude" / "projects" / escaped
    if not proj_dir.is_dir():
        return []

    out: list[dict] = []
    for jf in proj_dir.glob("*.jsonl"):
        try:
            mtime = jf.stat().st_mtime
        except OSError:
            continue
        summary = ""
        try:
            with jf.open() as f:
                for _ in range(30):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") == "summary" and d.get("summary"):
                        summary = d["summary"]
                        break
                    if d.get("type") == "user":
                        msg = d.get("message") or {}
                        content = msg.get("content")
                        if isinstance(content, str):
                            summary = content
                            break
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    summary = block.get("text", "")
                                    break
                            if summary:
                                break
        except OSError:
            pass
        out.append(
            {
                "id": jf.stem,
                "session_id": jf.stem,
                "summary": (summary or "").strip().replace("\n", " ") or "[no summary]",
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                "_mtime": mtime,
            }
        )
    out.sort(key=lambda s: s["_mtime"], reverse=True)
    return out


def list_claude_sessions(working_dir: str | None = None, user_id: str | None = None) -> str:
    """List recent Claude Code sessions by scanning project storage."""
    sessions = _scan_project_sessions(working_dir)
    if not sessions:
        return "No active sessions."

    shown = sessions[:10]
    if user_id is not None:
        with _last_listed_lock:
            _last_listed[user_id] = shown

    lines = ["Recent Claude Code Sessions:\n"]
    for i, s in enumerate(shown, 1):
        sid = s["id"]
        summary = s["summary"][:40]
        ts = s["updated_at"]
        lines.append(f"  {i}. [{sid[:8]}] {summary} ({ts})")
    lines.append("\nUse /use <number> to switch session")
    return "\n".join(lines)


def pick_session(choice: str, user_id: str, working_dir: str | None = None) -> str:
    """Switch to a session by number or session-id prefix.

    Numeric choices resolve against the last listing shown to this user
    (via /ss), so the numbering stays consistent even if session
    mtimes shift between listing and selection.
    """
    with _last_listed_lock:
        cached = list(_last_listed.get(user_id, []))

    # Numeric pick → prefer cached snapshot; fall back to fresh scan if absent.
    try:
        idx = int(choice) - 1
    except ValueError:
        idx = None

    if idx is not None:
        source = cached if cached else _scan_project_sessions(working_dir)
        if not source:
            return "No sessions available. Run /ss first."
        if 0 <= idx < len(source):
            target = source[idx]
            session_id = target["id"]
            summary = target["summary"][:40]
            with _sessions_lock:
                _sessions[user_id] = session_id
            _save_sessions()
            return f"Switched to session: [{session_id[:8]}] {summary}"
        return f"Session '{choice}' not found. Use /ss to list."

    # Prefix-by-id pick always uses a fresh scan (id-based, not positional).
    sessions = _scan_project_sessions(working_dir)
    if not sessions:
        return "No sessions available."

    target = None
    for s in sessions:
        if s["id"].startswith(choice):
            target = s
            break

    if not target:
        return f"Session '{choice}' not found. Use /ss to list."

    session_id = target["id"]
    summary = target["summary"][:40]
    with _sessions_lock:
        _sessions[user_id] = session_id
    _save_sessions()
    return f"Switched to session: [{session_id[:8]}] {summary}"


# ── Message Handler ─────────────────────────────────────────────


def _schedule_flush(client: ILinkClient, user_id: str, working_dir: str | None) -> None:
    """Reset 5s debounce Timer for this user."""
    with _buffer_lock:
        old = _msg_timer.pop(user_id, None)
        if old:
            old.cancel()
        timer = threading.Timer(
            DEBOUNCE_S,
            lambda: _flush(client, user_id, working_dir),
        )
        _msg_timer[user_id] = timer
        timer.start()


def _flush(client: ILinkClient, user_id: str, working_dir: str | None) -> None:
    """Timer fired — merge buffered messages and call_agent once."""
    with _buffer_lock:
        if user_id in _inflight:
            # Re-arm; current call_agent in flight, retry in 5s
            timer = threading.Timer(
                DEBOUNCE_S,
                lambda: _flush(client, user_id, working_dir),
            )
            _msg_timer[user_id] = timer
            timer.start()
            return
        items = _msg_buffer.pop(user_id, [])
        _msg_timer.pop(user_id, None)
        if not items:
            return
        _inflight.add(user_id)

    try:
        merged_text = "\n\n".join(i["text"] for i in items if i["text"]).strip()
        merged_images: list[Path] = []
        for i in items:
            merged_images.extend(i["images"])
        ctx_token = items[-1]["ctx"]

        stop_typing = threading.Event()
        typing_thread = threading.Thread(
            target=_typing_loop,
            args=(client, user_id, ctx_token, stop_typing),
            daemon=True,
        )
        typing_thread.start()

        try:
            response = call_agent(
                merged_text, user_id, working_dir, merged_images or None
            )
            main_text, buddy_chunk = extract_buddy(response)
            main_text = md_to_plain(main_text)
            chunks = split_msg(main_text)
            if buddy_chunk:
                chunks.append(buddy_chunk)
        finally:
            stop_typing.set()
            typing_thread.join(timeout=1)

        for chunk in chunks:
            client.send_text(user_id, ctx_token, chunk)
        logger.info(
            "Replied to %s (%d msgs merged, %d bubbles, %d chars)",
            user_id[:16],
            len(items),
            len(chunks),
            len(response),
        )

        try:
            _memory.log_conversation(merged_text[:200], response[:200])
        except Exception:
            pass

    except Exception as e:
        logger.error("flush error: %s", e, exc_info=True)
        try:
            client.send_text(user_id, items[-1]["ctx"], "[Internal error, please try again]")
        except Exception:
            pass
    finally:
        with _buffer_lock:
            _inflight.discard(user_id)
            # If new messages arrived during call_agent, re-arm Timer
            if _msg_buffer.get(user_id):
                old = _msg_timer.pop(user_id, None)
                if old:
                    old.cancel()
                timer = threading.Timer(
                    DEBOUNCE_S,
                    lambda: _flush(client, user_id, working_dir),
                )
                _msg_timer[user_id] = timer
                timer.start()


def handle_message(
    client: ILinkClient,
    msg: dict,
) -> None:
    """Handle a single incoming WeChat message (runs in thread)."""
    global _working_dir
    from_user = msg.get("from_user_id", "unknown")
    context_token = msg.get("context_token", "")

    try:
        text = client.extract_text(msg) or ""

        # Download images/files (best-effort)
        image_paths: list[Path] = []
        try:
            image_paths = _handle_images(client, msg)
        except Exception as e:
            logger.warning("Image download failed: %s", e)

        # Handle voice messages
        try:
            for item in client.extract_media(msg):
                if item["type"] == "voice" and item.get("text"):
                    text = (text + "\n" + item["text"]) if text else item["text"]
        except Exception as e:
            logger.warning("Voice extraction failed: %s", e)

        if not text.strip() and not image_paths:
            return

        # Debug: log raw item_list types
        raw_items = msg.get("item_list", [])
        item_types = [i.get("type") for i in raw_items]
        if any(t != 1 for t in item_types):
            logger.info(
                "Raw item_list types: %s, keys: %s",
                item_types,
                [list(i.keys()) for i in raw_items],
            )

        logger.debug(
            "Message from %s (%d chars, %d images)",
            from_user[:16],
            len(text),
            len(image_paths),
        )

        cmd = text.strip()
        # Tolerate accidental whitespace right after the leading slash
        # (iOS WeChat keyboards sometimes insert a space after "/").
        if cmd.startswith("/"):
            cmd = "/" + cmd[1:].lstrip()
        cmd_lower = cmd.lower()

        with _workdir_lock:
            working_dir = _working_dir

        # ── Special commands ──
        if cmd_lower in ("/reset", "/clear"):
            with _sessions_lock:
                _sessions.pop(from_user, None)
            _save_sessions()
            client.send_text(from_user, context_token, "Session cleared.")
            return

        if cmd_lower == "/status":
            with _sessions_lock:
                sid = _sessions.get(from_user, "")[:8] or "none"
            agent_name = AGENTS.get(_get_user_agent(from_user), {}).get("name", "?")
            client.send_text(
                from_user,
                context_token,
                f"Bridge: running\nAgent: {agent_name}\nSession: {sid}\nWorking dir: {working_dir or '(default)'}",
            )
            return

        if cmd_lower == "/ss":
            client.send_text(
                from_user, context_token, list_claude_sessions(working_dir, from_user)
            )
            return

        if cmd_lower.startswith("/use "):
            client.send_text(
                from_user,
                context_token,
                pick_session(cmd[5:].strip(), from_user, working_dir),
            )
            return

        if cmd_lower == "/new":
            with _sessions_lock:
                _sessions.pop(from_user, None)
            _save_sessions()
            client.send_text(from_user, context_token, "New session started.")
            return

        if cmd_lower.startswith("/workdir"):
            parts = cmd.split(maxsplit=1)
            if len(parts) < 2:
                client.send_text(
                    from_user,
                    context_token,
                    f"Current: {working_dir or '(default)'}\nUsage: /workdir /path/to/project",
                )
                return
            new_path = Path(parts[1].strip()).expanduser()
            if not new_path.is_dir():
                client.send_text(
                    from_user, context_token, f"Directory not found: {parts[1].strip()}"
                )
                return
            with _workdir_lock:
                _working_dir = str(new_path)
            client.send_text(
                from_user, context_token, f"Working directory changed to: {new_path}"
            )
            return

        if cmd_lower in ("/agent", "/agents"):
            current = _get_user_agent(from_user)
            lines = ["Available agents:\n"]
            for key, agent in AGENTS.items():
                installed = "ok" if _find_binary(agent["binary"]) else "not installed"
                marker = " <-- current" if key == current else ""
                lines.append(f"  {key}: {agent['name']} ({installed}){marker}")
            lines.append("\nUse /agent <name> to switch")
            client.send_text(from_user, context_token, "\n".join(lines))
            return

        if cmd_lower.startswith("/agent "):
            agent_key = cmd[7:].strip().lower()
            if agent_key not in AGENTS:
                client.send_text(
                    from_user,
                    context_token,
                    f"Unknown agent: {agent_key}\nAvailable: {', '.join(AGENTS)}",
                )
                return
            agent = AGENTS[agent_key]
            if not _find_binary(agent["binary"]):
                client.send_text(
                    from_user,
                    context_token,
                    f"{agent['name']} not installed ({agent['binary']} not in PATH)",
                )
                return
            _user_agent[from_user] = agent_key
            with _sessions_lock:
                _sessions.pop(from_user, None)
            _save_sessions()
            client.send_text(from_user, context_token, f"Switched to {agent['name']}")
            return

        # ── Memory commands ──
        if cmd_lower.startswith("/remember "):
            content = cmd[10:].strip()
            client.send_text(from_user, context_token, _memory.remember(content))
            return

        if cmd_lower.startswith("/forget "):
            keyword = cmd[8:].strip()
            client.send_text(from_user, context_token, _memory.forget(keyword))
            return

        if cmd_lower == "/memory":
            client.send_text(from_user, context_token, _memory.list_memories())
            return

        if cmd_lower.startswith("/search "):
            query = cmd[8:].strip()
            client.send_text(from_user, context_token, _memory.search(query))
            return

        if cmd_lower == "/log":
            client.send_text(from_user, context_token, _memory.get_today_log())
            return

        # ── Persona commands ──
        if cmd_lower == "/persona":
            p = _personas.get(from_user, "")
            client.send_text(
                from_user,
                context_token,
                f"Current persona: {p}"
                if p
                else "No persona set.\nUse /persona <description>",
            )
            return

        if cmd_lower.startswith("/persona "):
            persona_text = cmd[9:].strip()
            _personas[from_user] = persona_text
            _save_personas()
            client.send_text(from_user, context_token, f"Persona set: {persona_text}")
            return

        # ── Scheduler commands ──
        if cmd_lower.startswith("/remind "):
            parts = cmd[8:].strip().split(maxsplit=1)
            if len(parts) < 2:
                client.send_text(
                    from_user,
                    context_token,
                    "Usage: /remind <time> <message>\nExample: /remind 17:00 Go home",
                )
                return
            client.send_text(
                from_user,
                context_token,
                _scheduler.add_reminder(from_user, parts[0], parts[1]),
            )
            return

        if cmd_lower.startswith("/every "):
            parts = cmd[7:].strip().split(maxsplit=1)
            if len(parts) < 2:
                client.send_text(
                    from_user,
                    context_token,
                    "Usage: /every <interval> <message>\nExample: /every 30m Check server",
                )
                return
            run_claude = parts[1].startswith("!")
            msg = parts[1][1:].strip() if run_claude else parts[1]
            client.send_text(
                from_user,
                context_token,
                _scheduler.add_interval(from_user, parts[0], msg, run_claude),
            )
            return

        if cmd_lower.startswith("/cron "):
            # /cron 0 9 * * 1-5 Good morning
            cron_parts = cmd[6:].strip().split(maxsplit=5)
            if len(cron_parts) < 6:
                client.send_text(
                    from_user,
                    context_token,
                    "Usage: /cron <min> <hour> <day> <mon> <wday> <message>\nExample: /cron 0 9 * * 1-5 Good morning",
                )
                return
            cron_expr = " ".join(cron_parts[:5])
            msg = cron_parts[5]
            run_claude = msg.startswith("!")
            if run_claude:
                msg = msg[1:].strip()
            client.send_text(
                from_user,
                context_token,
                _scheduler.add_cron(from_user, cron_expr, msg, run_claude),
            )
            return

        if cmd_lower == "/jobs":
            client.send_text(from_user, context_token, _scheduler.list_jobs(from_user))
            return

        if cmd_lower.startswith("/cancel "):
            job_id = cmd[8:].strip()
            client.send_text(
                from_user, context_token, _scheduler.cancel_job(from_user, job_id)
            )
            return

        if cmd_lower == "/help":
            client.send_text(
                from_user,
                context_token,
                "Session:\n"
                "  /ss /use <n> /new /reset\n"
                "Agent:\n"
                "  /agent /agent <x> /workdir <p>\n"
                "Memory:\n"
                "  /remember <text> /forget <key>\n"
                "  /memory /search <q> /log\n"
                "Persona:\n"
                "  /persona /persona <desc>\n"
                "Schedule:\n"
                "  /remind <time> <msg>\n"
                "  /every <interval> <msg>\n"
                "  /cron <expr> <msg>\n"
                "  /jobs /cancel <id>\n"
                "Other:\n"
                "  /status /help\n"
                "\nPrefix msg with ! in /every /cron to run through Claude.\n"
                "Anything else is sent to the current agent.",
            )
            return

        # ── Forward to AI agent (debounced) ──
        with _buffer_lock:
            _msg_buffer.setdefault(from_user, []).append({
                "text": text,
                "images": list(image_paths),
                "ctx": context_token,
            })
        _schedule_flush(client, from_user, working_dir)

    except Exception as e:
        logger.error("handle_message error: %s", e, exc_info=True)
        try:
            client.send_text(
                from_user, context_token, "[Internal error, please try again]"
            )
        except Exception:
            pass


# ── Main Bridge Loop ────────────────────────────────────────────


def run_bridge(working_dir: str | None = None) -> None:
    """Main bridge loop: poll WeChat -> call agent -> reply."""
    global _working_dir
    _working_dir = working_dir

    client = ILinkClient()

    if not client.is_logged_in:
        print("No saved login found. Starting QR code login...\n")
        client.login()

    _load_sessions()
    _load_personas()

    # Scheduler callback: send message (and optionally run Claude) when job fires
    def _on_job_fire(user_id: str, message: str, run_claude: bool) -> None:
        try:
            if run_claude:
                response = call_agent(message, user_id, _working_dir, None)
                main_text, buddy_chunk = extract_buddy(response)
                main_text = md_to_plain(main_text)
                chunks = split_msg(main_text)
                if not chunks:
                    chunks = ["[Empty response]"]
                chunks[0] = f"[Scheduled] {chunks[0]}"
                if buddy_chunk:
                    chunks.append(buddy_chunk)
            else:
                chunks = [f"[Reminder] {message}"]
            for chunk in chunks:
                if not client.send_text(user_id, "", chunk):
                    logger.warning(
                        "Failed to deliver scheduled message to %s", user_id[:16]
                    )
                    break
        except Exception as e:
            logger.error("Scheduler callback error: %s", e)

    _scheduler.set_callback(_on_job_fire)
    _scheduler.start()

    print("\n=== WeChat-Claude Code Bridge ===")
    print(f"Working directory: {working_dir or '(default)'}")
    print(f"Default agent: {AGENTS['claude']['name']}")
    print(f"Memory: {_memory.get_context()[:30] or '(empty)'}...")
    print(f"Scheduled jobs: {len(_scheduler._jobs)}")
    print("Listening for WeChat messages... (Ctrl+C to stop)\n")

    consecutive_errors = 0
    max_consecutive_errors = 10

    try:
        while True:
            try:
                messages = client.poll_messages()
                consecutive_errors = 0

                for msg in messages:
                    _executor.submit(handle_message, client, msg)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                consecutive_errors += 1
                logger.error(
                    "Poll error (%d/%d): %s",
                    consecutive_errors,
                    max_consecutive_errors,
                    e,
                )
                err_str = str(e).lower()
                if "401" in err_str or "unauthorized" in err_str:
                    logger.warning("Token may have expired. Re-login with --login")

                if consecutive_errors >= max_consecutive_errors:
                    logger.critical("Too many consecutive errors, stopping.")
                    break
                time.sleep(min(2**consecutive_errors, 30))

    except KeyboardInterrupt:
        print("\nStopping bridge...")
    finally:
        _save_sessions()
        _scheduler.stop()
        _executor.shutdown(wait=False)
        client.close()
        print("Bridge stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat ClawBot <-> AI Agent bridge")
    parser.add_argument("--logout", action="store_true", help="Clear login credentials")
    parser.add_argument("--login", action="store_true", help="Force re-login")
    parser.add_argument(
        "--workdir", "-w", type=str, default=None, help="Working directory"
    )
    args = parser.parse_args()

    if args.logout:
        client = ILinkClient()
        client.logout()
        client.close()
        print("Logged out.")
        return

    if args.login:
        client = ILinkClient()
        client.logout()
        client.login()
        client.close()
        print("Login complete.")
        return

    run_bridge(working_dir=args.workdir)


if __name__ == "__main__":
    main()
