# Changelog

All notable changes to MARS will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **`ask_grok` default model bumped to `grok-4.5`** (from
  `grok-4.3`, which stays available as the documented fallback when
  4.5 errors on a given call). Context-hint and practical-output
  tables gained `grok-4.5` entries mirroring 4.3 pending xAI docs.
  Alongside it, the tool docstrings now carry the full usage contract
  that previously lived in the operator's client-side notes: the
  session-continuity rule is spelled out on every `ask_*` tool
  (`ask_mimo` had an abbreviated form), and `ask_kimi` notes the
  absence of a `max_tokens` parameter and that it's preferred over
  Kimi-via-`ask_openrouter` for repo-aware work — per the practice of
  putting tool guidance in tool descriptions rather than host-side
  prompt files.

- **`ask_kimi` rerouted from the Moonshot HTTPS API to the local
  `kimi` Kimi Code CLI** (`@moonshot-ai/kimi-code`, verified on
  0.31.1). Billing moves from per-token `KIMI_API_KEY`
  (api.moonshot.ai) to the Kimi Code *subscription* (CLI OAuth via
  `kimi login`; quota refreshes weekly), and the tool gains the CLI's
  full agent loop (read/write files, shell in `cwd`, Moonshot web
  search/fetch — no sandbox; print mode auto-approves tool calls).
  Signature: `max_tokens` dropped (no CLI equivalent); `cwd` and
  `timeout_sec` (default 600s) added; `system` is now folded into the
  prompt as a `<system>` preamble on fresh sessions. Default model is
  the CLI alias `"kimi-code/k3"` (K3 flagship, 1M context, thinking —
  continues the K3 default set 2026-07-18); other aliases:
  `"kimi-code/k3-256k"`, `"kimi-code/kimi-for-coding"` (K2.7),
  `"kimi-code/kimi-for-coding-highspeed"`. Session ids are now kimi's
  own `"session_<uuid>"` form (stored under `~/.kimi-code/sessions`,
  parsed from the stream-json `session.resume_hint` meta event);
  legacy API-era kimi sessions in `~/.mars/api-sessions/` are no
  longer resumable, though `list_api_sessions` / `delete_api_session`
  still see them for cleanup. `KIMI_API_KEY` / `KIMI_CODE_API_KEY` are
  no longer read, and the direct-Kimi entries in
  `_MODEL_CONTEXT_HINT` were removed (the CLI manages its own
  context; `moonshotai/*` OpenRouter entries remain for
  `ask_openrouter`). Transport workaround baked in: the CLI mangles
  any `-p` argument containing a newline on Windows (drops the prompt
  and prints its idle greeting), so multi-line prompts — and prompts
  over `KIMI_BRIEF_THRESHOLD` chars (default 20000; Windows
  CreateProcess caps command lines at ~32K) — are written to a temp
  disk brief the agent reads back. End-to-end verified: fresh session
  with `<system>` fold + multi-line prompt, and resume with context
  retention and stable session id.

### Fixed

