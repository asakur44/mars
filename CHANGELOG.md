# Changelog

All notable changes to MARS will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed (BREAKING)

- **Project renamed: ModelMesh → MARS** (Model Adapter & Routing System).
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
- pyproject.toml `pyjwt` dependency: `pyjwt>=2.0.0` is declared in the
  PEP 723 inline metadata in `server.py` for `uv run`, but is not yet
  in pyproject.toml's `[project] dependencies`. Pre-existing (predates
  this rename); flagged for a separate fix.

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
