# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.2.0,<2",
#   "httpx>=0.27.0",
#   "pyjwt>=2.0.0",
#   "pywinpty==2.0.14; sys_platform == 'win32'",
# ]
# ///
"""MARS — Model Adapter Routing System (MCP server).

(Project was previously named ModelMesh; renamed 2026-05-04. The legacy
`modelmesh` console command and `MODELMESH_*` env vars continue to work
with a DeprecationWarning until MARS v0.2.0 — see CHANGELOG.)

Exposes eight subagent tools to any MCP client (Claude Code, Cursor, etc.):
  - ask_codex     -> wraps the local `codex` CLI (agentic loop)
  - ask_agy       -> wraps the local `agy` Antigravity CLI: Claude Opus/
                     Sonnet 4.6 (Thinking) + Gemini 3.5/3.1 on Google AI Pro
                     (multi-turn via agy conversations; Windows ConPTY)
                     NOTE: the standalone `ask_gemini` tool (the `gemini` CLI)
                     was removed 2026-06-22 — Google discontinued the free
                     Gemini Code Assist CLI tier ("IneligibleTierError …
                     migrate to Antigravity"); reach Gemini via ask_agy.
  - ask_openrouter-> chat completion via OpenRouter (multi-turn supported)
  - ask_deepseek  -> chat completion via DeepSeek API (multi-turn supported)
  - ask_grok      -> chat completion via xAI Grok API (multi-turn supported)
  - ask_zai       -> chat completion via z.ai (Zhipu) GLM API (multi-turn supported)
  - ask_mimo      -> chat completion via Xiaomi MiMo API (multi-turn supported)
  - ask_kimi      -> wraps the local `kimi` Kimi Code CLI (agentic loop;
                     Moonshot K3/K2.7 on the Kimi Code subscription).
                     Rerouted 2026-08-01 from the Moonshot HTTPS API.

Plus admin tools:
  - list_api_sessions  -> enumerate stored DeepSeek/OpenRouter/Grok/z.ai/mimo
                          sessions (+ legacy API-era kimi sessions)
  - delete_api_session -> drop a stored session

Codex/agy/kimi inherit auth from their own CLIs (`codex login`,
`agy` interactive Google OAuth, `kimi login` device-code flow).
ask_agy additionally needs pywinpty==2.0.14 (agy demands a real
console; see _agy_pty_run).
API tools read keys from env:
  - OPENROUTER_API_KEY for ask_openrouter
  - DEEPSEEK_API_KEY   for ask_deepseek
  - XAI_API_KEY        for ask_grok
  - ZAI_API_KEY        for ask_zai (legacy "id.secret" format; tool generates
                                    JWT per call, do NOT pre-sign)
  - MIMO_API_KEY       for ask_mimo (Xiaomi MiMo Singapore plan)

All eight chat tools return: {"output": str, "session_id": str | None}.

Codex sessions live where the CLI puts them (codex sqlite); kimi
sessions live in the CLI's own store (~/.kimi-code/sessions).
DeepSeek / OpenRouter / Grok / z.ai / mimo sessions live in
$MARS_DIR/api-sessions/<uuid>.json (default ~/.mars/api-sessions/, with
~/.modelmesh/ as a deprecated fallback if it already exists) — full
message history, replayed on each call.

Optional env vars:
  MARS_DIR                       override session storage root
                                 (legacy MODELMESH_DIR also accepted)
  MARS_HEARTBEAT_INTERVAL_SEC    progress-heartbeat interval (default 30)
                                 (legacy MODELMESH_HEARTBEAT_INTERVAL_SEC
                                 also accepted)
  OPENROUTER_REFERER             HTTP-Referer header sent to OpenRouter
                                 (analytics)
  OPENROUTER_TITLE               X-Title header sent to OpenRouter
                                 (analytics)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import warnings
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
import jwt
from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("mars")


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------

class SubprocessTimeout(RuntimeError):
    """A CLI subagent exceeded its timeout and its process tree was killed.

    Carries whatever the child wrote before the kill so tool wrappers can
    salvage a session id for the caller to resume with.
    """

    def __init__(
        self,
        message: str,
        partial_stdout: str = "",
        partial_stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.partial_stdout = partial_stdout
        self.partial_stderr = partial_stderr


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and every descendant it spawned.

    Plain Process.kill() terminates only the direct child. The CLIs we
    wrap are npm `.cmd` shims (kimi/codex: cmd.exe -> node.exe -> ...) or
    helper scripts that spawn their own children, so kill() left the
    actual agent loop running as an orphan that kept editing files long
    after we reported a timeout (observed with ask_kimi 2026-08-01: two
    timed-out calls kept writing for 21 and 44 more minutes). On Windows
    `taskkill /T /F` walks the child tree; on POSIX the child is its own
    session leader (start_new_session below) so killpg reaches the group.
    """
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    # Backstop for taskkill/killpg failure modes (already-exited race,
    # access denied): at minimum take down the direct child.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        proc.kill()


def _timeout_message(
    base: str, provider: str, session_id: Optional[str]
) -> str:
    """Timeout error text carrying the recovered session id (when there is
    one) so the caller can resume the interrupted work instead of
    restarting it from scratch."""
    if session_id:
        return (
            f"{base} Recovered session_id: {session_id} — the {provider} "
            f"session survived on disk; pass it back as session_id on the "
            f"next call to resume instead of restarting."
        )
    return (
        f"{base} No session_id could be recovered for the {provider} run; "
        f"a follow-up call must start fresh."
    )


async def _run_subprocess(
    args: list[str],
    timeout_sec: int,
    cwd: Optional[str] = None,
    stdin_data: Optional[str] = None,
) -> tuple[str, str]:
    """Run a command, return (stdout, stderr). Raises on non-zero exit.

    On timeout, kills the entire process tree (not just the direct child)
    and raises SubprocessTimeout carrying the partial stdout/stderr
    captured before the kill.
    """
    resolved = shutil.which(args[0])
    if resolved is None:
        raise RuntimeError(
            f"`{args[0]}` not found on PATH. Install it and re-run."
        )
    # On Windows, asyncio.create_subprocess_exec doesn't follow PATHEXT,
    # so npm-installed `.cmd` shims (codex, kimi) fail unless we pass
    # the fully-resolved path here.
    args = [resolved, *args[1:]]

    # Both `codex exec` and `gemini -p` have known bugs where they hang
    # waiting on stdin when run as a subprocess (gemini-cli #6715, #12362,
    # #13604; codex exec stdin handling on Windows). The fix is to either
    # send the prompt-via-stdin path or explicitly close stdin to DEVNULL.
    # We default to DEVNULL when no stdin_data is provided.
    stdin_arg = (
        asyncio.subprocess.PIPE
        if stdin_data is not None
        else asyncio.subprocess.DEVNULL
    )
    spawn_kwargs: dict = {}
    if sys.platform != "win32":
        # Own session => own process group, so _kill_process_tree can
        # killpg the whole tree instead of just the direct child.
        spawn_kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=stdin_arg,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        **spawn_kwargs,
    )

    # Drain stdout/stderr into buffers as the child runs (instead of
    # proc.communicate) so a timeout still has the partial output — the
    # only place a killed run's session id can be recovered from.
    stdout_buf = bytearray()
    stderr_buf = bytearray()

    async def _drain(stream: asyncio.StreamReader, buf: bytearray) -> None:
        while chunk := await stream.read(65536):
            buf.extend(chunk)

    io_tasks = [
        asyncio.create_task(_drain(proc.stdout, stdout_buf)),
        asyncio.create_task(_drain(proc.stderr, stderr_buf)),
    ]
    if stdin_data is not None:

        async def _feed_stdin() -> None:
            try:
                proc.stdin.write(stdin_data.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    proc.stdin.close()

        io_tasks.append(asyncio.create_task(_feed_stdin()))

    async def _settle_io(grace_sec: float) -> None:
        """Await IO tasks up to grace_sec, then cancel stragglers. The
        readers end at pipe EOF, which only arrives once every
        handle-holder in the child tree is gone — don't hang on a
        detached grandchild that inherited the pipes."""
        done, pending = await asyncio.wait(io_tasks, timeout=grace_sec)
        for t in done:
            with contextlib.suppress(Exception):
                t.exception()
        for t in pending:
            t.cancel()
        for t in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        _kill_process_tree(proc)
        await proc.wait()
        # Give the readers a beat to flush what the tree wrote pre-kill.
        await _settle_io(grace_sec=2.0)
        raise SubprocessTimeout(
            f"subagent timed out after {timeout_sec}s; "
            f"killed the whole process tree.",
            partial_stdout=stdout_buf.decode("utf-8", errors="replace"),
            partial_stderr=stderr_buf.decode("utf-8", errors="replace"),
        )

    await _settle_io(grace_sec=5.0)

    out = stdout_buf.decode("utf-8", errors="replace")
    err = stderr_buf.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        tail = err.strip() or out.strip() or "(no output)"
        raise RuntimeError(
            f"subagent exited with code {proc.returncode}:\n{tail}"
        )
    return out, err


# ---------------------------------------------------------------------------
# Codex session-id extraction
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
)
_CODEX_SESSION_RE = re.compile(
    r"(?:Codex session|thread[_ ]id|session[_ ]id|conversation[_ ]id)\s*"
    r"[:= ]\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)


def _extract_codex_session_id(stdout: str, stderr: str) -> Optional[str]:
    """Find the Codex thread/session UUID in CLI output."""
    for text in (stderr, stdout):
        m = _CODEX_SESSION_RE.search(text)
        if m:
            return m.group(1).lower()
    # fallback: first UUID anywhere — codex usually prints it on the first line
    for text in (stderr, stdout):
        m = _UUID_RE.search(text)
        if m:
            return m.group(1).lower()
    return None


# ---------------------------------------------------------------------------
# OpenAI-compatible HTTP chat
# ---------------------------------------------------------------------------

async def _openai_compatible_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    extra_headers: Optional[dict] = None,
    timeout_sec: int = 900,
) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"{base_url} returned {resp.status_code}: {resp.text[:600]}"
            )
        data = resp.json()

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"unexpected response shape: {data}") from e


