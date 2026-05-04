# MARS

**Model Adapter & Routing System.** An MCP server that lets Claude Code (or any MCP client) delegate work to other LLMs as subagents — preserving conversation continuity across turns via stable session IDs. Part of the Fr4ym + MARS + BCKS stack.

> **Renamed from ModelMesh on 2026-05-04.** The legacy `modelmesh` console command, `MODELMESH_*` env vars, and `~/.modelmesh/` storage path continue to work with a `DeprecationWarning` through MARS v0.2.0. See [CHANGELOG.md](./CHANGELOG.md) for the full migration path.

Wraps six backends:

| Tool | Backend | Auth |
|---|---|---|
| `ask_codex` | Local [`codex`](https://developers.openai.com/codex/cli) CLI (full agent loop) | `codex login` |
| `ask_gemini` | Local [`gemini`](https://geminicli.com) CLI (full agent loop) | `gemini auth` |
| `ask_openrouter` | OpenRouter HTTPS API (any model) | `OPENROUTER_API_KEY` env |
| `ask_deepseek` | DeepSeek HTTPS API | `DEEPSEEK_API_KEY` env |
| `ask_grok` | xAI Grok HTTPS API | `XAI_API_KEY` env |
| `ask_zai` | Z.AI (Zhipu) HTTPS API — JWT auth handled internally | `ZAI_API_KEY` env (legacy `id.secret` shape) |

Plus admin tools: `list_api_sessions`, `delete_api_session`.

---

## Why this exists

Claude Code is great, but sometimes you want a second opinion from GPT-5.5, or to delegate a long task to Gemini's 1M-context model, or to run cheap reasoning on DeepSeek-V4-Flash, or to triangulate against Z.AI's GLM-5.1. Doing this by hand — switching tools, copy-pasting context, losing thread — is friction.

MARS turns those LLMs into first-class tools Claude can call mid-conversation. Each call returns a `session_id` you can pass back on the next call to keep the conversation going. Codex/Gemini sessions persist in their own native stores; DeepSeek/OpenRouter/Grok/Z.AI conversations are kept in a local JSON store and replayed on each call.

---

## Install

### Prerequisites

- Python 3.10+ (PyJWT is now a dependency — `pip install` handles it; `uv run` reads it from PEP 723 metadata)
- For `ask_codex`: install and authenticate [Codex CLI](https://developers.openai.com/codex/cli) (`npm install -g @openai/codex` then `codex login`)
- For `ask_gemini`: install and authenticate [Gemini CLI](https://geminicli.com) (`npm install -g @google/gemini-cli` then `gemini auth`)
- For `ask_openrouter`: an [OpenRouter API key](https://openrouter.ai/settings/keys)
- For `ask_deepseek`: a [DeepSeek API key](https://platform.deepseek.com/api_keys)
- For `ask_grok`: an [xAI API key](https://console.x.ai/)
- For `ask_zai`: a [Z.AI API key](https://platform.kimi.ai/) (legacy `id.secret` format — the tool generates the required JWT internally; do NOT pre-sign)

### Install the server

```bash
git clone https://github.com/asakur44/mars.git
cd mars
pip install .
```

This puts a `mars` console command on your PATH. (The legacy `modelmesh` console command is also installed for backwards compatibility through MARS v0.2.0.)

(Alternative: if you have [`uv`](https://docs.astral.sh/uv/) installed, you can skip `pip install` and use `uv run server.py` — the inline PEP 723 metadata handles deps including `pyjwt>=2.0.0` for `ask_zai`.)

### Register with Claude Code

```bash
claude mcp add --scope user mars \
  --env OPENROUTER_API_KEY=sk-or-... \
  --env DEEPSEEK_API_KEY=sk-... \
  --env XAI_API_KEY=xai-... \
  --env ZAI_API_KEY=<key_id>.<secret> \
  -- mars
```

`--scope user` makes it available across all your projects. Drop `--env` flags for any provider you don't have a key for; the corresponding tools will return a clean error when called instead of crashing the server.

In Claude Code, run `/mcp` to confirm `mars` is connected. Eight tools should now be callable (six chat subagents + two admin tools).

### Register with other MCP clients

Cursor, Windsurf, VS Code MCP, etc. all accept stdio MCP servers. Point them at the `mars` command with the same env vars.

---

## Usage

### Basic call

```python
ask_openrouter(prompt="explain memoization briefly")
# → {"output": "...", "session_id": "a1b2c3d4-..."}
```

### Multi-turn continuity

The whole point. Capture `session_id`, pass it back:

```python
r1 = ask_deepseek(prompt="propose a schema for a vehicle inspection finding")
sid = r1["session_id"]

r2 = ask_deepseek(prompt="now critique it", session_id=sid)   # remembers turn 1
r3 = ask_deepseek(prompt="rewrite for postgres", session_id=sid)  # remembers 1 & 2
```

For Codex/Gemini (agentic CLIs):

```python
r1 = ask_codex(prompt="implement parser in src/parser.py", cwd="/path/to/project")
sid = r1["session_id"]   # codex thread UUID

# resume later — even after other codex calls happened in between
ask_codex(prompt="now write tests for it", session_id=sid)
```

### Choosing a model

All six chat subagents have explicit string defaults — no "depends on local CLI" footnotes. Override per call when you want something else.

`ask_openrouter` defaults to `moonshotai/kimi-k2.6` (Moonshot AI Kimi 2.6, 256K context, thinking-mode). Override:

```python
ask_openrouter(prompt="...", model="deepseek/deepseek-v4-pro")    # 1M ctx, ~5× cheaper on output
ask_openrouter(prompt="...", model="anthropic/claude-sonnet-4.6")
ask_openrouter(prompt="...", model="openai/gpt-5.5")
ask_openrouter(prompt="...", model="x-ai/grok-4.20-reasoning")
```

`ask_deepseek` defaults to `deepseek-v4-pro` (V4 advanced reasoning, thinking-mode). Drop to V4-Flash for high-volume / cost-sensitive work:

```python
ask_deepseek(prompt="...", model="deepseek-v4-flash")
```

Legacy aliases `deepseek-chat` / `deepseek-reasoner` route to V4-Flash non-thinking / thinking-mode respectively; both deprecated 2026-07-24.

`ask_grok` defaults to `grok-4.20-reasoning` (xAI flagship reasoning). Other choices:

```python
ask_grok(prompt="...", model="grok-4-1-fast-reasoning")  # 10× cheaper, 2M ctx, high-volume
ask_grok(prompt="...", model="grok-code-fast-1")          # agentic coding (256K)
```

`ask_gemini` defaults to `gemini-3.1-pro-preview` (preview tier — Google rotates `-preview` ids on roughly quarterly cadence; revisit when it gets promoted). Override to a stable tier when you don't want preview-rotation risk:

```python
ask_gemini(prompt="...", model="gemini-2.5-pro")
```

`ask_zai` defaults to `glm-5.1` (Zhipu AI flagship; thinking-mode with separate `reasoning_content` stream like `deepseek-reasoner`). Other GLM variants:

```python
ask_zai(prompt="...", model="glm-5-turbo")    # faster, lower latency
ask_zai(prompt="...", model="glm-4.7")         # prior generation
```

`ask_codex` defaults to `gpt-5.5` (OpenAI flagship). Override:

```python
ask_codex(prompt="...", model="gpt-5")
ask_codex(prompt="...", model="o3")
```

---

## Telling Claude to use session IDs

Claude Code won't automatically capture session IDs unless you teach it to. Add this to your global `~/.claude/CLAUDE.md` (creates if missing):

```markdown
## mars — session_id continuity

The MCP server `mars` exposes `ask_codex`, `ask_gemini`,
`ask_deepseek`, `ask_openrouter`, `ask_grok`, `ask_zai`. Each returns
`{"output": str, "session_id": str | None}`.

**Rule:** Treat the returned `session_id` as load-bearing.
- Capture it from every subagent call.
- On any follow-up call continuing the same task, pass it back as
  the `session_id` argument.
- Only omit it (start fresh) when the work is genuinely unrelated.
- Surface the active `session_id` in your visible response the first
  time you receive it, so it remains addressable later.
```

This loads in every Claude Code session in every project.

### (optional) Orchestration discipline for sub-agent loops

If you use Claude (or any other agent) to spawn sub-agents that themselves call MARS — parallel fan-out, structured-data extraction, multi-vendor critique passes — add this second snippet alongside the session-id one. It encodes the failure modes that MARS can't engineer around (provider-side gateway limits, parent-agent dispatch hygiene, late-write recovery):

```markdown
## mars — orchestration discipline

When spawning sub-agents that call MARS, apply these to avoid
silent failures:

**Pick the model for the shape of work:**
- Bulk fan-out (single call >10K output): `ask_grok` —
  `grok-4.20-reasoning` empirically holds long single-call output;
  xAI is the only gateway that reliably delivers it.
- Per-section structured outputs (small per-table calls):
  `ask_zai` / `ask_deepseek` / `ask_gemini` are fine. For GLM-5.1
  specifically, fragment large work into 5–10 per-table calls —
  Z.AI gateway truncates or 504s on bulk requests >~16K output.
- Heavy agentic work in a repo: `ask_codex` (MARS handles the
  disk-brief workaround for long structured prompts automatically).

**Dispatch hygiene:**
- **Never describe planned tool calls in prose.** Emit `tool_use`
  blocks directly. If you find yourself writing "dispatching X" or
  "calling Y", stop and emit the actual calls.
- After spawning a child agent that should make N calls, count
  `tool_use` blocks in its transcript. If <N, the agent silently
  no-op'd — re-prompt explicitly with the count assertion.
- For parallel fan-out, 1–2 MARS calls per child agent.
  Multiple sequential calls within one child compound watchdog risk
  (heartbeats every 30s help, but the cumulative window adds up).

**When a child appears killed mid-call:**
- Check the expected output file 5–10 minutes after the kill before
  discarding the work. Thinking-mode calls (V4-Pro, GLM-5.1,
  Grok-4.20-reasoning) often complete and write to disk *after* the
  parent watchdog timestamp. Re-read the file and recover the
  artifact rather than retrying.
```

---

## Configuration

All optional. Set in the `--env` flags when registering, or in your shell environment.

| Env var | Purpose | Default |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API key | required for `ask_deepseek` |
| `OPENROUTER_API_KEY` | OpenRouter API key | required for `ask_openrouter` |
| `XAI_API_KEY` | xAI Grok API key | required for `ask_grok` |
| `ZAI_API_KEY` | Z.AI (Zhipu) API key in legacy `id.secret` format — tool generates JWT internally | required for `ask_zai` |
| `MARS_DIR` | Where to store API session files | `~/.mars/` |
| `MARS_HEARTBEAT_INTERVAL_SEC` | Progress-heartbeat interval (seconds) | `30` |
| `OPENROUTER_REFERER` | `HTTP-Referer` header sent to OpenRouter (analytics attribution) | omitted |
| `OPENROUTER_TITLE` | `X-Title` header sent to OpenRouter | omitted |

**Legacy env vars** (deprecated, removed in MARS v0.2.0): `MODELMESH_DIR` and `MODELMESH_HEARTBEAT_INTERVAL_SEC` are still read if the new names are unset, with a `DeprecationWarning`. Existing storage at `~/.modelmesh/` is also auto-detected and used as a fallback if `~/.mars/` doesn't exist yet — same warning. Migrate with `mv ~/.modelmesh ~/.mars` to silence.

---

## Tool reference

### `ask_codex(prompt, model?, cwd?, sandbox?, timeout_sec?, session_id?)`

Runs the Codex CLI's full agent loop (read files, edit, run shell commands) inside a sandbox.

- `model`: default `"gpt-5.5"`. Override to `"gpt-5"`, `"o3"`, or any model id your Codex CLI auth has access to.
- `sandbox`: `"read-only"` | `"workspace-write"` (default) | `"danger-full-access"`
- `cwd`: working directory for Codex (default: server's CWD)
- `session_id`: pass `None` for fresh, `"last"` for most recent, or any UUID/thread name to resume that exact session

### `ask_gemini(prompt, model?, cwd?, approval_mode?, timeout_sec?, session_id?)`

Runs the Gemini CLI as an agent.

- `model`: default `"gemini-3.1-pro-preview"`. Preview tier — Google rotates `-preview` ids quarterly; revisit when it promotes. Override to `"gemini-2.5-pro"` for stable.
- `approval_mode`: `"yolo"` (default) | `"auto_edit"` | `"plan"` (read-only) — must NOT be `"default"` (would block on prompts)
- `session_id`: pass `None` for fresh, `"last"` for most recent, or a hex id previously returned by this tool

Note: Gemini's CLI resumes by mtime-ordered index, not by stable id. The server resolves your hex id to the current index by scanning `~/.gemini/tmp/<user>/chats/`. IDs are stable as long as the chat file isn't deleted.

### `ask_openrouter(prompt, model?, system?, max_tokens?, session_id?)`

Stateless API calls + local session replay.

- `model`: default `"moonshotai/kimi-k2.6"` (Moonshot AI Kimi 2.6; 256K context; thinking-mode). Pass any [OpenRouter model id](https://openrouter.ai/models) to override.
- `max_tokens`: default `100000` — effectively no-cap; OpenRouter clamps to per-model `max_completion_tokens` server-side. The cap is a ceiling, not a charge.
- `system`: optional system prompt (used only on fresh sessions)
- `session_id`: pass `None` for fresh, or a UUID from a previous call

History is replayed each call; oldest pairs are trimmed when context approaches the model's window.

### `ask_deepseek(prompt, model?, system?, max_tokens?, session_id?)`

- `model`: default `"deepseek-v4-pro"` (V4 advanced reasoning, thinking-mode; $0.435/$0.87 per M tokens with 75% discount through 2026-05-05, then ~$1.74/$3.48). Drop to `"deepseek-v4-flash"` ($0.14/$0.28 per M, ~3× cheaper) for high-volume work. Legacy `"deepseek-chat"` / `"deepseek-reasoner"` route to V4-Flash non-thinking / thinking-mode; deprecated 2026-07-24.
- `max_tokens`: default `100000`. V4-Pro is thinking-mode and consumes tokens on internal reasoning before producing visible output; budget generously.
- For thinking-mode (V4-Pro and `deepseek-reasoner`): `reasoning_content` (CoT) is intentionally NOT stored, per DeepSeek's guidance — only the final assistant message goes into history.

### `ask_grok(prompt, model?, system?, max_tokens?, session_id?)`

- `model`: default `"grok-4.20-reasoning"` (xAI flagship reasoning; $2/$6 per M tokens). Drop to `"grok-4-1-fast-reasoning"` ($0.20/$0.50 per M, 2M ctx) for high-volume work. Other ids: `"grok-code-fast-1"` (256K coding-tuned), `"grok-4"` / `"grok-4-0709"` (older 256K).
- `max_tokens`: default `100000`.
- `session_id`: same shape as the other API tools — None for fresh, a UUID from a previous call to resume.

### `ask_zai(prompt, model?, system?, max_tokens?, session_id?)`

Z.AI (Zhipu AI) GLM API. Default model `glm-5.1` (flagship; thinking-mode with separate `reasoning_content` stream like `deepseek-reasoner`).

- `model`: default `"glm-5.1"`. Other ids: `"glm-5"`, `"glm-5-turbo"` (faster), `"glm-5v-turbo"` (multimodal vision), `"glm-4.7"` / `"glm-4.7-flash"`, `"glm-4.6"`, `"glm-4.5"`.
- `max_tokens`: default `100000`. GLM-5.1 is thinking-mode; budget generously (the model consumes ~70+ reasoning tokens even on trivial prompts).
- **Auth note**: `ZAI_API_KEY` must be the legacy Zhipu `id.secret` format (32-char hex + `.` + alphanum secret). The tool generates an HS256-signed JWT per call internally before sending; raw-Bearer auth with the unsigned key fails on `paas/v4` with `"token expired or incorrect"` despite some docs implying it works. The official `z-ai-sdk-python` does this signing transparently; we do it explicitly.

### `list_api_sessions(provider?)`

List stored DeepSeek / OpenRouter / Grok / Z.AI sessions, newest first. Filter by `"deepseek"`, `"openrouter"`, `"grok"`, or `"zai"`.

### `delete_api_session(session_id)`

Drop a stored session.

---

## How session continuity works

| Provider | Session storage | ID stability |
|---|---|---|
| Codex | Codex's own SQLite + rollout JSONL in `~/.codex/` | Stable UUIDs. Pass back to resume. |
| Gemini | Gemini's chat files in `~/.gemini/tmp/<user>/chats/` | Hex suffix from filename. Stable as long as file exists. |
| DeepSeek | `$MARS_DIR/api-sessions/<uuid>.json` | UUID we generate. Atomic JSON writes. |
| OpenRouter | Same as DeepSeek | Same. |
| Grok | Same as DeepSeek | Same. |
| Z.AI | Same as DeepSeek | Same. JWT is regenerated per call (1-hour exp); session_id only tracks message history. |

For DeepSeek/OpenRouter/Grok/Z.AI, full message history is replayed on every call (the API itself is stateless). When estimated tokens approach the model's context window, oldest user/assistant pairs are dropped (system messages preserved).

---

## Calling from a sub-agent loop

Spawning a child agent (Claude sub-agent, CI runner, automated orchestrator) that itself calls MARS introduces a supervisor-vs-thinking-model timing trap: parent agents kill children that go silent for ~600s, but thinking-mode reasoning models (DeepSeek V4-Pro, Grok 4.20-reasoning, Kimi K2.6, GLM-5.1, Gemini 3.1 Pro Preview) routinely take 5–15 minutes per call. The parent sees silence, kills the child, and the underlying call eventually completes anyway — to no one.

MARS handles this with three engineered fixes shipped in v0.1.2 (commits [`e1301f0`](https://github.com/asakur44/mars/commit/e1301f0) and [`794ca01`](https://github.com/asakur44/mars/commit/794ca01)):

### Progress heartbeats (automatic when called via MCP)

Every chat tool now accepts `ctx: Optional[Context] = None`. FastMCP injects `Context` automatically when the tool is invoked over MCP; while the slow API/CLI call awaits, a background task emits MCP progress notifications every 30s with messages like `"deepseek/deepseek-v4-pro: thinking... (60s elapsed)"`. Parent watchdogs that count progress notifications as liveness see ~20 pings over a 10-minute call instead of one silent block.

Tunable via `MARS_HEARTBEAT_INTERVAL_SEC` env var (default `30`). No-op when called from non-MCP code paths (no `ctx` available).

### Auto disk-brief for Codex (closes ~75% rejection on long structured prompts)

Codex CLI fresh sessions empirically reject ~5KB structured prompts ~75% of the time — Codex returns "send the skeleton you want filled in", returns `[]`, or hallucinates a different schema. The pattern that empirically unblocks Codex is: write the brief to a file, send Codex a one-liner "read FILE and execute". `ask_codex` now does this automatically:

- Triggers when `len(prompt) > CODEX_BRIEF_THRESHOLD` (default `3000`; env-tunable) AND `session_id is None`
- Writes the prompt to `<tempdir>/mars-codex-briefs/brief-<uuid>.md`
- Replaces the prompt with: *"Read PATH and execute. Emit outputs INLINE (do not write files; caller will persist)."*
- Cleans up the brief file after Codex returns (best-effort)

The inline-emit instruction also closes a related Codex sandbox quirk: even at `sandbox=danger-full-access`, in-Codex Write calls aren't reliably permitted by the runtime. Inline-emit avoids the issue entirely; the caller writes the output to disk after `ask_codex` returns.

### Inner timeout bumped from 180s → 900s

Heartbeats keep the parent watchdog alive but don't help if the inner httpx call itself times out at 3 minutes before the model finishes. `_openai_compatible_chat`'s default `timeout_sec` is now `900` (15 min) — fits the typical 5–15 min thinking-mode envelope.

### Per-model output budget

Beyond watchdog and timeout, each provider has its own *output ceiling* — a practical limit on visible tokens per single call beyond which the gateway truncates, returns 504, or silently drops content. These ceilings shift with provider load and aren't always stable, so MARS doesn't enforce them; instead they live as discoverable hints in `_MODEL_PRACTICAL_OUTPUT_CEILING` (in `server.py`).

Empirically observed:

| Model | Practical output ceiling | Bulk fan-out (single call >10K output) |
|---|---|---|
| `grok-4.20-reasoning` (and the 4.20 family) | ~60K | **✓ Holds.** xAI gateway is the only one that reliably delivers long single-call output. |
| `grok-4-1-fast-reasoning` | ~60K | ✓ Holds. |
| `deepseek-v4-flash` | ~64K | ✓ Generally holds (non-thinking). |
| `deepseek-v4-pro` | ~32K | △ Thinking-mode reserves significant budget for internal reasoning; bulk above ~32K can degrade. |
| `moonshotai/kimi-k2.6` | ~32K | △ Treat conservatively — bulk-fanout limit unverified beyond small calls. |
| `gemini-3.1-pro-preview` | ~32K | △ Treat conservatively pending evidence. |
| `glm-5.1` (and the GLM family) | **~16K** | **✗ Fails.** Z.AI gateway truncates / 504s on bulk-output requests. Empirical pattern that works on GLM: per-table fragmentation (5–10 per-section calls of ~4K output each), assembled client-side. |

**The verdict that follows from this:** for bulk fan-out work — single call producing >10K output — route through `ask_grok` with `grok-4.20-reasoning`. For per-section work where each call produces ~4K output and the caller assembles the result, `ask_zai` / `ask_deepseek` / `ask_gemini` are all fine. Don't try to drop GLM-5.1 in as a Grok substitute on bulk-fanout requests; the gateway-side ceiling will silently fail you.

### Patterns that still require caller discipline

Even with the engineered fixes, four orchestration patterns remain caller-side:

- **Smaller agents.** For parallel fan-out, prefer 1–2 MARS calls per child agent. Multiple sequential calls within one child compound the watchdog risk.
- **Inner timeout < parent watchdog.** If the parent's watchdog is 600s, set `ask_*(timeout_sec=400)` so inner failures are cleanly recoverable.
- **Main-thread fallback.** Anticipating a stall? Fire `ask_*` directly from the parent rather than through a child sub-agent. Costs context budget, eliminates supervisor-kill.
- **Late-write recovery.** If a child gets killed mid-call, check the expected output file 5–10 minutes later before discarding the work as failed — the underlying call may have completed and written the file after the kill.

---

## Limitations / known issues

- **No streaming.** Tools return final text only. Codex/Gemini agent loops can take minutes; you won't see partial output.
- **No image input** for `ask_codex` / `ask_gemini` (CLIs support `-i`; not exposed yet).
- **DeepSeek/OpenRouter/Grok/Z.AI token cost grows linearly per turn** — full history is resent each call. DeepSeek's context caching offsets repeat-prefix cost; OpenRouter pass-through depends on the underlying provider; xAI's caching policy varies by model; Z.AI doesn't currently surface a caching parameter on `paas/v4`.
- **Thinking-mode models can hit `max_tokens` invisibly** if you set the cap too low — the model spends 2–6K tokens on internal reasoning before producing visible output, so `max_tokens=4096` will silently truncate real work. The `100000` default avoids this; budget at least 16K if you override.
- **MCP progress notifications are best-effort.** Heartbeats emit via the standard MCP `notifications/progress` channel; clients that don't understand them silently ignore. There's an open issue ([modelcontextprotocol/python-sdk#953](https://github.com/modelcontextprotocol/python-sdk/issues/953)) on streamable-HTTP transport — if your MCP client uses streamable-HTTP, verify heartbeats reach the watchdog before relying on them. Stdio transport (Claude Code's default) is unaffected.
- **Gemini IDs are not natively stable.** The CLI resumes by mtime-ordered index; we rebuild stability by scanning the chat dir. If the user clears history, IDs become invalid.
- **Z.AI key format quirk:** keys are `id.secret` and require client-side JWT signing. The tool handles this internally; do NOT pre-sign or pass a JWT as `ZAI_API_KEY`.
- **Windows + npm shim quirk:** the server resolves CLI paths via `shutil.which()` and explicitly closes stdin to `DEVNULL` to avoid documented hangs in `codex exec` and `gemini -p` when run as a subprocess.

---

## License

MIT. See [LICENSE](./LICENSE).

---

## Contributing

PRs welcome. The whole server is one file (`server.py`) plus `pyproject.toml` — keep it that way unless there's a strong reason to split.

Areas where help would be useful:
- Streaming output (would require an MCP shape change)
- Image input for Codex/Gemini
- More providers (Anthropic direct, local Ollama, Mistral, etc.)
- Tests
