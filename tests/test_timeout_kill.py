"""Regression tests for subprocess timeout handling in CLI-backed seats.

The bug (observed 2026-08-01): `_run_subprocess` reported "subagent timed
out after Ns" but only killed the direct child. On Windows the direct
child of an npm-installed CLI is a `.cmd` shim (cmd.exe), whose node.exe
descendants survived and kept running the agent loop — two timed-out
ask_kimi calls kept writing files for 21 and 44 more minutes, racing a
follow-up session in the same directory. On top of that, the timeout
error carried no session_id, so the orphan's session couldn't be resumed.

These tests use long-running fake CLIs that spawn their own children
(mirroring the shim -> node -> workers tree) and assert that:
  1. the WHOLE tree is dead after a timeout (a grandchild heartbeat file
     stops updating), and
  2. the timeout error message carries a recoverable session id for each
     CLI-backed seat (kimi, codex, agy).
"""

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

import server

# Writes a monotonically growing heartbeat file until killed. If the
# process-tree kill misses it, the file keeps growing after the timeout.
GRANDCHILD_SRC = """\
import sys, time
hb = sys.argv[1]
while True:
    with open(hb, "a") as f:
        f.write(str(time.time()) + "\\n")
    time.sleep(0.1)
"""

# Direct child that spawns the heartbeat grandchild, then hangs — the
# minimal shape of the orphan bug.
PARENT_SRC = """\
import subprocess, sys, time
hb = sys.argv[1]
grandchild = sys.argv[2]
subprocess.Popen([sys.executable, grandchild, hb])
time.sleep(300)
"""

# Fake kimi CLI: creates its session dir in the (fake) store at startup
# like the real CLI does, emits one stream-json event, spawns a heartbeat
# grandchild, then hangs past the timeout. Ignores its argv.
FAKE_KIMI_TEMPLATE = """\
import json, os, subprocess, sys, time
session_dir = {session_dir!r}
hb = {hb!r}
grandchild = {grandchild!r}
os.makedirs(session_dir, exist_ok=True)
print(json.dumps({{"role": "assistant", "content": "partial answer"}}), flush=True)
subprocess.Popen([sys.executable, grandchild, hb])
time.sleep(300)
"""

# Fake codex CLI: prints its session id early (like the real one), hangs.
FAKE_CODEX_SRC = """\
import time
print("session id: 0f14d0ab-9605-4a62-a9e4-5ed26688389b", flush=True)
time.sleep(300)
"""


def _make_cli_shim(bindir: Path, name: str, script: Path) -> None:
    """Install `script` as a CLI named `name` in bindir. On Windows this
    is a .cmd shim — the same shape npm uses for kimi/codex, and exactly
    the indirection (cmd.exe -> interpreter) that plain proc.kill()
    failed to see through."""
    if sys.platform == "win32":
        shim = bindir / f"{name}.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n')
    else:
        shim = bindir / name
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        shim.chmod(0o755)


def _assert_heartbeat_stopped(hb: Path) -> None:
    """The grandchild wrote heartbeats while alive; after a proper tree
    kill the file must stop growing."""
    assert hb.exists(), "grandchild never started — test setup problem"
    time.sleep(0.5)  # let any in-flight write land
    before = hb.read_text()
    assert before, "grandchild never wrote a heartbeat"
    time.sleep(1.2)
    after = hb.read_text()
    assert after == before, (
        "grandchild is still writing after the timeout kill — "
        "the process tree was not fully terminated"
    )


def test_timeout_kills_whole_process_tree(tmp_path):
    hb = tmp_path / "heartbeat.txt"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(GRANDCHILD_SRC)
    parent = tmp_path / "parent.py"
    parent.write_text(PARENT_SRC)

    with pytest.raises(server.SubprocessTimeout):
        asyncio.run(
            server._run_subprocess(
                [sys.executable, str(parent), str(hb), str(grandchild)],
                timeout_sec=3,
            )
        )
    _assert_heartbeat_stopped(hb)


