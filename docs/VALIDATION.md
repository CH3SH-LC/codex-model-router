# Validation and known limits

The initial in-app implementation was checked on Windows with desktop
`26.1007.9446.0` and engine `0.162.0-alpha.17.2`. Compatibility with every newer
desktop build is not claimed.

## Runtime validation

- 32 router and MCP application tests passed on Windows.
- The final modified network/application areas passed an 18-test regression.
- The actual engine loaded the plugin, native settings, and MCP App HTML.
- A real subscription request completed through the loopback WebSocket route.
- The actual engine's `model/list` included native and local fixture models.
- An actual read-only `get_goal` tool call completed through a scripted local
  Chat Completions upstream. A script that imitates a provider is not a real model.
- The actual desktop MCP App was operated: add two models, save, edit, test
  Responses and Chat Completions, show a deliberate 503 error, inspect request
  records, cancel deletion, confirm deletion, and refresh.
- Test configuration was removed afterward. Authentication and global config
  hashes remained unchanged. Current subscription requests continued successfully.

Only the generic empty configuration screenshot is included publicly. Raw machine
paths, account metadata, native catalogs, detailed logs, and private receipts are
not part of this repository.

## Public distribution

Version 1.2.0 adds a portable Windows setup path, versioned runtimes, installer
regression tests, a public-file allowlist, deterministic packaging, and Windows CI.
All 37 tests passed locally on Windows. A built source ZIP was extracted and its
installer was run against the actual desktop engine with a separate `CODEX_HOME`
and `LOCALAPPDATA`: dependencies were installed, the plugin was registered and
cached, routing remained disabled, the fixture default model was preserved, and
no authentication file was created. This is an isolated directory installation on
the development machine, not a clean Windows-machine certification.

See the Actions run for the exact commit being installed.

## Not yet claimed

- The native desktop model menu's custom entry has not been selected in a visual
  acceptance run; engine catalog acceptance is separate.
- No arbitrary third-party commercial provider is certified.
- The real tool-loop acceptance used a read-only tool, not filesystem writes.
- Ordinary ChatGPT cloud conversations, cloud Work, voice, and non-Windows
  platforms are outside scope.
- Updating a desktop client can change its plugin and model APIs. Revalidate after
  upgrading; the plugin does not patch client binaries or suppress client checks.
