# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.2.0",
#   "httpx>=0.27.0",
# ]
# ///
"""ModelMesh MCP server.

Exposes four subagent tools to any MCP client (Claude Code, Cursor, etc.):
  - ask_codex     -> wraps the local `codex` CLI (agentic loop)
  - ask_gemini    -> wraps the local `gemini` CLI (agentic loop)
  - ask_openrouter-> chat completion via OpenRouter (multi-turn supported)
  - ask_deepseek  -> chat completion via DeepSeek API (multi-turn supported)

Plus admin tools:
  - list_api_sessions  -> enumerate stored DeepSeek/OpenRouter sessions
  - delete_api_session -> drop a stored session

Codex/Gemini inherit auth from their own CLIs (`codex login`, `gemini auth`).
OpenRouter/DeepSeek read keys from env: OPENROUTER_API_KEY, DEEPSEEK_API_KEY.

All four chat tools return: {"output": str, "session_id": str | None}.

Codex / Gemini sessions live where the CLI puts them (codex sqlite,
gemini chat files). DeepSeek / OpenRouter sessions live in
$MODELMESH_DIR/api-sessions/<uuid>.json (default
~/.modelmesh/api-sessions/) — full message history, replayed on
each call.

Optional env vars:
  MODELMESH_DIR   override session storage root
  OPENROUTER_REFERER  HTTP-Referer header sent to OpenRouter (analytics)
  OPENROUTER_TITLE    X-Title header sent to OpenRouter (analytics)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("modelmesh")


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
    timeout_sec: int = 180,
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


def _require_env(var: str) -> str:
    val = os.environ.get(var)
    if not val:
        raise RuntimeError(
            f"{var} is not set. Add it to the MCP server env in your "
            f"Claude Code config and reload."
        )
    return val


# ---------------------------------------------------------------------------
# API session storage (DeepSeek / OpenRouter)
# ---------------------------------------------------------------------------

API_SESSIONS_DIR = (
    Path(os.environ["MODELMESH_DIR"])
    if os.environ.get("MODELMESH_DIR")
    else Path.home() / ".modelmesh"
) / "api-sessions"

# Per-model context window guards. ~4 chars/token rough estimate. We trim
# history when it would push above this. The model's own response budget
# (max_tokens) sits on top of this, so leave headroom.
_MODEL_CONTEXT_HINT = {
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 64_000,
    "deepseek/deepseek-v4-pro": 1_000_000,
    "deepseek/deepseek-v4-flash": 1_000_000,
}
_DEFAULT_CONTEXT_HINT = 100_000  # safe-ish for most OpenRouter models


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
        f"[modelmesh] trimmed session history to "
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
) -> dict:
    """Run an OpenAI-compatible chat with optional session persistence."""
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

@mcp.tool()
async def ask_codex(
    prompt: str,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    sandbox: str = "workspace-write",
    timeout_sec: int = 600,
    session_id: Optional[str] = None,
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
        model: Override Codex's default model (e.g. "gpt-5", "o3").
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
    args.append(prompt)

    stdout, stderr = await _run_subprocess(args, timeout_sec=timeout_sec)

    resolved_id = _extract_codex_session_id(stdout, stderr)
    # If user passed an explicit UUID and we couldn't extract one, keep theirs.
    if resolved_id is None and session_id and session_id != "last":
        resolved_id = session_id

    return {"output": stdout.strip(), "session_id": resolved_id}


@mcp.tool()
async def ask_gemini(
    prompt: str,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    approval_mode: str = "yolo",
    timeout_sec: int = 600,
    session_id: Optional[str] = None,
) -> dict:
    """Run a prompt through the Google Gemini CLI as an agentic subagent.

    Continuity: this tool returns a session_id. To continue the same
    Gemini conversation on a follow-up call, you MUST pass that
    session_id back. Omitting it starts a fresh agent that has no
    memory of prior turns. Only start fresh when the work is unrelated.

    Args:
        prompt: Task description for Gemini.
        model: Override default Gemini model (e.g. "gemini-2.5-pro").
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
    model: str = "deepseek/deepseek-v4-pro",
    system: Optional[str] = None,
    max_tokens: int = 4096,
    session_id: Optional[str] = None,
) -> dict:
    """Chat completion via OpenRouter, with multi-turn sessions.

    Continuity: this tool returns a session_id. To continue the same
    conversation on a follow-up call, you MUST pass that session_id
    back. Omitting it starts a fresh chat that has no memory of prior
    turns. Only start fresh when the work is unrelated.

    Args:
        prompt: User message.
        model: OpenRouter model id. Default: "deepseek/deepseek-v4-pro"
            (1M context, strong general/reasoning, cheap). Override
            only when you need a specific other model.
            Common alternatives:
              - "anthropic/claude-sonnet-4.6" — strong code + writing
              - "anthropic/claude-opus-4.7" — deepest reasoning
              - "openai/gpt-5" — OpenAI perspective
              - "google/gemini-2.5-pro" — long-context Google
              - "x-ai/grok-4" — break out of consensus answers
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
    model: str = "deepseek-chat",
    system: Optional[str] = None,
    max_tokens: int = 4096,
    session_id: Optional[str] = None,
) -> dict:
    """Chat completion via the DeepSeek API, with multi-turn sessions.

    Continuity: this tool returns a session_id. To continue the same
    conversation on a follow-up call, you MUST pass that session_id
    back. Omitting it starts a fresh chat that has no memory of prior
    turns. Only start fresh when the work is unrelated.

    Args:
        prompt: User message.
        model: "deepseek-chat" (V3) or "deepseek-reasoner" (R1). On resume
            the model is locked to whatever was used originally.
        system: Optional system prompt. Used only on a fresh session.
        max_tokens: Cap on response tokens for this turn.
        session_id: Pass None to start a new session (returns a UUID), or
            a UUID from a previous call to continue that conversation.
            History is replayed each call; oldest turns are trimmed when
            context approaches the model's window (128K chat / 64K reasoner).
            For deepseek-reasoner: the chain-of-thought (reasoning_content)
            is intentionally NOT stored — only the final assistant message,
            per DeepSeek's guidance.

    Returns:
        {"output": str, "session_id": str}
        Stash session_id; pass it back to continue.
    """
    api_key = _require_env("DEEPSEEK_API_KEY")
    return await _api_chat_with_session(
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
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
    """List stored DeepSeek / OpenRouter sessions, newest first.

    Args:
        provider: Filter to "deepseek" or "openrouter". None returns both.

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
    """Delete a stored DeepSeek / OpenRouter session.

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
