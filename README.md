# ModelMesh

An MCP server that lets Claude Code (or any MCP client) delegate work to other LLMs as subagents — preserving conversation continuity across turns via stable session IDs.

Wraps five backends:

| Tool | Backend | Auth |
|---|---|---|
| `ask_codex` | Local [`codex`](https://developers.openai.com/codex/cli) CLI (full agent loop) | `codex login` |
| `ask_gemini` | Local [`gemini`](https://geminicli.com) CLI (full agent loop) | `gemini auth` |
| `ask_openrouter` | OpenRouter HTTPS API (any model) | `OPENROUTER_API_KEY` env |
| `ask_deepseek` | DeepSeek HTTPS API | `DEEPSEEK_API_KEY` env |
| `ask_grok` | xAI Grok HTTPS API | `XAI_API_KEY` env |

Plus admin tools: `list_api_sessions`, `delete_api_session`.

---

## Why this exists

Claude Code is great, but sometimes you want a second opinion from GPT-5, or to delegate a long task to Gemini's 1M-context model, or to run a cheap reasoning pass on DeepSeek-R1. Doing this by hand — switching tools, copy-pasting context, losing thread — is friction.

ModelMesh turns those LLMs into first-class tools Claude can call mid-conversation. Each call returns a `session_id` you can pass back on the next call to keep the conversation going. Codex/Gemini sessions persist in their own native stores; DeepSeek/OpenRouter conversations are kept in a local JSON store and replayed on each call.

---

## Install

### Prerequisites

- Python 3.10+
- For `ask_codex`: install and authenticate [Codex CLI](https://developers.openai.com/codex/cli) (`npm install -g @openai/codex` then `codex login`)
- For `ask_gemini`: install and authenticate [Gemini CLI](https://geminicli.com) (`npm install -g @google/gemini-cli` then `gemini auth`)
- For `ask_openrouter`: an [OpenRouter API key](https://openrouter.ai/settings/keys)
- For `ask_deepseek`: a [DeepSeek API key](https://platform.deepseek.com/api_keys)
- For `ask_grok`: an [xAI API key](https://console.x.ai/)

### Install the server

```bash
git clone https://github.com/asakur44/ModelMesh.git
cd ModelMesh
pip install .
```

This puts a `modelmesh` console command on your PATH.

(Alternative: if you have [`uv`](https://docs.astral.sh/uv/) installed, you can skip `pip install` and use `uv run server.py` — the inline PEP 723 metadata handles deps.)

### Register with Claude Code

```bash
claude mcp add --scope user modelmesh \
  --env OPENROUTER_API_KEY=sk-or-... \
  --env DEEPSEEK_API_KEY=sk-... \
  --env XAI_API_KEY=xai-... \
  -- modelmesh
```

`--scope user` makes it available across all your projects. Drop `--env` flags for any provider you don't have a key for; the corresponding tools will return a clean error when called instead of crashing the server.

In Claude Code, run `/mcp` to confirm `modelmesh` is connected. Seven tools should now be callable.

### Register with other MCP clients

Cursor, Windsurf, VS Code MCP, etc. all accept stdio MCP servers. Point them at the `modelmesh` command with the same env vars.

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

`ask_openrouter` defaults to `deepseek/deepseek-v4-pro` (1M context, cheap, strong general/reasoning). Override per-call:

```python
ask_openrouter(prompt="...", model="anthropic/claude-sonnet-4.6")
ask_openrouter(prompt="...", model="openai/gpt-5")
ask_openrouter(prompt="...", model="x-ai/grok-4")
```

`ask_deepseek` defaults to `deepseek-chat` (V3). For hard reasoning use `deepseek-reasoner` (R1):

```python
ask_deepseek(prompt="prove that...", model="deepseek-reasoner")
```

`ask_grok` defaults to `grok-4-1-fast` (alias for the current frontier reasoning variant; 2M context). Other choices:

```python
ask_grok(prompt="...", model="grok-4-1-fast-non-reasoning")  # fast, no reasoning
ask_grok(prompt="...", model="grok-code-fast-1")              # agentic coding (256K)
```

---

## Telling Claude to use session IDs

Claude Code won't automatically capture session IDs unless you teach it to. Add this to your global `~/.claude/CLAUDE.md` (creates if missing):

```markdown
## modelmesh — session_id continuity

The MCP server `modelmesh` exposes `ask_codex`, `ask_gemini`,
`ask_deepseek`, `ask_openrouter`, `ask_grok`. Each returns
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

---

## Configuration

All optional. Set in the `--env` flags when registering, or in your shell environment.

| Env var | Purpose | Default |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API key | required for `ask_deepseek` |
| `OPENROUTER_API_KEY` | OpenRouter API key | required for `ask_openrouter` |
| `XAI_API_KEY` | xAI Grok API key | required for `ask_grok` |
| `MODELMESH_DIR` | Where to store API session files | `~/.modelmesh/` |
| `OPENROUTER_REFERER` | `HTTP-Referer` header sent to OpenRouter (analytics attribution) | omitted |
| `OPENROUTER_TITLE` | `X-Title` header sent to OpenRouter | omitted |

---

## Tool reference

### `ask_codex(prompt, model?, cwd?, sandbox?, timeout_sec?, session_id?)`

Runs the Codex CLI's full agent loop (read files, edit, run shell commands) inside a sandbox.

- `sandbox`: `"read-only"` | `"workspace-write"` (default) | `"danger-full-access"`
- `cwd`: working directory for Codex (default: server's CWD)
- `session_id`: pass `None` for fresh, `"last"` for most recent, or any UUID/thread name to resume that exact session

### `ask_gemini(prompt, model?, cwd?, approval_mode?, timeout_sec?, session_id?)`

Runs the Gemini CLI as an agent.

- `approval_mode`: `"yolo"` (default) | `"auto_edit"` | `"plan"` (read-only) — must NOT be `"default"` (would block on prompts)
- `session_id`: pass `None` for fresh, `"last"` for most recent, or a hex id previously returned by this tool

Note: Gemini's CLI resumes by mtime-ordered index, not by stable id. The server resolves your hex id to the current index by scanning `~/.gemini/tmp/<user>/chats/`. IDs are stable as long as the chat file isn't deleted.

### `ask_openrouter(prompt, model?, system?, max_tokens?, session_id?)`

Stateless API calls + local session replay.

- `model`: default `"deepseek/deepseek-v4-pro"`. Pass any [OpenRouter model id](https://openrouter.ai/models).
- `system`: optional system prompt (used only on fresh sessions)
- `session_id`: pass `None` for fresh, or a UUID from a previous call

History is replayed each call; oldest pairs are trimmed when context approaches the model's window.

### `ask_deepseek(prompt, model?, system?, max_tokens?, session_id?)`

- `model`: `"deepseek-chat"` (V3, default) or `"deepseek-reasoner"` (R1)
- For `deepseek-reasoner`: `reasoning_content` (CoT) is intentionally NOT stored, per DeepSeek's guidance — only the final assistant message goes into history.

### `ask_grok(prompt, model?, system?, max_tokens?, session_id?)`

- `model`: `"grok-4-1-fast"` (default; alias for `grok-4-1-fast-reasoning`, 2M context). Other ids: `"grok-4-1-fast-non-reasoning"`, `"grok-code-fast-1"` (256K coding), `"grok-4"` / `"grok-4-0709"` (older 256K).
- `session_id`: same shape as the other API tools — None for fresh, a UUID from a previous call to resume.

### `list_api_sessions(provider?)`

List stored DeepSeek / OpenRouter / Grok sessions, newest first. Filter by `"deepseek"`, `"openrouter"`, or `"grok"`.

### `delete_api_session(session_id)`

Drop a stored session.

---

## How session continuity works

| Provider | Session storage | ID stability |
|---|---|---|
| Codex | Codex's own SQLite + rollout JSONL in `~/.codex/` | Stable UUIDs. Pass back to resume. |
| Gemini | Gemini's chat files in `~/.gemini/tmp/<user>/chats/` | Hex suffix from filename. Stable as long as file exists. |
| DeepSeek | `$MODELMESH_DIR/api-sessions/<uuid>.json` | UUID we generate. Atomic JSON writes. |
| OpenRouter | Same as DeepSeek | Same. |
| Grok | Same as DeepSeek | Same. |

For DeepSeek/OpenRouter/Grok, full message history is replayed on every call (the API itself is stateless). When estimated tokens approach the model's context window, oldest user/assistant pairs are dropped (system messages preserved).

---

## Limitations / known issues

- **No streaming.** Tools return final text only. Codex/Gemini agent loops can take minutes; you won't see partial output.
- **No image input** for `ask_codex` / `ask_gemini` (CLIs support `-i`; not exposed yet).
- **DeepSeek/OpenRouter/Grok token cost grows linearly per turn** — full history is resent each call. DeepSeek's context caching offsets repeat-prefix cost; OpenRouter pass-through depends on the underlying provider; xAI's caching policy varies by model.
- **Gemini IDs are not natively stable.** The CLI resumes by mtime-ordered index; we rebuild stability by scanning the chat dir. If the user clears history, IDs become invalid.
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
- More providers (Anthropic direct, local Ollama, etc.)
- Tests
