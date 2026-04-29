"""Dry-run test for debounce logic — no real WeChat / Claude calls.

Stubs out call_agent / typing / send_text to inspect what _flush dispatches.
Run: .venv/bin/python test_debounce.py
"""

import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bridge  # noqa: E402


# ── Stubs ────────────────────────────────────────────────────────

call_agent_log: list[dict] = []
sent_chunks: list[tuple[str, str, str]] = []  # (user_id, ctx, chunk)


def fake_call_agent(text, user_id, working_dir, image_paths):
    call_agent_log.append({
        "text": text,
        "user_id": user_id,
        "images": list(image_paths) if image_paths else [],
        "t": time.monotonic(),
    })
    # Simulate variable agent latency
    time.sleep(0.2)
    return f"reply to: {text[:60]}\n\n<!-- buddy: ack -->"


class FakeClient:
    def send_text(self, user_id, ctx, chunk):
        sent_chunks.append((user_id, ctx, chunk))
        return True

    def send_typing(self, *a, **kw):
        return None


def _no_typing(client, user_id, ctx, stop_event):
    # No-op typing loop; honor stop_event
    stop_event.wait()


# Patch
bridge.call_agent = fake_call_agent
bridge._typing_loop = _no_typing
# Speed up debounce window for tests
bridge.DEBOUNCE_S = 0.5


def reset_state():
    call_agent_log.clear()
    sent_chunks.clear()
    with bridge._buffer_lock:
        bridge._msg_buffer.clear()
        for t in bridge._msg_timer.values():
            t.cancel()
        bridge._msg_timer.clear()
        bridge._inflight.clear()


# ── Helpers ──────────────────────────────────────────────────────

def enqueue(client, user_id, text, images=None, ctx="ctx-1"):
    """Mimic the post-buffer code in handle_message."""
    with bridge._buffer_lock:
        bridge._msg_buffer.setdefault(user_id, []).append({
            "text": text,
            "images": list(images or []),
            "ctx": ctx,
        })
    bridge._schedule_flush(client, user_id, working_dir=None)


def wait_idle(timeout=3.0):
    """Wait until no Timer pending and no inflight."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with bridge._buffer_lock:
            no_timer = not bridge._msg_timer
            no_inflight = not bridge._inflight
            no_buf = not any(bridge._msg_buffer.values())
        if no_timer and no_inflight and no_buf:
            time.sleep(0.05)  # let in-flight thread finish send loop
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout: timer={bridge._msg_timer} inflight={bridge._inflight} buf={bridge._msg_buffer}")


# ── Tests ────────────────────────────────────────────────────────

def test_single_message():
    reset_state()
    c = FakeClient()
    enqueue(c, "userA", "老公")
    wait_idle()
    assert len(call_agent_log) == 1, f"expected 1 call, got {len(call_agent_log)}"
    assert call_agent_log[0]["text"] == "老公"
    print("✓ test_single_message")


def test_burst_merge():
    reset_state()
    c = FakeClient()
    enqueue(c, "userA", "老公")
    time.sleep(0.1)
    enqueue(c, "userA", "在干嘛")
    time.sleep(0.1)
    enqueue(c, "userA", "饿了")
    wait_idle()
    assert len(call_agent_log) == 1, f"expected 1 merged call, got {len(call_agent_log)}"
    assert call_agent_log[0]["text"] == "老公\n\n在干嘛\n\n饿了"
    print("✓ test_burst_merge")


def test_text_plus_image():
    reset_state()
    c = FakeClient()
    enqueue(c, "userA", "看看这个", images=[Path("/tmp/a.jpg")])
    time.sleep(0.1)
    enqueue(c, "userA", "", images=[Path("/tmp/b.jpg")])
    wait_idle()
    assert len(call_agent_log) == 1
    assert call_agent_log[0]["text"] == "看看这个"  # empty text dropped from join
    assert call_agent_log[0]["images"] == [Path("/tmp/a.jpg"), Path("/tmp/b.jpg")]
    print("✓ test_text_plus_image")


def test_inflight_buffers_next_round():
    """Message arriving during call_agent goes to next round, not interrupting."""
    reset_state()
    c = FakeClient()

    # Slow agent
    def slow_agent(text, user_id, working_dir, image_paths):
        call_agent_log.append({"text": text, "user_id": user_id, "images": [], "t": time.monotonic()})
        time.sleep(1.0)
        return f"reply\n\n<!-- buddy: x -->"

    bridge.call_agent = slow_agent
    try:
        enqueue(c, "userA", "first")
        time.sleep(0.7)  # wait past debounce, _flush starts; agent now sleeping
        assert len(call_agent_log) == 1, "first call should have started"
        # Send during inflight
        enqueue(c, "userA", "second")
        wait_idle(timeout=5.0)
        assert len(call_agent_log) == 2, f"expected 2 calls (one per round), got {len(call_agent_log)}"
        assert call_agent_log[0]["text"] == "first"
        assert call_agent_log[1]["text"] == "second"
        # second should start AFTER first finishes
        gap = call_agent_log[1]["t"] - call_agent_log[0]["t"]
        assert gap >= 1.0, f"second round started too early: gap={gap:.2f}s"
    finally:
        bridge.call_agent = fake_call_agent
    print("✓ test_inflight_buffers_next_round")


def test_multi_user_independent():
    reset_state()
    c = FakeClient()
    enqueue(c, "userA", "hi A")
    enqueue(c, "userB", "hi B")
    wait_idle()
    texts = sorted(c["text"] for c in call_agent_log)
    assert texts == ["hi A", "hi B"], f"got {texts}"
    print("✓ test_multi_user_independent")


def test_send_chunks_use_last_ctx():
    reset_state()
    c = FakeClient()
    enqueue(c, "userA", "first", ctx="ctx-OLD")
    time.sleep(0.1)
    enqueue(c, "userA", "second", ctx="ctx-NEW")
    wait_idle()
    ctxs = {ctx for _, ctx, _ in sent_chunks}
    assert ctxs == {"ctx-NEW"}, f"chunks should all use latest ctx, got {ctxs}"
    print("✓ test_send_chunks_use_last_ctx")


def test_buddy_chunk_appended():
    reset_state()
    c = FakeClient()
    enqueue(c, "userA", "ping")
    wait_idle()
    chunks = [chunk for _, _, chunk in sent_chunks]
    # Last chunk should be the buddy line
    assert any("ack" in c for c in chunks), f"buddy chunk missing in {chunks}"
    print("✓ test_buddy_chunk_appended")


# ── Run ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_single_message,
        test_burst_merge,
        test_text_plus_image,
        test_inflight_buffers_next_round,
        test_multi_user_independent,
        test_send_chunks_use_last_ctx,
        test_buddy_chunk_appended,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)