- **Subprocess timeouts now kill the whole process tree and report a
  resumable session id (all CLI-backed seats: kimi, codex, agy).**
  `_run_subprocess` used plain `proc.kill()` on timeout, which on
  Windows terminates only the direct child — for npm-installed CLIs
  that's the `.cmd` shim (cmd.exe), so the node.exe agent loop
  survived as an orphan that kept running after MARS reported
  "subagent timed out" (observed 2026-08-01: two timed-out `ask_kimi`
  calls with `timeout_sec=1500` kept writing files for 21 and 44 more
  minutes, racing a follow-up session in the same directory). Timeouts
  now kill the full tree: `taskkill /T /F` on Windows, process-group
  kill (`start_new_session` + `killpg`) on POSIX; the agy ConPTY
  helper's internal timeout path got the same treatment
  (`PtyProcess.terminate` shares the direct-child-only flaw).
  Additionally, a timed-out call used to return no session id, so the
  interrupted run couldn't be resumed. `_run_subprocess` now streams
  stdout/stderr into buffers as the child runs and raises
  `SubprocessTimeout` carrying the partial output; each CLI seat then
  recovers the id — codex from the partial output (it prints the
  thread id early), kimi from the CLI session store
  (`~/.kimi-code/sessions/wd_*/session_*`, newest dir touched since
  launch, scoped to the call's cwd), agy from its cwd→conversation
  cache — and embeds it in the timeout error message with a hint to
  pass it back as `session_id` and resume. Regression tests in
  `tests/test_timeout_kill.py` drive fake long-running CLIs (installed
  as real `.cmd` shims that spawn grandchildren, mirroring the
  npm-shim tree) and assert the whole tree dies and the session id
  surfaces; run with `pytest` (new `dev` dependency group).
- **Pinned `mcp<2`.** mcp 2.0.0 removed the `mcp.server.fastmcp`
  module this server is built on, so a fresh install resolving
  `mcp>=1.2.0` to 2.x failed on import (verified 2026-08-01). Both
  `pyproject.toml` and the PEP 723 inline metadata now cap at `<2`.
- **`pyproject.toml` was missing two runtime dependencies that the
  PEP 723 inline metadata at the top of `server.py` already declared.**
  `pyjwt>=2.0.0` (needed by `ask_zai`'s JWT signing) and
  `pywinpty==2.0.14; sys_platform == 'win32'` (needed by the `ask_agy`
  ConPTY helper) are now listed in `[project] dependencies`, so
  `pip install .` pulls in everything `uv run server.py` already got
  from the inline metadata. Closes the `pyjwt` gap self-flagged in the
  Notes section below, and fixes the same-shaped `pywinpty` gap that
  was never flagged.
- **README documentation catch-up for `ask_mimo` / `ask_kimi` /
  `ask_agy`.** Three of the server's eight chat subagent tools had no
  `Tool reference` entries, the intro table only listed seven backends
  and omitted `ask_agy` entirely, and the `list_api_sessions` /
  `delete_api_session` reference text hadn't caught up to the
  `mimo`/`kimi` provider support the `server.py` docstrings already
  documented. Added `Tool reference` entries for all three, fixed the
  intro backend count/table and tool-count line, added an `ask_agy`
  Prerequisites blurb, and fixed the Install section's stale
  PyJWT-only dependency claim (see the `pyproject.toml` fix above).
- **Purged stale Gemini-CLI references from the README.** The multi-turn
  continuity example header, the no-streaming limitation, and the
  npm-shim quirk still described the removed `ask_gemini` backend as
  active, and the 'Gemini IDs are not natively stable' limitation
  bullet described session handling for a tool that no longer exists.
  Cleaned up; the intentional strikethrough removal notice in
  Prerequisites stays.

- **API-key env vars now survive an MCP host that fails to expand
  `${VAR}` placeholders.** Claude Code expands `${VAR}` references in the
  server's `env` block on initial launch, but its MCP *reconnect* path was
  observed passing the literal placeholder through unexpanded
  (claude.exe 2.1.118, 2026-07-20: same parent process and config — the
  session-start spawn got real values, the reconnect respawn got literal
  `${DEEPSEEK_API_KEY}` etc., producing 401s on every API-key route while
  CLI-OAuth routes kept working). `_require_env` (and the
  `KIMI_CODE_API_KEY` path) now detects unset or `${VAR}` /
  `${VAR:-default}`-shaped values and falls back to the persistent OS
  user/machine environment (Windows registry), which is always current
  even when the process env is stale or corrupted. Off-Windows the
  fallback is a no-op and behavior is unchanged.

### Added

- **`ask_mimo`** — chat-completion backend for the Xiaomi MiMo API
  (OpenAI-compatible, Singapore plan; default model `mimo-v2.5-pro`).
  Reads `MIMO_API_KEY`.
- **`ask_kimi`** — chat-completion backend for Kimi / Moonshot AI
  (OpenAI-compatible; default model `kimi-k2.6` on the Moonshot Open
  Platform, `api.moonshot.ai`). Reads `KIMI_API_KEY`. Passing
  `model="kimi-for-coding"` routes to the Kimi Code *subscription*
  endpoint (`api.kimi.com/coding`) and requires `KIMI_CODE_API_KEY`;
  this route is guarded so it errors clearly rather than misrouting when
  unconfigured.
- Context-window hints and `list/delete_api_session` provider support for
  the `mimo` and `kimi` providers.
- **Kimi K3 is now the default Moonshot model** (2026-07-18). `ask_kimi`
  defaults to `kimi-k3` and `ask_openrouter` to `moonshotai/kimi-k3`,
  superseding `kimi-k2.6`. Both ids validated live on their routes;
  context-window and practical-output-ceiling hints added for k3. Pass
  the older `kimi-k2.6` / `moonshotai/kimi-k2.6` ids explicitly to fall
  back.

### Changed

- **`ask_zai` default model bumped `glm-5.1` → `glm-5.2`.** Recorded as
  the operator's default on 2026-06-17 (Zhipu AI's current flagship,
  superseding `glm-5.1`), but the server itself was never updated to
  match until now. Pass `model="glm-5.1"` explicitly to keep the old
  default. Added a conservative `glm-5.2` context-window hint (128K,
  mirrors `glm-5.1`, pending z.ai per-model docs) to
  `_MODEL_CONTEXT_HINT`. Updated the README's `ask_zai` default-model
  mentions (Choosing a model, Tool reference) to match.

### Removed

- **`ask_gemini`** — removed 2026-06-22. Google discontinued the free Gemini
  Code Assist CLI tier (`IneligibleTierError: This client is no longer supported
  for Gemini Code Assist for individuals … migrate to the Antigravity suite`), so
  the `gemini` CLI no longer authenticates as a subagent. The tool, its
  `~/.gemini` chat-tracking helpers (`_gemini_chats_dir`, `_gemini_chat_files`,
  `_gemini_id_from_filename`, `_resolve_gemini_index`, `_GEMINI_FILE_RE`), and the
  docstring/README references are removed. **Gemini models remain reachable via
  `ask_agy`** (Antigravity CLI: `Gemini 3.1 Pro` / `Gemini 3.5 Flash` labels on
  the Google AI Pro plan). The server now exposes **eight** subagent tools.

### Changed (BREAKING)

- **Project renamed: ModelMesh → MARS** (Model Adapter Routing System).
  Part of the Fr4ym + MARS + BCKS stack.
- **MCP server registration name** changed from `modelmesh` to `mars`.
- **MCP tool names** auto-derived from the server name and therefore changed
  from `mcp__modelmesh__*` to `mcp__mars__*`. This is a breaking change for
  any client that hard-codes tool names; **there is no shim for MCP tool
  names**. Affected callers must:
  1. Re-register the MCP server under `mars` in their client config
     (e.g. in `~/.claude.json` rename the `"modelmesh"` key to `"mars"`).
  2. Replace `mcp__modelmesh__*` with `mcp__mars__*` in any saved
     scripts, playbooks, allowlists, or autocomplete configs.
  3. Restart the client so the MCP server is launched under the new
     registration name.
- **GitHub repository renamed** from `asakur44/ModelMesh` to `asakur44/mars`.
  GitHub auto-redirects the legacy URL so existing `git clone` and `git fetch`
  continue to work without action.

### Deprecated

The following legacy names continue to work with a `DeprecationWarning`
through MARS v0.2.0 (one minor release after this one); they will be
removed thereafter:

- `modelmesh` console-script entry point — use `mars` instead.
  Both are installed by `pip install .` for now.
- `MODELMESH_DIR` env var — use `MARS_DIR` instead.
- `MODELMESH_HEARTBEAT_INTERVAL_SEC` env var — use `MARS_HEARTBEAT_INTERVAL_SEC`.
- `~/.modelmesh/` default storage path — used as a fallback if `~/.mars/`
  doesn't exist yet. Migrate with `mv ~/.modelmesh ~/.mars` to preserve
  existing session files, or set `MARS_DIR=~/.modelmesh` to keep the legacy
  location explicitly.

### Unchanged

- All tool function signatures (`ask_grok`, `ask_deepseek`, `ask_codex`,
  `ask_gemini`, `ask_openrouter`, `ask_zai`, `list_api_sessions`,
  `delete_api_session`) and their return shapes.
- All env var names that aren't `MODELMESH_*` (DeepSeek / OpenRouter / xAI /
  Z.AI keys, OpenRouter analytics headers, etc.).
