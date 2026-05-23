# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.2.0",
#   "httpx>=0.27.0",
#   "pyjwt>=2.0.0",
# ]
# ///
"""MARS — Model Adapter Routing System (MCP server).

(Project was previously named ModelMesh; renamed 2026-05-04. The legacy
`modelmesh` console command and `MODELMESH_*` env vars continue to work
with a DeprecationWarning until MARS v0.2.0 — see CHANGELOG.)

Exposes eight subagent tools to any MCP client (Claude Code, Cursor, etc.):
  - ask_codex     -> wraps the local `codex` CLI (agentic loop)
  - ask_gemini    -> wraps the local `gemini` CLI (agentic loop)
  - ask_openrouter-> chat completion via OpenRouter (multi-turn supported)
  - ask_deepseek  -> chat completion via DeepSeek API (multi-turn supported)
  - ask_grok      -> chat completion via xAI Grok API (multi-turn supported)
  - ask_zai       -> chat completion via z.ai (Zhipu) GLM API (multi-turn supported)
  - ask_mimo      -> chat completion via Xiaomi MiMo API (multi-turn supported)
  - ask_kimi      -> chat completion via Kimi / Moonshot AI (multi-turn supported)

Plus admin tools:
  - list_api_sessions  -> enumerate stored DeepSeek/OpenRouter/Grok/z.ai/mimo/kimi sessions
  - delete_api_session -> drop a stored session

Codex/Gemini inherit auth from their own CLIs (`codex login`, `gemini auth`).
API tools read keys from env:
  - OPENROUTER_API_KEY for ask_openrouter
  - DEEPSEEK_API_KEY   for ask_deepseek
  - XAI_API_KEY        for ask_grok
  - ZAI_API_KEY        for ask_zai (legacy "id.secret" format; tool generates
                                    JWT per call, do NOT pre-sign)
  - MIMO_API_KEY       for ask_mimo (Xiaomi MiMo Singapore plan)
  - KIMI_API_KEY       for ask_kimi (Moonshot Open Platform, api.moonshot.ai)
  - KIMI_CODE_API_KEY  for ask_kimi when model="kimi-for-coding" (Kimi Code
                                    subscription, api.kimi.com/coding)

All eight chat tools return: {"output": str, "session_id": str | None}.

Codex / Gemini sessions live where the CLI puts them (codex sqlite,
gemini chat files). DeepSeek / OpenRouter / Grok / z.ai / mimo / kimi sessions live in
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

async def _run_subprocess(
    args: list[str],
    timeout_sec: int,
    cwd: Optional[str] = None,
    stdin_data: Optional[str] = None,
) -> tuple[str, str]:
    """Run a command, return (stdout, stderr). Raises on non-zero exit."""
    resolved = shutil.which(args[0])
    if resolved is None:
        raise RuntimeError(
            f"`{args[0]}` not found on PATH. Install it and re-run."
        )
    # On Windows, asyncio.create_subprocess_exec doesn't follow PATHEXT,
    # so npm-installed `.cmd` shims (codex, gemini) fail unless we pass
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
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=stdin_arg,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_data.encode() if stdin_data else None),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"subagent timed out after {timeout_sec}s")

    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")

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
# Gemini chat tracking
# ---------------------------------------------------------------------------

_GEMINI_FILE_RE = re.compile(r"session-.+-([0-9a-fA-F]{6,})\.jsonl$")


def _gemini_chats_dir() -> Optional[Path]:
    """Locate `~/.gemini/tmp/<user>/chats/` (varies by OS user)."""
    base = Path.home() / ".gemini" / "tmp"
    if not base.exists():
        return None
    candidates = [p / "chats" for p in base.iterdir() if (p / "chats").is_dir()]
    if not candidates:
        return None
    # Most-recently-touched wins if there are multiple users on the box.
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _gemini_chat_files() -> list[Path]:
    d = _gemini_chats_dir()
    if not d:
        return []
    return sorted(
        d.glob("session-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _gemini_id_from_filename(p: Path) -> Optional[str]:
    m = _GEMINI_FILE_RE.search(p.name)
    return m.group(1).lower() if m else None


def _resolve_gemini_index(session_id: str) -> Optional[int]:
    """Map our stable session_id (the file's hex suffix) to gemini's
    current 1-based mtime index. Returns None if no matching file."""
    sid = session_id.lower()
    for i, f in enumerate(_gemini_chat_files(), start=1):
        if (_gemini_id_from_filename(f) or "") == sid:
            return i
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


def _require_env(var: str) -> str:
    val = os.environ.get(var)
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
    "moonshotai/kimi-k2.6": 256_000,
    "moonshotai/kimi-k2.5": 262_000,
    "moonshotai/kimi-latest": 262_000,
    # xAI Grok direct
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
    "glm-5.1": 128_000,
    "glm-5": 128_000,
    "glm-5-turbo": 128_000,
    "glm-5v-turbo": 128_000,
    "glm-4.7": 128_000,
    "glm-4.7-flash": 128_000,
    "glm-4.6": 128_000,
    "glm-4.5": 128_000,
    # Kimi / Moonshot direct — api.moonshot.ai, api.kimi.com/coding (added 2026-05-23)
    "kimi-k2.6": 262_000,
    "kimi-k2.5": 262_000,
    "kimi-k2": 262_000,
    "kimi-for-coding": 262_000,
    "moonshot-v1-128k": 128_000,
    "moonshot-v1-32k": 32_000,
    "moonshot-v1-8k": 8_000,
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
        timeout_sec: Hard kill after this many seconds. Default 10 minutes.
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
async def ask_gemini(
    prompt: str,
    model: str = "gemini-3.1-pro-preview",
    cwd: Optional[str] = None,
    approval_mode: str = "yolo",
    timeout_sec: int = 600,
    session_id: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Run a prompt through the Google Gemini CLI as an agentic subagent.

    Continuity: this tool returns a session_id. To continue the same
    Gemini conversation on a follow-up call, you MUST pass that
    session_id back. Omitting it starts a fresh agent that has no
    memory of prior turns. Only start fresh when the work is unrelated.

    Args:
        prompt: Task description for Gemini.
        model: Gemini model id. Default: "gemini-3.1-pro-preview"
            (Gemini 3.1 Pro — preview status, not stable; latest
            advanced reasoning + agentic-coding tier). Override per
            call when needed:
              - "gemini-2.5-pro" — stable, no preview-rotation risk
              - "gemini-3-flash-preview" — cheaper, frontier-class
              - "gemini-3.1-flash-lite-preview" — efficiency variant
              - "gemini-3.1-flash-live-preview" — real-time / voice
            Preview ids are rotated by Google on quarterly cadence
            (-preview → -001 → deprecated); revisit if pinning here
            starts trailing the published preview id.
        cwd: Working directory. Defaults to the MCP server's CWD.
        approval_mode: One of "default", "auto_edit", "yolo", "plan".
            Must be non-"default" — subprocess cannot answer interactive
            prompts. "yolo" auto-approves all tools, "plan" is read-only.
        timeout_sec: Hard kill after this many seconds. Default 10 minutes.
        session_id: Conversation continuity.
            - None: start a fresh session. The new id is returned.
            - "last": resume the most recent session (gemini -r latest).
            - any hex id previously returned by this tool: resume that
              exact chat. The server resolves it to gemini's current
              session index by scanning ~/.gemini/tmp/<user>/chats/.

    Returns:
        {"output": str, "session_id": str | None}
        session_id is the hex suffix of gemini's chat file. Stash it;
        pass it back on the next call to keep the same conversation.

    Note: Gemini's CLI resumes by mtime-ordered index, not by stable id.
    The stability of session_id therefore depends on the chat file
    surviving on disk; if the user clears chat history, ids become invalid.

    Practical output budget: Gemini 3.1 Pro Preview is thinking-mode;
    bulk single-call outputs above ~32K visible tokens are unverified.
    For large structured work, prefer per-section fragmentation. Bulk
    fan-out (single-call >10K output) lands more reliably on ask_grok
    with grok-4.20-reasoning until Gemini's bulk-output ceiling is
    empirically pinned down.
    """
    if approval_mode == "default":
        raise RuntimeError(
            "approval_mode='default' would block on interactive prompts. "
            "Use 'yolo', 'auto_edit', or 'plan'."
        )

    args = [
        "gemini",
        "-p", prompt,
        f"--approval-mode={approval_mode}",
        "--output-format=text",
    ]
    if model:
        args.extend(["-m", model])

    target_known_id: Optional[str] = None
    if session_id is not None:
        if session_id == "last":
            args.extend(["-r", "latest"])
        else:
            idx = _resolve_gemini_index(session_id)
            if idx is None:
                raise RuntimeError(
                    f"gemini session_id '{session_id}' not found in "
                    f"~/.gemini/tmp/<user>/chats/. Pass 'last' or omit "
                    f"to start fresh."
                )
            target_known_id = session_id
            args.extend(["-r", str(idx)])

    chats_before = {p.name for p in _gemini_chat_files()}
    async with _heartbeat_context(ctx, "gemini", model):
        stdout, _ = await _run_subprocess(args, timeout_sec=timeout_sec, cwd=cwd)

    # Resolve the resulting session_id.
    if target_known_id is not None:
        # Resume kept the same file → id is stable.
        resolved_id: Optional[str] = target_known_id
    else:
        after = _gemini_chat_files()
        new_files = [p for p in after if p.name not in chats_before]
        chosen = new_files[0] if new_files else (after[0] if after else None)
        resolved_id = _gemini_id_from_filename(chosen) if chosen else None

    return {"output": stdout.strip(), "session_id": resolved_id}


@mcp.tool()
async def ask_openrouter(
    prompt: str,
    model: str = "moonshotai/kimi-k2.6",
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
        model: OpenRouter model id. Default: "moonshotai/kimi-k2.6"
            (Moonshot AI Kimi 2.6, released 2026-04-20; 256K context;
            $0.7448/$4.655 per M tokens in/out; thinking-mode).
            Common alternatives:
              - "deepseek/deepseek-v4-pro" — 1M context, ~5× cheaper
                on output ($0.435/$0.87 with 75%-off through
                2026-05-05; ~$1.74/$3.48 full price after); right
                pick for cost-sensitive or long-context work
              - "moonshotai/kimi-latest" — auto-rolls to newest Kimi
                (currently K2.6); use when you want to track latest
                without manual id updates
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
    model: str = "grok-4.20-reasoning",
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
        model: xAI model id. Default: "grok-4.20-reasoning" (xAI
            flagship "best overall — recommended"; reasoning variant;
            $2/$6 per M tokens in/out at 2026-04-27).
            Other choices:
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
    model: str = "glm-5.1",
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
        model: z.ai model id. Default: "glm-5.1" (Zhipu AI flagship for
            coding + agent tasks; thinking-mode with separate
            reasoning_content stream — internal reasoning is NOT stored
            in session history; only the final assistant message is
            retained, mirroring DeepSeek V4-Pro and the deepseek-reasoner
            legacy alias).
            Other choices:
              - "glm-5" — base GLM-5 without 5.1 refinements
              - "glm-5-turbo" — faster, lower latency variant
              - "glm-5v-turbo" — multimodal vision variant
              - "glm-4.7" / "glm-4.7-flash" — prior generation
              - "glm-4.6" / "glm-4.5" — older generations
            On resume the model is locked to whatever was used originally
            and this argument is ignored.
        system: Optional system prompt. Used only on a fresh session.
        max_tokens: Cap on response tokens for this turn. GLM-5.1 is
            thinking-mode and consumes tokens on internal reasoning
            before producing visible output (~70 reasoning tokens even
            for trivial prompts), so budget generously — the 100000
            default is effectively no-cap for any single response GLM
            will actually generate.
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
    conversation on a follow-up call, you MUST pass that session_id back.

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


@mcp.tool()
async def ask_kimi(
    prompt: str,
    model: str = "kimi-k2.6",
    system: Optional[str] = None,
    max_tokens: int = 100000,
    session_id: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Chat completion via Kimi (Moonshot AI), with multi-turn sessions.

    Continuity: this tool returns a session_id. To continue the same
    conversation on a follow-up call, you MUST pass that session_id back.

    Args:
        prompt: User message.
        model: Kimi model id. Default: "kimi-k2.6" (Moonshot flagship,
            served from the Open Platform endpoint https://api.moonshot.ai/v1,
            billed per token). Other Moonshot ids: "kimi-k2.5",
            "moonshot-v1-8k" / "moonshot-v1-32k" / "moonshot-v1-128k".
            Special: model="kimi-for-coding" routes to the Kimi Code
            *subscription* endpoint (https://api.kimi.com/coding/v1) and
            requires KIMI_CODE_API_KEY instead of KIMI_API_KEY.
            Note: kimi-k2.5/k2.6 only accept temperature=1; MARS never
            sends a temperature field, so the model's own default applies.
        system: Optional system prompt (fresh sessions only).
        max_tokens: Cap on response tokens. Default 100000.
        session_id: None for fresh session, UUID from prior call to resume.

    Auth: KIMI_API_KEY (Moonshot Open Platform console) for every model
    except "kimi-for-coding", which uses KIMI_CODE_API_KEY (Kimi Code
    Console; tied to a Kimi membership, quota refreshes every 7 days).

    Returns:
        {"output": str, "session_id": str}
    """
    if model == "kimi-for-coding":
        # Dormant route: the Kimi Code *subscription* endpoint. Only usable if
        # the operator has explicitly provisioned a Kimi Code Console key.
        # Guarded so it never silently misroutes during normal operation —
        # the working/default route is Moonshot + kimi-k2.6 below.
        if not os.environ.get("KIMI_CODE_API_KEY"):
            raise RuntimeError(
                "model='kimi-for-coding' targets the Kimi Code subscription "
                "endpoint, which is NOT configured (KIMI_CODE_API_KEY unset). "
                "Use the default model 'kimi-k2.6' (Moonshot Open Platform), "
                "which is the active route, or set KIMI_CODE_API_KEY first."
            )
        base_url = "https://api.kimi.com/coding/v1"
        api_key = os.environ["KIMI_CODE_API_KEY"]
    else:
        # Default / active route: Moonshot Open Platform (api.moonshot.ai),
        # authenticated by KIMI_API_KEY. This is the only configured path.
        base_url = "https://api.moonshot.ai/v1"
        api_key = _require_env("KIMI_API_KEY")
    return await _api_chat_with_session(
        ctx=ctx,
        provider="kimi",
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Session admin tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_api_sessions(provider: Optional[str] = None) -> list[dict]:
    """List stored DeepSeek / OpenRouter / Grok / z.ai / mimo / kimi sessions, newest first.

    Args:
        provider: Filter to "deepseek", "openrouter", "grok", "zai", "mimo",
            or "kimi". None returns all.

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
    """Delete a stored DeepSeek / OpenRouter / Grok / z.ai / mimo / kimi session.

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
