# Contributing

Use Windows with CPython 3.13. Install `requirements.lock.txt` into a local venv.
Run `python -m unittest discover -s tests -v` after completing the change.
The tests use temporary directories and local upstream fixtures, not your account.

Keep routing, credentials, native configuration, and protocol limitations
explicit. Preserve unrelated TOML edits and reject ambiguous or unsupported
conversions. Do not add implicit provider fallback.

For a release, update the version in the plugin manifest, `router/__init__.py`,
the UI app information, the launcher runtime path, and installation docs.
Update `public-files.txt` intentionally when adding a public file, run
`scripts/check_public.py`, then `scripts/package.py`. Package checksums are
generated after all files and documentation are final.

Public contributions must not contain user data, live credentials, native model
caches, private configuration, or third-party proprietary desktop resources.
Any desktop integration test must use a temporary `CODEX_HOME` unless explicitly
performing an authorized real installation.

Small focused pull requests with a concrete before/after description and test
evidence are welcome. Use the security reporting path for vulnerabilities.
