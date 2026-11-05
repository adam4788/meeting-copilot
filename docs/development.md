# Development

Use a source checkout and a separate development environment. Keep real meeting
data out of fixtures and do not work in an installed plugin directory.

## Deterministic checks

Python 3.11/3.12 and Node.js 22 match Windows/Linux CI. macOS CI uses Python 3.12
and builds the overlay with the macOS 26 toolchain.

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -v
node --check desktop/plugin.js
node --test test_desktop.mjs
```

`npm test` runs those three checks; `npm run check` runs JavaScript syntax only.
These synthetic checks do not establish live provider access, visual acceptance,
or hardware readiness. Some platform/hardware checks skip unless explicitly enabled.

For an offline source-mapping smoke check:

```sh
python ask.py --docs sample-docs --project acme --provider fake --question "When is finance sign-off needed?"
python demo_standalone.py
```

With Swift installed, `python verify_native_backpressure.py` runs a device-free
synthetic writer probe. It opens no audio device and does not establish loss-free
capture. See [the investigation](native-backpressure.md).

## Source-checkout plugin validation

Run in an isolated Hermes validation environment, using your real checkout path:

```sh
hermes plugins doctor /path/to/meeting-copilot --ci
hermes plugins validate /path/to/meeting-copilot --install-deps
```

The manifest declares `fastapi>=0.115,<1`, resolved under Hermes constraints.
Keep complete scanner output, including private API and development findings.
Passing registration is not a device/provider acceptance claim. See
[catalog preflight](catalog-readiness.md) and [compatibility](catalog-compatibility.md).

## Runtime source export

From a clean, committed checkout:

```sh
python export_catalog_runtime.py /new/output/directory
```

The exporter creates a distinct allowlisted source tree without publishing it,
preserving provenance and runtime bytes. Tests/mutation probes remain in the full
source commit. Export is a packaging boundary, not scanner suppression. Validate
the actual export separately; any catalog pin must identify that published tree.

The current export allowlist includes text documentation but excludes image/SVG
assets. A later packaging change must account for local image references before
exporting a README that uses them. This docs branch does not change packaging code.

## Manual verification

Keep device/provider/network checks explicit and separate. Obtain permission
before recording. Use fictional speech and synthetic files; never commit
recordings, transcripts, keys, session databases, or personal local paths. Short
synthetic runs do not establish long-call reliability.

Standalone BYOK requires `--provider openai|openrouter --model EXACT_ID` and the
matching process environment key; it reads no credential files. `--doctor` checks
local setup only. See [BYOK](byok.md), [contributing](../CONTRIBUTING.md), and
[the demo](../DEMO.md).
