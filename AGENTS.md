# Working in Meeting Copilot

Start with [README.md](README.md) for product status and [SECURITY.md](SECURITY.md)
for data boundaries. This is an early developer preview, not proof of a working
live meeting or provider account.

## Map

- Root Python modules, `plugin.yaml`, `dashboard/`, `desktop/`, `native/` and
  `install.py`: Hermes runtime and its loading/install boundaries. Keep their
  paths stable unless the manifest, installer and export are updated together.
- `tests/`: Python and Node regression tests, synthetic backend and development
  probes. Probes are not runtime dependencies.
- `scripts/`: source-only packaging tools; `docs/`: task guides and design
  contracts; `sample-docs/`: fictional retrieval fixtures.

## Setup and checks

From the repository root, use a separate Python 3.11/3.12 environment and
Node 22. Install `requirements-dev.txt`, then run:

```sh
python -m unittest discover -v
node --check desktop/plugin.js
node --test tests/test_desktop.mjs
```

`npm test` runs the same checks. For a focused Python test use, for example,
`python -m unittest tests.test_catalog_export -v`. See
[development](docs/development.md) for validation, offline demo, and export.

## Change boundaries

- Capture requires explicit consent; do not send, speak, or post automatically.
  Preserve scoped credentials, explicit provider/model selection, bounded data,
  read-only retrieval, and visible opt-in web behavior.
- The overlay is read-only and must not retain pane credentials or initiate
  answers. Pane answers require a host-bound scope. See
  [compatibility](docs/catalog-compatibility.md) before changing either path.
  Web search uses a profile-bound public registry and fails visibly when absent.
- Keep test fixtures synthetic. Do not commit credentials, recordings,
  transcripts, databases, private project data, or personal paths.
- `install.FILES` defines the legacy installer payload. The catalog exporter
  also has a narrow allowlist; update both and their tests when paths change.
  Do not put test helpers or mutation probes in the runtime export.
- Validate the behavior and links affected by a change. Report platform skips
  and distinguish mocked tests from live provider, device, and UI verification.
