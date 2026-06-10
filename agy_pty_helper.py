"""ConPTY helper for ask_agy — runs the Antigravity CLI in a pseudo-console.

The agy CLI blocks forever with zero output when stdio is a pipe; every
subcommand, including `-p` print mode, demands a real console. pywinpty
provides one, but its ConPTY machinery cannot share a process with
asyncio's Windows proactor event loop (Overlapped deallocation errors kill
the loop — observed 2026-06-11). So the MCP server delegates to this
standalone helper process instead of spawning the PTY in-process.

Protocol: JSON on stdin: {"args": [...agy argv...], "timeout_sec": int,
"cwd": str | null}. On success, JSON on stdout: {"output": str}. On
timeout, exit 3 with partial output on stderr.

Requires pywinpty==2.0.14 — the 3.x Rust build fails to load its DLL on
this machine (verified 2026-06-10).
"""

import json
import re
import sys
import threading
import time

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]|\r")
# Spinner frames ("braille char + label...") survive ANSI stripping because
# agy repaints them with \r, not cursor codes.
_SPINNER_RE = re.compile(r"[⠀-⣿][^\n⠀-⣿]*?\.\.\.")


def main() -> int:
    from winpty import PtyProcess

    req = json.load(sys.stdin)
    args = req["args"]
    timeout_sec = req.get("timeout_sec", 600)
    cwd = req.get("cwd")

    # Wide pty: ConPTY hard-wraps at the column count, injecting newlines
    # into long output lines. 500 cols keeps prose intact in practice.
    proc = PtyProcess.spawn(args, cwd=cwd, dimensions=(50, 500))
    chunks = []

    def reader():
        # proc.read blocks with no timeout, hence the daemon thread.
        while True:
            try:
                data = proc.read(4096)
            except Exception:
                return
            if not data:
                return
            chunks.append(data)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    deadline = time.time() + timeout_sec
    while time.time() < deadline and proc.isalive():
        time.sleep(0.25)
    timed_out = proc.isalive()
    try:
        proc.terminate(force=True)
    except Exception:
        pass
    t.join(timeout=2)

    text = _ANSI_RE.sub("", "".join(chunks))
    text = _SPINNER_RE.sub("", text).strip()

    if timed_out:
        sys.stderr.write(
            "agy timed out after %ds. If output below is empty, the likely "
            "cause is an interactive prompt (workspace trust or expired "
            "Google sign-in) that print mode cannot answer:\n%s"
            % (timeout_sec, text[-2000:])
        )
        sys.stderr.flush()
        return 3

    sys.stdout.write(json.dumps({"output": text}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    # os._exit dodges pywinpty 2.x's flaky interpreter-teardown hang.
    import os

    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