- Session-storage file format. Existing JSON sessions in `~/.modelmesh/api-sessions/`
  remain readable after `mv ~/.modelmesh ~/.mars`.
- Package version (`0.1.0`). Version bump will land in a separate PR after
  this rename merges.

### Migration checklist

- [ ] Re-register MARS in your MCP client config (rename `"modelmesh"` →
      `"mars"` in `~/.claude.json` or equivalent).
- [ ] Update any scripts that reference `mcp__modelmesh__*` tool names →
      `mcp__mars__*`.
- [ ] Optionally `mv ~/.modelmesh ~/.mars` to silence the storage-path
      deprecation warning and keep existing sessions reachable under the
      new default path.
- [ ] Optionally rename `MODELMESH_DIR` / `MODELMESH_HEARTBEAT_INTERVAL_SEC`
      env vars to `MARS_*` to silence env-var deprecation warnings.
- [ ] Restart your MCP client so the server launches under the new
      registration name.

### Notes

- This project has no public Python `import modelmesh` surface (the
  distribution ships a single top-level module `server.py`), so the
  rename does not require a Python import-path deprecation shim.
  The deprecation surface is the console-script name, the env vars, the
  storage path, and the MCP server name.
- pyproject.toml `pyjwt` dependency: was flagged here as pending a
  separate fix — now fixed. See the **Fixed** entry above, which adds
  both `pyjwt` and the same-shaped `pywinpty` gap to `[project]
  dependencies`.

### External services (require separate action by the maintainer)

- **PyPI** — when the package is published (currently it isn't), it should
  publish under the new name `mars`. The legacy `modelmesh` name is unclaimed
  on PyPI; reserving it as a deprecation-redirect is optional.
- **Docker Hub / npm / Cargo crate** — N/A for this project (no images
  / packages published there).

---

## Earlier history

This CHANGELOG was added with the rename. For the project history before
the rename (under the name "ModelMesh"), see the git log on the
[`v0.1.0`](https://github.com/asakur44/mars/releases/tag/v0.1.0) and
[`v0.1.1`](https://github.com/asakur44/mars/releases/tag/v0.1.1) tags.
