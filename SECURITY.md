# Security

## Reporting

Use [GitHub private vulnerability reporting](https://github.com/CH3SH-LC/codex-model-router/security/advisories/new)
for security issues. Do not place API keys, subscription tokens, native model
caches, raw request bodies, or local configuration in public issues.

## Boundaries

The gateway binds only to `127.0.0.1` and requires a randomly generated path
capability. It does not expose an external configuration website. Do not publish
or tunnel its port. A local process running as the same Windows user is inside
the trust boundary and may access local credentials or decrypted data.

Custom endpoints receive the selected conversation input and tool definitions.
Choose providers you trust. Subscription authorization is not forwarded to
custom endpoints. The plugin does not load keys from other applications.

DPAPI protects stored keys for the current Windows user, not from malicious
software already running as that user. Only the bundled UI can use app-only
configuration tools through the normal host; the session capability is kept out
of model-visible tool text.

No Windows autostart, public server, approval bypass, or client binary patch is
installed. Turning off routing restores only the two fields written by the
router and refuses to overwrite conflicting edits.