# ---------------------------------------------------------------------------
# Progress heartbeat (parent-agent watchdog kept alive during slow calls)
# ---------------------------------------------------------------------------
# Thinking-mode reasoning models — DeepSeek V4-Pro, Grok 4.20-reasoning,
# Kimi K2.6, Gemini 3.1 Pro Preview, GLM-5.1 — routinely take 5–15
# minutes per call. Parent agents (e.g. Claude Code's stream watchdog)
# kill any agent that goes ~600s without emitting tool-call output.
# A long ask_* call spends that whole window awaiting the underlying
# API; the parent sees silence and gives up even though the call is
# making real progress.
#
# Fix: while the main API/CLI call is awaiting, emit MCP progress
# notifications every 30s. The parent's watchdog should count these as
# liveness signal. Notifications are protocol-level (not stdout text),
# so clients that don't understand them silently ignore.
#
# No-op when ctx is None (e.g. test harnesses that import the helpers
# directly without going through MCP).

def _get_heartbeat_interval_sec() -> float:
    """Read heartbeat interval from env, preferring MARS_HEARTBEAT_INTERVAL_SEC.
    Falls back to legacy MODELMESH_HEARTBEAT_INTERVAL_SEC with a
    DeprecationWarning. Default 30s.
    """
    if v := os.environ.get("MARS_HEARTBEAT_INTERVAL_SEC"):
        return float(v)
    if v := os.environ.get("MODELMESH_HEARTBEAT_INTERVAL_SEC"):
        warnings.warn(
            "MODELMESH_HEARTBEAT_INTERVAL_SEC is deprecated; use "
            "MARS_HEARTBEAT_INTERVAL_SEC instead. Legacy support will be "
            "removed in MARS v0.2.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return float(v)
    return 30.0


HEARTBEAT_INTERVAL_SEC = _get_heartbeat_interval_sec()


@contextlib.asynccontextmanager
async def _heartbeat_context(
    ctx: Optional[Context],
    provider: str,
    model: str,
    interval_sec: float = HEARTBEAT_INTERVAL_SEC,
) -> AsyncIterator[None]:
    """Emit progress notifications every interval_sec while the wrapped
    block runs. Cancels the heartbeat task on exit (success or error).
    Heartbeat exceptions are swallowed — the actual call must not crash
    because the watchdog ping failed.
    """
    if ctx is None:
        yield
        return

    async def _emit_loop() -> None:
        # Initial ping at 0 so the watchdog sees us start.
        try:
            await ctx.report_progress(
                progress=0.0,
                message=f"{provider}/{model}: starting...",
            )
        except Exception:
            pass
        elapsed = 0.0
        while True:
            try:
                await asyncio.sleep(interval_sec)
                elapsed += interval_sec
                await ctx.report_progress(
                    progress=elapsed,
                    message=(
                        f"{provider}/{model}: thinking... "
                        f"({int(elapsed)}s elapsed)"
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Heartbeat must never crash the actual call.
                pass

    task = asyncio.create_task(_emit_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# Shape of an env value the MCP host failed to expand: ${NAME} or
# ${NAME:-default}. Claude Code expands these in the server's `env` block
# on initial launch, but its MCP *reconnect* path has been observed passing
# the literal placeholder through (claude.exe 2.1.118, 2026-07-20 — same
# parent process, same config: 00:54 spawn expanded, 01:15 respawn did not).
_PLACEHOLDER_RE = re.compile(r"^\$\{(?P<name>\w+)(?::-(?P<default>[^}]*))?\}$")


def _registry_env(var: str) -> Optional[str]:
    """Read a persistent env var from the Windows registry (user scope
    first, then machine scope). Unlike the process environment — frozen
    at spawn time and corruptible by an unexpanded placeholder — the
    registry value is always current. Returns None off-Windows.
    """
    if sys.platform != "win32":
        return None
    import winreg

    for hive, subkey in (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                val, _ = winreg.QueryValueEx(key, var)
        except OSError:
            continue
        if val:
            return str(val)
    return None


def _optional_env(var: str) -> Optional[str]:
    """os.environ lookup that survives an unexpanded ${VAR} placeholder.

    If the process env holds a literal placeholder (or nothing), fall back
    to the OS-scope value so one bad spawn doesn't take the tool down for
    the life of the server process.
    """
    val = os.environ.get(var)
    if not val:
        return _registry_env(var)
    m = _PLACEHOLDER_RE.match(val)
    if m is None:
        return val
    name = m.group("name")
    if name != var:
        ref = os.environ.get(name)
        if ref and not _PLACEHOLDER_RE.match(ref):
            return ref
    return _registry_env(name) or _registry_env(var) or m.group("default")


def _require_env(var: str) -> str:
    val = _optional_env(var)
    if not val:
        raise RuntimeError(
            f"{var} is not set. Add it to the MCP server env in your "
            f"Claude Code config and reload."
        )
    return val


# ---------------------------------------------------------------------------
# API session storage (DeepSeek / OpenRouter / Grok)
# ---------------------------------------------------------------------------

def _get_mars_dir() -> Path:
    """Resolve MARS storage root, preferring new names and falling back
    to legacy ones with a DeprecationWarning so existing users keep
    their sessions until they migrate.

    Resolution order:
      1. MARS_DIR env var (new)
      2. MODELMESH_DIR env var (deprecated; emits DeprecationWarning)
      3. ~/.mars/ if it exists (new default)
      4. ~/.modelmesh/ if it exists (deprecated; emits DeprecationWarning)
      5. ~/.mars/ (new default, will be created by callers)
    """
    if path := os.environ.get("MARS_DIR"):
        return Path(path)
    if path := os.environ.get("MODELMESH_DIR"):
        warnings.warn(
            "MODELMESH_DIR is deprecated; use MARS_DIR instead. "
            "Legacy support will be removed in MARS v0.2.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return Path(path)
    new = Path.home() / ".mars"
    old = Path.home() / ".modelmesh"
    if not new.exists() and old.exists():
        warnings.warn(
            f"Found existing storage at {old}; this is the deprecated "
            f"~/.modelmesh location. Move it to {new} or set "
            f"MARS_DIR={old} to silence this warning. Legacy fallback "
            f"will be removed in MARS v0.2.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return old
    return new


API_SESSIONS_DIR = _get_mars_dir() / "api-sessions"

# Per-model context window guards. ~4 chars/token rough estimate. We trim
# history when it would push above this. The model's own response budget
# (max_tokens) sits on top of this, so leave headroom.
_MODEL_CONTEXT_HINT = {
    # DeepSeek direct
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    # Legacy aliases — DeepSeek deprecates 2026-07-24; both now route to
    # V4-Flash (non-thinking and thinking-mode respectively) with 1M ctx.
    "deepseek-chat": 1_000_000,
    "deepseek-reasoner": 1_000_000,
    # OpenRouter ids
    "deepseek/deepseek-v4-pro": 1_000_000,
    "deepseek/deepseek-v4-flash": 1_000_000,
    # Moonshot AI / Kimi via OpenRouter (added 2026-04-28)
    "moonshotai/kimi-k3": 256_000,  # current flagship (default set 2026-07-18)
    "moonshotai/kimi-k2.6": 256_000,
    "moonshotai/kimi-k2.5": 262_000,
    "moonshotai/kimi-latest": 262_000,
    # xAI Grok direct
    "grok-4.5": 1_000_000,  # current flagship (default set 2026-08-04); conservative pending xAI docs
    "grok-4.3": 1_000_000,  # prior flagship (added 2026-06-13)
    "grok-4-1-fast": 2_000_000,
    "grok-4-1-fast-latest": 2_000_000,
    "grok-4-1-fast-reasoning": 2_000_000,
    "grok-4-1-fast-non-reasoning": 2_000_000,
    "grok-code-fast-1": 256_000,
    "grok-4": 256_000,
    "grok-4-0709": 256_000,
    # 4.20 family (added 2026-04-27); context hints are conservative
    # estimates pending xAI docs confirmation — used for history trimming,
    # so understating is the safe direction.
    "grok-4.20-reasoning": 256_000,
    "grok-4.20-0309-reasoning": 256_000,
    "grok-4.20-0309-non-reasoning": 256_000,
    "grok-4.20-multi-agent-0309": 256_000,
    # Zhipu z.ai GLM family (added 2026-04-29); conservative 128K hints
    # pending z.ai docs confirmation per model.
    "glm-5.2": 128_000,  # mirrors glm-5.1; placeholder pending z.ai docs (default bumped 2026-07-20)
    "glm-5.1": 128_000,
    "glm-5": 128_000,
    "glm-5-turbo": 128_000,
    "glm-5v-turbo": 128_000,
    "glm-4.7": 128_000,
    "glm-4.7-flash": 128_000,
    "glm-4.6": 128_000,
    "glm-4.5": 128_000,
    # (Kimi / Moonshot direct entries removed 2026-08-01: ask_kimi now
    # routes through the kimi CLI, which manages its own context; the
    # moonshotai/* OpenRouter entries above still serve ask_openrouter.)
    # Xiaomi MiMo direct — Singapore plan (added 2026-05-23)
    "mimo-v2.5-pro": 256_000,
    "mimo-v2.5": 256_000,
    "mimo-v2-pro": 256_000,
    "mimo-v2-omni": 256_000,
}
_DEFAULT_CONTEXT_HINT = 100_000  # safe-ish for most OpenRouter models


# Per-model practical output ceilings — empirically observed maximums
# beyond which the provider gateway returns 504 / truncates / silently
# drops content, even when the model's *context window* and the
# tool-level max_tokens both nominally allow more. NOT enforced by
# MARS (the limits shift with provider load and aren't always
# stable enough to encode as hard caps); just discoverable in code so
# callers planning bulk-fanout work see the number alongside the
# context window.
#
# Rule of thumb: bulk fan-out (single call >10K visible output) is
# only reliable on grok-4.20-reasoning (xAI gateway holds). Other
# thinking-mode models — V4-Pro, GLM-5.1, Kimi K2.6, Gemini 3.1 Pro
# Preview — should be used in per-table / per-section fragmentation
# (5–10 small calls, each <16K output) for large work.
#
# Empirical sources for each value live in the inline comments below
# and in `wiki/patterns/subagent-orchestration.md` § "Per-model
# output budgets".

_MODEL_PRACTICAL_OUTPUT_CEILING = {
    # Z.AI / Zhipu — observed 504s and silent truncation on 60K-output
    # bulk-fanout requests; 4K-output per-table calls work clean.
    # Conservative ceiling to bias callers toward fragmentation.
    "glm-5.1": 16_000,
    "glm-5": 16_000,
    "glm-5-turbo": 16_000,
    "glm-5v-turbo": 16_000,
    "glm-4.7": 16_000,
    "glm-4.7-flash": 16_000,
    "glm-4.6": 16_000,
    "glm-4.5": 16_000,
    # Xiaomi MiMo — conservative 32K pending bulk-fanout evidence
    # against the Singapore endpoint.
    "mimo-v2.5-pro": 32_000,
    "mimo-v2.5": 32_000,
    "mimo-v2-pro": 32_000,
    "mimo-v2-omni": 32_000,
    # DeepSeek V4 — thinking-mode V4-Pro tolerates moderate outputs
    # but reasoning_content allocation eats budget; fragment beyond
    # ~32K output. V4-Flash is non-thinking, more headroom.
    "deepseek-v4-pro": 32_000,
    "deepseek-v4-flash": 64_000,
    "deepseek-chat": 64_000,         # V3 legacy alias → V4-Flash
    "deepseek-reasoner": 32_000,     # V3 legacy alias → V4-Pro thinking
    # xAI Grok — flagship reasoning empirically holds bulk-fanout
    # cleanly (5–15 min calls observed clean per chairman's runs).
    # Cheaper variants are similar shape but smaller context.
    "grok-4.5": 60_000,   # mirrors grok-4.3 pending bulk-fanout evidence
    "grok-4.3": 60_000,
    "grok-4.20-reasoning": 60_000,
    "grok-4.20-0309-reasoning": 60_000,
    "grok-4.20-0309-non-reasoning": 60_000,
    "grok-4.20-multi-agent-0309": 60_000,
    "grok-4-1-fast-reasoning": 60_000,
    "grok-4-1-fast": 60_000,
    "grok-4-1-fast-non-reasoning": 60_000,
    "grok-code-fast-1": 32_000,
    "grok-4": 32_000,
    "grok-4-0709": 32_000,
    # Moonshot Kimi via OpenRouter — thinking-mode; treat conservatively
    # pending bulk-fanout evidence (we've only verified small calls
    # work cleanly).
    "moonshotai/kimi-k3": 32_000,
    "moonshotai/kimi-k2.6": 32_000,
    "moonshotai/kimi-k2.5": 32_000,
    "moonshotai/kimi-latest": 32_000,
    # OpenRouter passthrough for DeepSeek — same as direct.
    "deepseek/deepseek-v4-pro": 32_000,
    "deepseek/deepseek-v4-flash": 64_000,
}
_DEFAULT_PRACTICAL_OUTPUT_CEILING = 16_000  # conservative when unknown


def _estimate_tokens(messages: list[dict]) -> int:
    chars = sum(len(m.get("content") or "") for m in messages)
    return chars // 4 + len(messages) * 4  # ~4 token overhead per message


def _trim_history(messages: list[dict], max_tokens: int) -> list[dict]:
    """Keep system messages; drop oldest user/assistant pairs if over cap."""
    if _estimate_tokens(messages) <= max_tokens:
        return messages
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    while rest and _estimate_tokens(system + rest) > max_tokens:
        # drop one pair (user + assistant); falls back to one message if odd
        rest = rest[2:] if len(rest) >= 2 else rest[1:]
    if _estimate_tokens(system + rest) > max_tokens:
        # last resort: keep only the final user message
        rest = rest[-1:] if rest else []
    print(
        f"[mars] trimmed session history to "
        f"~{_estimate_tokens(system + rest)} tokens",
        file=sys.stderr,
    )
    return system + rest


def _session_path(session_id: str) -> Path:
    # Defensive: only allow uuid-like ids in filenames.
    if not re.fullmatch(r"[0-9a-fA-F-]{8,40}", session_id):
        raise RuntimeError(f"invalid session_id: {session_id}")
    return API_SESSIONS_DIR / f"{session_id}.json"


def _load_api_session(session_id: str) -> dict:
    f = _session_path(session_id)
    if not f.exists():
        raise RuntimeError(
            f"session {session_id} not found in {API_SESSIONS_DIR}"
        )
    return json.loads(f.read_text(encoding="utf-8"))


def _save_api_session(session_id: str, data: dict) -> None:
    API_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    f = _session_path(session_id)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(f)  # atomic on same filesystem


async def _api_chat_with_session(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    system: Optional[str],
    max_tokens: int,
    session_id: Optional[str],
    extra_headers: Optional[dict] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Run an OpenAI-compatible chat with optional session persistence.

    If ctx is provided (FastMCP injects automatically when the calling
    tool declares `ctx: Context`), progress notifications are emitted
    every HEARTBEAT_INTERVAL_SEC while the slow API call awaits, keeping
    the parent agent's watchdog alive on thinking-mode runs that can
    take 5-15 minutes.
    """
    is_resume = session_id is not None
    if is_resume:
        sess = _load_api_session(session_id)
        messages: list[dict] = list(sess.get("messages", []))
        # Honor the locked-in model from the original session.
        model = sess.get("model", model)
        created_at = sess.get("created_at", time.time())
    else:
        session_id = str(uuid.uuid4())
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        created_at = time.time()

    messages.append({"role": "user", "content": prompt})

    ctx_cap = _MODEL_CONTEXT_HINT.get(model, _DEFAULT_CONTEXT_HINT)
    # Reserve room for the response.
    history_budget = max(ctx_cap - max_tokens, ctx_cap // 2)
    messages = _trim_history(messages, history_budget)

    async with _heartbeat_context(ctx, provider, model):
        text = await _openai_compatible_chat(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            extra_headers=extra_headers,
        )
    messages.append({"role": "assistant", "content": text})

    _save_api_session(
        session_id,
        {
            "session_id": session_id,
            "provider": provider,
            "model": model,
            "messages": messages,
            "created_at": created_at,
            "updated_at": time.time(),
        },
    )
    return {"output": text, "session_id": session_id}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Codex disk-brief workaround
# ---------------------------------------------------------------------------
# Codex CLI fresh sessions reject ~5KB structured prompts with messages like
# "send the skeleton you want filled in"; ~75% failure rate observed when
# inlining long structured prompts (3 of 4 fresh sessions either refused,
# returned empty arrays, or hallucinated a different schema). The pattern
# that empirically unblocks Codex is: write the brief to a .md file on
# disk, send Codex a one-liner "read FILE and execute it." We do this
# automatically when the prompt exceeds CODEX_BRIEF_THRESHOLD chars on a
# fresh session (resume sessions are locked to original context, no
# disk-brief). Threshold tunable via CODEX_BRIEF_THRESHOLD env var.
#
# The replacement prompt also tells Codex to emit outputs INLINE in its
# reply rather than try to Write to disk — Codex's sandbox blocks Write
# even at sandbox=danger-full-access, and MARS auto-overflows large
# tool results to a temp file the caller can read back.

CODEX_BRIEF_THRESHOLD = int(os.environ.get("CODEX_BRIEF_THRESHOLD", "3000"))


def _write_codex_brief(prompt: str) -> Path:
    """Write a long structured prompt to a temp file for Codex to read.

    Returns the absolute path. Caller is responsible for cleanup after
    Codex returns. The path is in the OS tempdir (not user cwd) so the
    write doesn't pollute project directories; Codex's sandbox allows
    arbitrary reads regardless of sandbox mode (workspace-write blocks
    writes outside cwd, but reads anywhere on disk are permitted).
    """
    tmp_dir = Path(tempfile.gettempdir()) / "mars-codex-briefs"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    brief_path = tmp_dir / f"brief-{uuid.uuid4().hex[:12]}.md"
    brief_path.write_text(prompt, encoding="utf-8")
    return brief_path


def _codex_disk_brief_replacement(brief_path: Path) -> str:
    """The one-liner Codex receives when we route through a disk brief."""
    return (
        f"Read the brief at {brief_path} and execute the task it describes. "
        "Emit all outputs INLINE in your reply (do not write files to disk; "
        "the caller will handle persistence). If the brief asks for "
        "structured output (YAML / JSON / markdown), emit it directly in "
        "your reply, properly formatted, with no commentary outside the "
        "structured block unless the brief asks for it."
    )


@mcp.tool()
async def ask_codex(
    prompt: str,
    model: str = "gpt-5.5",
    cwd: Optional[str] = None,
    sandbox: str = "workspace-write",
    timeout_sec: int = 600,
    session_id: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Run a prompt through the OpenAI Codex CLI as an agentic subagent.

    Continuity: this tool returns a session_id. To continue the same
    Codex conversation on a follow-up call, you MUST pass that
    session_id back. Omitting it starts a fresh agent that has no
    memory of prior turns. Only start fresh when the work is unrelated.

    The CLI runs its full agent loop (read files, edit, run shell commands)
    inside the chosen sandbox.

    Args:
        prompt: Task description for Codex.
        model: Codex model id. Default: "gpt-5.5" (OpenAI flagship,
            pinned 2026-04-30 — was CLI-deferred to
            whatever `codex` picks). Override per call:
              - "gpt-5" — prior generation
              - "o3" — reasoning-specialized
              - any other model id your Codex CLI auth has access to
        cwd: Working directory for Codex. Defaults to the MCP server's CWD.
        sandbox: One of "read-only", "workspace-write", "danger-full-access".
        timeout_sec: Hard kill (whole process tree) after this many
            seconds. Default 10 minutes. The timeout error includes a
            recovered session_id when one is available — pass it back
            to resume the interrupted session instead of restarting.
        session_id: Conversation continuity.
            - None: start a fresh session. The new UUID is returned.
            - "last": resume the most recent Codex session.
            - any UUID (or codex thread name): resume that exact session.

    Returns:
        {"output": str, "session_id": str | None}
        session_id is the UUID Codex used. Stash it; pass it back on the
        next call to keep the same conversation.
    """
    # Auto disk-brief: long structured prompts on fresh sessions fail
    # ~75% of the time (Codex rejects with "send the skeleton you want
    # filled in", returns empty arrays, or hallucinates a different
    # schema). Workaround that empirically unblocks: write to disk,
    # send one-liner "read FILE and execute". Resume sessions are locked
    # to the original prompt context, so disk-brief doesn't apply.
    brief_path: Optional[Path] = None
    effective_prompt = prompt
    if session_id is None and len(prompt) > CODEX_BRIEF_THRESHOLD:
        brief_path = _write_codex_brief(prompt)
        effective_prompt = _codex_disk_brief_replacement(brief_path)

    args = ["codex", "exec"]
    if session_id is not None:
        # `codex exec resume` does NOT accept -s (sandbox) or -C (cwd):
        # both are locked to the original session. Pass only what's valid.
        args.append("resume")
        if session_id == "last":
            args.append("--last")
        else:
            args.append(session_id)
        args.append("--skip-git-repo-check")
        if model:
            args.extend(["-m", model])
    else:
        args.extend(["--skip-git-repo-check", "-s", sandbox])
        if model:
            args.extend(["-m", model])
        if cwd:
            args.extend(["-C", cwd])
    args.append(effective_prompt)

    async with _heartbeat_context(ctx, "codex", model):
        try:
            stdout, stderr = await _run_subprocess(args, timeout_sec=timeout_sec)
        except SubprocessTimeout as e:
            # Codex prints its session/thread id near the start of the
            # run, so the partial output usually has it even on a kill.
            timeout_id = _extract_codex_session_id(
                e.partial_stdout, e.partial_stderr
            )
            if timeout_id is None and session_id and session_id != "last":
                timeout_id = session_id
            raise RuntimeError(
                _timeout_message(str(e), "codex", timeout_id)
            ) from None
        finally:
            # Best-effort cleanup of the brief file regardless of success/error.
            if brief_path is not None:
                try:
                    brief_path.unlink()
                except OSError:
                    pass

    resolved_id = _extract_codex_session_id(stdout, stderr)
    # If user passed an explicit UUID and we couldn't extract one, keep theirs.
    if resolved_id is None and session_id and session_id != "last":
        resolved_id = session_id

    return {"output": stdout.strip(), "session_id": resolved_id}


@mcp.tool()
async def ask_openrouter(
    prompt: str,
    model: str = "moonshotai/kimi-k3",
    system: Optional[str] = None,
    max_tokens: int = 100000,
    session_id: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Chat completion via OpenRouter, with multi-turn sessions.

    Continuity: this tool returns a session_id. To continue the same
    conversation on a follow-up call, you MUST pass that session_id
    back. Omitting it starts a fresh chat that has no memory of prior
    turns. Only start fresh when the work is unrelated.

    Args:
        prompt: User message.
        model: OpenRouter model id. Default: "moonshotai/kimi-k3"
            (Moonshot AI Kimi K3, current frontier; set as the default
            2026-07-18, superseding kimi-k2.6). Common alternatives:
              - "deepseek/deepseek-v4-pro" — 1M context, ~5× cheaper
                on output ($0.435/$0.87 with 75%-off through
                2026-05-05; ~$1.74/$3.48 full price after); right
                pick for cost-sensitive or long-context work
              - "moonshotai/kimi-latest" — auto-rolls to newest Kimi
                (K3 as of 2026-07-18); prefer the pinned "moonshotai/kimi-k3"
                id when you specifically want K3, since this alias moves
              - "moonshotai/kimi-k2.6" — prior Kimi flagship
                (2026-04-20; 256K ctx); fall back if k3 errors
              - "moonshotai/kimi-k2.5" — Jan 2026 Kimi; 262K ctx;
                $0.44/$2.00 per M tokens (cheaper Kimi alternative)
              - "anthropic/claude-sonnet-4.6" — strong code + writing
              - "anthropic/claude-opus-4.7" — deepest reasoning
              - "openai/gpt-5" — OpenAI perspective
              - "google/gemini-2.5-pro" — long-context Google
              - "x-ai/grok-4" — xAI via OR (use direct ask_grok if
                XAI_API_KEY is set; cheaper)
            On resume the model is locked to whatever was used
            originally and this argument is ignored.
        system: Optional system prompt. Used only on a fresh session;
            ignored on resume.
        max_tokens: Cap on response tokens for this turn.
        session_id: Pass None to start a new session (returns a UUID), or
            a UUID from a previous call to continue that conversation.
            History is replayed on each call; oldest turns are trimmed
            when total context approaches the model's window.

    Returns:
        {"output": str, "session_id": str}
        Stash session_id; pass it back to continue.
    """
    api_key = _require_env("OPENROUTER_API_KEY")
    headers: dict = {}
    if referer := os.environ.get("OPENROUTER_REFERER"):
        headers["HTTP-Referer"] = referer
    if title := os.environ.get("OPENROUTER_TITLE"):
        headers["X-Title"] = title
    return await _api_chat_with_session(
        ctx=ctx,
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        model=model,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        session_id=session_id,
        extra_headers=headers or None,
    )


@mcp.tool()
async def ask_deepseek(
    prompt: str,
    model: str = "deepseek-v4-pro",
    system: Optional[str] = None,
    max_tokens: int = 100000,
    session_id: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Chat completion via the DeepSeek API, with multi-turn sessions.

    Continuity: this tool returns a session_id. To continue the same
    conversation on a follow-up call, you MUST pass that session_id
    back. Omitting it starts a fresh chat that has no memory of prior
    turns. Only start fresh when the work is unrelated.

    Args:
        prompt: User message.
        model: DeepSeek model id. Default: "deepseek-v4-pro" (V4
            advanced reasoning, thinking-mode; $0.435/$0.87 per M tokens
            with 75% discount valid until 2026-05-05, then ~$1.74/$3.48
            full price; 1M context).
            Other choices:
              - "deepseek-v4-flash" — V4 fast tier; $0.14/$0.28 per M
                tokens cache-miss; supports both non-thinking and
                thinking modes; ~3× cheaper now / ~12× cheaper post-
                discount than V4-Pro; right pick for high-volume work
              - "deepseek-chat" / "deepseek-reasoner" — legacy aliases
                routing to V4-Flash non-thinking and thinking-mode
                respectively; deprecated 2026-07-24
            On resume the model is locked to whatever was used originally
            and this argument is ignored.
        system: Optional system prompt. Used only on a fresh session.
        max_tokens: Cap on response tokens for this turn. Default
            100000 (bumped 2026-04-29 from 16384, which itself replaced
            the original 4096 on 2026-04-28). All current thinking-mode
            defaults — V4-Pro, Kimi K2.6, Grok 4.20-reasoning — consume
            tokens on internal reasoning before producing visible
            output, and any cap below ~16K silently truncated real
            work. 100k is effectively "no cap" for any single response
            modern models will actually generate (most cap their own
            output at 8K–32K regardless of what's requested), and
            providers either accept and clamp or pass through cleanly.
            The cap is just a ceiling; you only pay for what's
            generated. Drop to ~512 for terse smoke-tests.
        session_id: Pass None to start a new session (returns a UUID), or
            a UUID from a previous call to continue that conversation.
            History is replayed each call; oldest turns are trimmed when
            context approaches the model's window (1M for V4 family).
            For deepseek-reasoner (legacy alias) and deepseek-v4-pro
            thinking mode: the chain-of-thought (reasoning_content) is
            intentionally NOT stored — only the final assistant message,
            per DeepSeek's guidance.

    Returns:
        {"output": str, "session_id": str}
        Stash session_id; pass it back to continue.

    Practical output budget: V4-Pro is thinking-mode and reserves
    significant tokens for internal reasoning before visible output.
    Bulk single-call output above ~32K visible tokens can degrade
    quality or get truncated; for large structured work, fragment
    into per-section calls. V4-Flash (non-thinking) holds longer
    output more reliably. For very long single-call output, prefer
    ask_grok with grok-4.20-reasoning.
    """
    api_key = _require_env("DEEPSEEK_API_KEY")
    return await _api_chat_with_session(
        ctx=ctx,
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key=api_key,
        model=model,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        session_id=session_id,
    )


@mcp.tool()
async def ask_grok(
    prompt: str,
    model: str = "grok-4.5",
    system: Optional[str] = None,
    max_tokens: int = 100000,
    session_id: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Chat completion via the xAI Grok API, with multi-turn sessions.

    Continuity: this tool returns a session_id. To continue the same
    conversation on a follow-up call, you MUST pass that session_id
    back. Omitting it starts a fresh chat that has no memory of prior
    turns. Only start fresh when the work is unrelated.

    Args:
        prompt: User message.
        model: xAI model id. Default: "grok-4.5" (xAI flagship; default
            bumped 2026-08-04 from grok-4.3 — fall back to "grok-4.3"
            if 4.5 errors on a given call).
            Other choices:
              - "grok-4.3" — prior flagship (2026-06-13; leads on
                non-hallucination rate, agentic tool-calling, and
                instruction following; 1M context; $1.25/$2.50 per M
                tokens in/out, $0.20/M cached input); the fallback
                when grok-4.5 errors
              - "grok-4.20-reasoning" — prior flagship reasoning variant
              - "grok-4.20-0309-reasoning" — date-stamped reasoning variant
              - "grok-4.20-0309-non-reasoning" — faster, lower latency
              - "grok-4.20-multi-agent-0309" — multi-agent / swarm reasoning
              - "grok-4-1-fast" / "grok-4-1-fast-reasoning" — 10× cheaper
                ($0.20/$0.50 per M); 2M context; good for high-volume work
              - "grok-4-1-fast-non-reasoning" — fast, no reasoning
              - "grok-code-fast-1" — agentic coding optimized (256K)
              - "grok-4" / "grok-4-0709" — older 256K-context model
            On resume the model is locked to whatever was used
            originally and this argument is ignored.
        system: Optional system prompt. Used only on fresh sessions.
        max_tokens: Cap on response tokens for this turn.
        session_id: Pass None to start a new session (returns a UUID), or
            a UUID from a previous call to continue that conversation.
            History is replayed each call; oldest turns are trimmed when
            context approaches the model's window.

    Returns:
        {"output": str, "session_id": str}
        Stash session_id; pass it back to continue.
    """
    api_key = _require_env("XAI_API_KEY")
    return await _api_chat_with_session(
        ctx=ctx,
        provider="grok",
        base_url="https://api.x.ai/v1",
        api_key=api_key,
        model=model,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        session_id=session_id,
    )


def _zai_jwt_token(api_key: str, exp_seconds: int = 3600) -> str:
    """Generate a JWT for z.ai's legacy "id.secret"-format API key.

    z.ai (Zhipu AI) keys come as "<key_id>.<secret>" and require client-side
    JWT signing — the gateway parses the Bearer header as a JWT, not as a
    raw key, despite some z.ai docs implying plain Bearer auth works on the
    paas/v4 endpoint. Raw-key Bearer fails with `{"code":"401","message":
    "token expired or incorrect"}` on first call. The official z-ai-sdk
    does this signing transparently; we do it explicitly here so the
    OpenAI-compatible chat-completions path still works.

    Algorithm: HS256 with the secret half of the key as the HMAC key.
    Headers: {"alg": "HS256", "sign_type": "SIGN"} — the sign_type header
    is required by z.ai's gateway and absent from the spec; copy from SDK.
    Claims: {"api_key": <key_id>, "exp": now_ms + exp_seconds*1000,
             "timestamp": now_ms} — note millisecond units, not seconds.
    """
    if "." not in api_key:
        raise RuntimeError(
            "ZAI_API_KEY must be in 'id.secret' format (legacy Zhipu shape)."
        )
    key_id, secret = api_key.split(".", 1)
    now_ms = int(round(time.time() * 1000))
    payload = {
        "api_key": key_id,
        "exp": now_ms + exp_seconds * 1000,
        "timestamp": now_ms,
    }
    return jwt.encode(
        payload,
        secret,
        algorithm="HS256",
        headers={"alg": "HS256", "sign_type": "SIGN"},
    )


@mcp.tool()
async def ask_zai(
    prompt: str,
    model: str = "glm-5.2",
    system: Optional[str] = None,
    max_tokens: int = 100000,
    session_id: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Chat completion via the z.ai (Zhipu AI) GLM API, with multi-turn sessions.

    Continuity: this tool returns a session_id. To continue the same
    conversation on a follow-up call, you MUST pass that session_id
    back. Omitting it starts a fresh chat that has no memory of prior
    turns. Only start fresh when the work is unrelated.

    Args:
        prompt: User message.
        model: z.ai model id. Default: "glm-5.2" (Zhipu AI flagship for
            coding + agent tasks; thinking-mode with separate
            reasoning_content stream — internal reasoning is NOT stored
            in session history; only the final assistant message is
            retained, mirroring DeepSeek V4-Pro and the deepseek-reasoner
            legacy alias. Set as the default 2026-06-17, superseding
            "glm-5.1" — fall back to "glm-5.1" if 5.2 errors on a given
            call).
            Other choices:
              - "glm-5.1" — prior flagship (superseded 2026-06-17)
              - "glm-5" — base GLM-5 without 5.1 refinements
              - "glm-5-turbo" — faster, lower latency variant
              - "glm-5v-turbo" — multimodal vision variant
              - "glm-4.7" / "glm-4.7-flash" — prior generation
              - "glm-4.6" / "glm-4.5" — older generations
            On resume the model is locked to whatever was used originally
            and this argument is ignored.
        system: Optional system prompt. Used only on a fresh session.
        max_tokens: Cap on response tokens for this turn. GLM-5.2 is
            thinking-mode (like 5.1) and consumes tokens on internal
            reasoning before producing visible output, so budget
            generously — the 100000 default is effectively no-cap for
            any single response GLM will actually generate.
        session_id: Pass None to start a new session (returns a UUID), or
            a UUID from a previous call to continue that conversation.
            History is replayed each call; oldest turns are trimmed when
            context approaches the model's window (conservative 128K
            hint pending z.ai per-model docs).

    Returns:
        {"output": str, "session_id": str}
        Stash session_id; pass it back to continue.

    Auth note: ZAI_API_KEY must be the legacy Zhipu "id.secret" format
    (32-char hex + dot + 17-char alphanum). The tool generates a fresh
    JWT per call (HS256-signed with the secret half) before sending; raw
    Bearer auth with the unsigned key fails on the paas/v4 endpoint.

    Practical output budget: Z.AI's gateway empirically truncates or
    returns 504 on bulk-output requests above ~16K visible output
    tokens. Per-table calls of ~4K output land cleanly. For large
    structured outputs (YAML / JSON >16K), fragment into 5–10 small
    per-section calls rather than one bulk-fanout request — the
    fragmented pattern is what empirically works on GLM. For bulk
    fan-out (single-call output >10K), prefer ask_grok with
    grok-4.20-reasoning, which holds long single-call output reliably.
    """
    api_key = _require_env("ZAI_API_KEY")
    jwt_token = _zai_jwt_token(api_key)
    return await _api_chat_with_session(
        ctx=ctx,
        provider="zai",
        base_url="https://api.z.ai/api/paas/v4",
        api_key=jwt_token,
        model=model,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        session_id=session_id,
    )


@mcp.tool()
async def ask_mimo(
    prompt: str,
    model: str = "mimo-v2.5-pro",
    system: Optional[str] = None,
    max_tokens: int = 100000,
    session_id: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Chat completion via the Xiaomi MiMo API, with multi-turn sessions.

    Continuity: this tool returns a session_id. To continue the same
    conversation on a follow-up call, you MUST pass that session_id
    back. Omitting it starts a fresh chat that has no memory of prior
    turns. Only start fresh when the work is unrelated.

    Args:
        prompt: User message.
        model: MiMo model id (lowercase). Default: "mimo-v2.5-pro" (flagship).
            The Singapore-plan endpoint accepts ONLY lowercase ids; any
            casing passed here is lowercased before forwarding. Other
            chat-capable choices:
              - "mimo-v2.5"     — non-Pro V2.5 tier
              - "mimo-v2-pro"   — previous-generation flagship
              - "mimo-v2-omni"  — multimodal V2-series
            TTS models ("mimo-v2.5-tts", "mimo-v2.5-tts-voiceclone",
            "mimo-v2.5-tts-voicedesign", "mimo-v2-tts") are NOT supported
            by this chat-completion tool; they require separate audio
            endpoints.
        system: Optional system prompt (fresh sessions only).
        max_tokens: Cap on response tokens. Default 100000.
        session_id: None for fresh session, UUID from prior call to resume.

    Endpoint: OpenAI-compatible /v1 on the Singapore plan
    (https://token-plan-sgp.xiaomimimo.com/v1).

    Returns:
        {"output": str, "session_id": str}
    """
    api_key = _require_env("MIMO_API_KEY")
    # Xiaomi MiMo Singapore endpoint accepts only lowercase model ids
    # (e.g. "mimo-v2.5-pro", not "MiMo-V2.5-Pro"). Normalize defensively so
    # any casing — including the PascalCase form older docs may suggest —
    # works without surfacing a 400 "Not supported model" to the caller.
    model = model.lower()
    return await _api_chat_with_session(
        ctx=ctx,
        provider="mimo",
        base_url="https://token-plan-sgp.xiaomimimo.com/v1",
        api_key=api_key,
        model=model,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Kimi Code CLI (kimi) — Moonshot K3 / K2.7 on the Kimi Code subscription
# ---------------------------------------------------------------------------

# Long or MULTI-LINE prompts go to a disk brief instead of the command
# line. Two transport limits force this: Windows CreateProcess caps the
# whole command line at ~32K chars, and the kimi CLI mangles any `-p`
# argument containing a newline (observed 0.31.1: it drops the prompt
# and prints its idle greeting — verified with single-line prompts
# passing and identical multi-line ones failing). Unlike the Codex
# brief (a model-behavior workaround), this is purely transport, so
# the length threshold is high. Tunable via KIMI_BRIEF_THRESHOLD.
KIMI_BRIEF_THRESHOLD = int(os.environ.get("KIMI_BRIEF_THRESHOLD", "20000"))

# The kimi CLI's own session store: sessions/wd_<dirname>_<hash>/session_<uuid>/
_KIMI_SESSIONS_DIR = Path.home() / ".kimi-code" / "sessions"


def _kimi_newest_session_since(
    start_ts: float, cwd: Optional[str]
) -> Optional[str]:
    """Recover the session id of a killed kimi run from the CLI's store.

    The stream-json resume hint is a trailing meta event, so a timed-out
    run usually dies before emitting it. The CLI does create its session
    directory (sessions/wd_<dirname>_<hash>/session_<uuid>/) at startup
    though, so the newest session dir touched after we launched
    identifies the orphan. Scoped to the cwd's workdir bucket when one
    matches, to dodge concurrent sessions in other directories.
    """
    try:
        wd_dirs = [d for d in _KIMI_SESSIONS_DIR.iterdir() if d.is_dir()]
    except OSError:
        return None
    slug = Path(cwd or os.getcwd()).name.lower()
    scoped = [d for d in wd_dirs if d.name.lower().startswith(f"wd_{slug}_")]
    if scoped:
        wd_dirs = scoped
    best: Optional[str] = None
    # Slack for coarse filesystem timestamps: the session dir appears
    # moments after our start timestamp.
    best_mtime = start_ts - 2.0
    for wd in wd_dirs:
        try:
            entries = list(wd.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith("session_"):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime >= best_mtime:
                best, best_mtime = entry.name, mtime
    return best


def _parse_kimi_stream(stdout: str) -> tuple[str, Optional[str]]:
    """Parse `kimi --output-format stream-json` output into (text, session_id).

    Each line is a JSON event, but tool stdout can leak into the stream
    as plain text (observed 0.31.1: a Bash tool call's output printed
    raw before its JSON event) — skip anything that doesn't parse as a
    JSON object. Assistant text arrives in "content" (tool-call events
    carry none); the session id arrives in a trailing meta event
    (type "session.resume_hint").
    """
    parts: list[str] = []
    session_id: Optional[str] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        role = event.get("role")
        if role == "assistant":
            content = event.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(content.strip())
        elif role == "meta" and event.get("session_id"):
            session_id = str(event["session_id"])
    return "\n\n".join(parts), session_id


@mcp.tool()
async def ask_kimi(
    prompt: str,
    model: str = "kimi-code/k3",
    system: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_sec: int = 600,
    session_id: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Run a prompt through the Kimi Code CLI (`kimi`) as an agentic subagent.

    Rerouted 2026-08-01: this tool now wraps the local `kimi` CLI
    (Moonshot's kimi-code, npm) instead of calling the Moonshot HTTPS
    API. Billing moved from per-token KIMI_API_KEY to the Kimi Code
    *subscription* (OAuth via `kimi login`; quota refreshes weekly).
    The CLI runs a full agent loop: it can read/write files and run
    shell commands in `cwd` (no sandbox — print mode auto-approves
    tool calls, observed 0.31.1), plus Moonshot web search / fetch.
    Prefer this over Kimi-via-ask_openrouter for repo-aware work.
    Note there is no max_tokens parameter (the CLI has no equivalent);
    output length is governed by the model itself.

    Continuity: this tool returns a session_id (kimi's own
    "session_<uuid>" form, stored under ~/.kimi-code/sessions). To
    continue the same conversation on a follow-up call, you MUST pass
    that session_id back. Omitting it starts a fresh session. Legacy
    API-era kimi sessions (bare UUIDs in ~/.mars/api-sessions/) are
    NOT resumable here — start fresh.

    Args:
        prompt: User message / task description.
        model: kimi CLI model alias (from ~/.kimi-code/config.toml).
            Default: "kimi-code/k3" (K3 flagship, 1M context, thinking;
            continues the K3 default set 2026-07-18). Others:
              - "kimi-code/k3-256k" — K3 at 262K context
              - "kimi-code/kimi-for-coding" — K2.7 Coding (the CLI's
                own default_model)
              - "kimi-code/kimi-for-coding-highspeed" — K2.7 fast lane
            Composable with resume (-S + -m verified on 0.31.1).
        system: Optional system prompt. The CLI has no system-prompt
            flag, so it is folded into the prompt as a <system>
            preamble on fresh sessions; ignored on resume.
        cwd: Workspace directory for the agent loop. Defaults to the
            MCP server's CWD.
        timeout_sec: Hard kill (whole process tree) after this many
            seconds. Default 10 minutes (K3 thinking at its default
            high effort can be slow to finish). The timeout error
            includes a recovered session_id when one is available —
            pass it back to resume instead of restarting.
        session_id: None for fresh, or a "session_..." id from a prior
            call to resume.

    Auth: the kimi CLI's own OAuth (`kimi login` device-code flow) —
    KIMI_API_KEY / KIMI_CODE_API_KEY are no longer read. If calls fail
    with an auth error, re-run `kimi login` in a real terminal.

    Returns:
        {"output": str, "session_id": str | None}
        output joins the assistant's text messages (tool-call chatter
        excluded). Stash session_id; pass it back to continue.
    """
    effective_prompt = prompt
    if system and session_id is None:
        effective_prompt = f"<system>\n{system}\n</system>\n\n{prompt}"

    brief_path: Optional[Path] = None
    if "\n" in effective_prompt or len(effective_prompt) > KIMI_BRIEF_THRESHOLD:
        brief_path = _write_codex_brief(effective_prompt)
        effective_prompt = (
            f"Read the file at {brief_path} — it contains the actual "
            "user message for this turn. Respond to that message "
            "directly (emit all outputs INLINE in your reply); do not "
            "comment on the file-reading step."
        )

    args = ["kimi"]
    if session_id:
        args.extend(["-S", session_id])
    if model:
        args.extend(["-m", model])
    args.extend(["-p", effective_prompt, "--output-format", "stream-json"])

    start_ts = time.time()
    async with _heartbeat_context(ctx, "kimi", model):
        try:
            stdout, _stderr = await _run_subprocess(
                args, timeout_sec=timeout_sec, cwd=cwd
            )
        except SubprocessTimeout as e:
            # On resume the id is already known; otherwise try the
            # partial stream (rarely has the trailing resume hint), then
            # the CLI's session store on disk.
            _partial_text, stream_id = _parse_kimi_stream(e.partial_stdout)
            timeout_id = (
                session_id
                or stream_id
                or _kimi_newest_session_since(start_ts, cwd)
            )
            raise RuntimeError(
                _timeout_message(str(e), "kimi", timeout_id)
            ) from None
        finally:
            if brief_path is not None:
                try:
                    brief_path.unlink()
                except OSError:
                    pass

    output, resolved_id = _parse_kimi_stream(stdout)
    if not output:
        # Defensive: if the stream had no assistant text (or the format
        # changes), surface the raw stdout rather than an empty reply.
        output = stdout.strip()
    return {"output": output, "session_id": resolved_id or session_id}


# ---------------------------------------------------------------------------
# Antigravity CLI (agy) — Claude Opus/Sonnet 4.6 + Gemini via Google AI Pro
# ---------------------------------------------------------------------------

_AGY_STATE_DIR = Path.home() / ".gemini" / "antigravity-cli"
_AGY_HELPER = Path(__file__).resolve().parent / "agy_pty_helper.py"


async def _agy_pty_run(args: list[str], timeout_sec: int, cwd: Optional[str]) -> str:
    """Run agy via the ConPTY helper process and return cleaned output.

    agy blocks forever with zero output when stdio is a pipe — every
    subcommand, including `-p` print mode, demands a real console. The
    helper provides one via pywinpty. It runs as a separate process
    because pywinpty's ConPTY cannot share a process with asyncio's
    Windows proactor loop (Overlapped deallocation kills the loop,
    observed 2026-06-11). See agy_pty_helper.py for the protocol.
    """
    if shutil.which("agy") is None:
        raise RuntimeError(
            "`agy` not found on PATH. Install the Antigravity CLI and sign in "
            "(Google OAuth) by running `agy` once in a real terminal."
        )
    payload = json.dumps({"args": args, "timeout_sec": timeout_sec, "cwd": cwd})
    # The helper enforces timeout_sec itself; give the subprocess wrapper
    # slack so the helper's clearer timeout message wins the race.
    stdout, _stderr = await _run_subprocess(
        [sys.executable, str(_AGY_HELPER)],
        timeout_sec=timeout_sec + 30,
        stdin_data=payload,
    )
    return json.loads(stdout)["output"]


def _agy_conversation_for_cwd(cwd: str) -> Optional[str]:
    """agy records cwd -> conversation-id in last_conversations.json."""
    cache = _AGY_STATE_DIR / "cache" / "last_conversations.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get(cwd)


def _agy_newest_conversation_since(start_ts: float) -> Optional[str]:
    """Fallback id discovery: newest conversation db touched after start."""
    conv_dir = _AGY_STATE_DIR / "conversations"
    best, best_mtime = None, start_ts
    try:
        for p in conv_dir.glob("*.db"):
            mtime = p.stat().st_mtime
            if mtime >= best_mtime:
                best, best_mtime = p.stem, mtime
    except OSError:
        return None
    return best


@mcp.tool()
async def ask_agy(
    prompt: str,
    model: str = "Claude Opus 4.6 (Thinking)",
    cwd: Optional[str] = None,
    timeout_sec: int = 600,
    session_id: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Run a prompt through the Google Antigravity CLI (`agy`) — the panelist
    route to Claude Opus 4.6 / Sonnet 4.6 (Thinking) and Gemini 3.5/3.1,
    billed to the Google AI Pro subscription instead of per-token APIs.

    Continuity: this tool returns a session_id (an agy conversation id).
    To continue the same conversation on a follow-up call, you MUST pass
    that session_id back. Omitting it starts a fresh conversation that has
    no memory of prior turns. Only start fresh when the work is unrelated.

    Args:
        prompt: User message.
        model: agy model label, passed verbatim (labels contain spaces and
            parentheses — keep them exact). Default:
            "Claude Opus 4.6 (Thinking)". Available labels (from
            `agy models`, 2026-06-10):
              - "Claude Opus 4.6 (Thinking)"   — deepest reasoning panelist
              - "Claude Sonnet 4.6 (Thinking)" — strong code + writing
              - "Gemini 3.5 Flash (Low|Medium|High)" — fast, tiered effort
              - "Gemini 3.1 Pro (Low|High)"    — Google advanced reasoning
              - "GPT-OSS 120B (Medium)"        — open-weights perspective
            Ignored on resume: agy locks the conversation to its original
            model.
        cwd: Working directory for agy. Defaults to the MCP server's CWD.
            Must be a trusted workspace (agy settings.json
            trustedWorkspaces), otherwise agy hangs on an interactive
            trust prompt until timeout.
        timeout_sec: Hard kill (whole process tree) after this many
            seconds. Default 10 minutes (thinking models can be slow to
            first token). The timeout error includes a recovered
            conversation id when one is available — pass it back as
            session_id to resume instead of restarting.
        session_id: None for a fresh conversation, or a conversation UUID
            from a previous call to resume it.

    Returns:
        {"output": str, "session_id": str | None}
        Stash session_id; pass it back to continue.

    Auth is the agy CLI's own Google OAuth (no API key env var). If calls
    start timing out with empty output, re-auth by launching `agy` in a
    visible terminal. Windows-only as implemented (ConPTY via pywinpty).
    """
    args = ["agy"]
    if session_id:
        args.extend(["--conversation", session_id])
    else:
        args.extend(["--model", model])
    args.extend(["-p", prompt, "--print-timeout", f"{max(timeout_sec - 15, 30)}s"])

    effective_cwd = str(Path(cwd or os.getcwd()).resolve())
    start_ts = time.time()
    try:
        async with _heartbeat_context(ctx, "agy", model):
            output = await _agy_pty_run(args, timeout_sec, effective_cwd)
    except RuntimeError as e:
        # Timeouts arrive two ways: SubprocessTimeout from the outer
        # wrapper (helper itself hung), or the helper's own exit-3
        # "agy timed out after Ns" surfaced as a nonzero-exit error.
        # Either way agy's conversation db was created at startup, so
        # the id is recoverable for a resume.
        if not (isinstance(e, SubprocessTimeout) or "timed out" in str(e)):
            raise
        timeout_id = (
            session_id
            or _agy_conversation_for_cwd(effective_cwd)
            or _agy_newest_conversation_since(start_ts)
        )
        raise RuntimeError(
            _timeout_message(str(e), "agy", timeout_id)
        ) from None

    resolved_id = (
        session_id
        or _agy_conversation_for_cwd(effective_cwd)
        or _agy_newest_conversation_since(start_ts)
    )
    return {"output": output, "session_id": resolved_id}


# ---------------------------------------------------------------------------
# Session admin tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_api_sessions(provider: Optional[str] = None) -> list[dict]:
    """List stored DeepSeek / OpenRouter / Grok / z.ai / mimo sessions, newest first.

    Args:
        provider: Filter to "deepseek", "openrouter", "grok", "zai", "mimo",
            or "kimi" (legacy API-era sessions only — since 2026-08-01
            ask_kimi routes through the kimi CLI, whose sessions live in
            ~/.kimi-code and don't appear here). None returns all.

    Returns:
        A list of session metadata dicts:
        {"session_id", "provider", "model", "turns",
         "created_at", "updated_at", "approx_tokens"}
    """
    if not API_SESSIONS_DIR.exists():
        return []
    out: list[dict] = []
    for f in API_SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if provider and data.get("provider") != provider:
            continue
        msgs = data.get("messages", [])
        out.append({
            "session_id": data.get("session_id"),
            "provider": data.get("provider"),
            "model": data.get("model"),
            "turns": sum(1 for m in msgs if m.get("role") == "user"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "approx_tokens": _estimate_tokens(msgs),
        })
    out.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return out


@mcp.tool()
async def delete_api_session(session_id: str) -> dict:
    """Delete a stored DeepSeek / OpenRouter / Grok / z.ai / mimo session
    (or a legacy API-era kimi session).

    Returns:
        {"deleted": bool, "session_id": str, "reason": str | None}
    """
    try:
        f = _session_path(session_id)
    except RuntimeError as e:
        return {"deleted": False, "session_id": session_id, "reason": str(e)}
    if not f.exists():
        return {"deleted": False, "session_id": session_id, "reason": "not found"}
    f.unlink()
    return {"deleted": True, "session_id": session_id, "reason": None}


def main() -> None:
    """Console-script entry point. Runs the MCP server on stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