def test_timeout_captures_partial_output(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text(
        "import time\n"
        "print('MARKER-BEFORE-TIMEOUT', flush=True)\n"
        "time.sleep(300)\n"
    )
    with pytest.raises(server.SubprocessTimeout) as ei:
        asyncio.run(
            server._run_subprocess([sys.executable, str(script)], timeout_sec=2)
        )
    assert "MARKER-BEFORE-TIMEOUT" in ei.value.partial_stdout
    assert "timed out after 2s" in str(ei.value)
    assert "process tree" in str(ei.value)


def test_normal_exit_paths_unchanged():
    out, err = asyncio.run(
        server._run_subprocess(
            [
                sys.executable,
                "-c",
                "import sys; print('ok'); print('warn', file=sys.stderr)",
            ],
            timeout_sec=30,
        )
    )
    assert out.strip() == "ok"
    assert err.strip() == "warn"

    with pytest.raises(RuntimeError, match="exited with code 7"):
        asyncio.run(
            server._run_subprocess(
                [sys.executable, "-c", "import sys; sys.exit(7)"],
                timeout_sec=30,
            )
        )


def test_stdin_data_round_trip():
    # The agy helper is fed its JSON request over stdin; make sure the
    # rewritten runner still delivers it.
    out, _err = asyncio.run(
        server._run_subprocess(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
            timeout_sec=30,
            stdin_data="hello",
        )
    )
    assert out.strip() == "HELLO"


def test_ask_kimi_timeout_recovers_session_id_and_kills_tree(
    tmp_path, monkeypatch
):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    store = tmp_path / "kimi-store"
    sid = f"session_{uuid.uuid4()}"
    session_dir = store / f"wd_{workdir.name}_0123456789ab" / sid
    hb = tmp_path / "heartbeat.txt"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(GRANDCHILD_SRC)
    fake = tmp_path / "fake_kimi.py"
    fake.write_text(
        FAKE_KIMI_TEMPLATE.format(
            session_dir=str(session_dir),
            hb=str(hb),
            grandchild=str(grandchild),
        )
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_cli_shim(bindir, "kimi", fake)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(server, "_KIMI_SESSIONS_DIR", store)

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(
            server.ask_kimi(prompt="hi", timeout_sec=3, cwd=str(workdir))
        )
    msg = str(ei.value)
    assert "timed out" in msg
    assert sid in msg, f"recovered session id missing from error: {msg}"
    assert "resume" in msg
    _assert_heartbeat_stopped(hb)


def test_ask_kimi_timeout_on_resume_reports_known_session_id(
    tmp_path, monkeypatch
):
    # When the caller passed a session_id (resume), the timeout error must
    # echo it back even if nothing is recoverable from output or store.
    fake = tmp_path / "fake_kimi.py"
    fake.write_text("import time\ntime.sleep(300)\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_cli_shim(bindir, "kimi", fake)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(server, "_KIMI_SESSIONS_DIR", tmp_path / "empty-store")

    known = f"session_{uuid.uuid4()}"
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(server.ask_kimi(prompt="hi", timeout_sec=2, session_id=known))
    assert known in str(ei.value)


def test_ask_codex_timeout_recovers_session_id(tmp_path, monkeypatch):
    fake = tmp_path / "fake_codex.py"
    fake.write_text(FAKE_CODEX_SRC)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_cli_shim(bindir, "codex", fake)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(server.ask_codex(prompt="hi", timeout_sec=3))
    msg = str(ei.value)
    assert "timed out" in msg
    assert "0f14d0ab-9605-4a62-a9e4-5ed26688389b" in msg
    assert "resume" in msg


def test_ask_agy_timeout_recovers_conversation_id(tmp_path, monkeypatch):
    # The ConPTY helper itself needs a real console (pywinpty), so this
    # exercises ask_agy's timeout-recovery logic with the pty layer
    # stubbed out: a timeout must surface the conversation id from agy's
    # cwd -> conversation cache.
    workdir = tmp_path / "w"
    workdir.mkdir()
    effective = str(workdir.resolve())
    state = tmp_path / "agy-state"
    (state / "cache").mkdir(parents=True)
    conv_id = "11112222-3333-4444-5555-666677778888"
    (state / "cache" / "last_conversations.json").write_text(
        json.dumps({effective: conv_id}), encoding="utf-8"
    )
    monkeypatch.setattr(server, "_AGY_STATE_DIR", state)

    async def fake_pty_run(args, timeout_sec, cwd):
        raise server.SubprocessTimeout(
            f"subagent timed out after {timeout_sec}s; "
            f"killed the whole process tree."
        )

    monkeypatch.setattr(server, "_agy_pty_run", fake_pty_run)

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(server.ask_agy(prompt="hi", timeout_sec=5, cwd=str(workdir)))
    msg = str(ei.value)
    assert "timed out" in msg
    assert conv_id in msg
    assert "resume" in msg
