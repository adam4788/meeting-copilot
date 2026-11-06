# Development

These commands require the full source checkout; runtime exports omit `tests/`
and `scripts/`. Work in a separate environment. Use synthetic fixtures and
keep real meetings, credentials, databases, and personal paths out of Git.

## Offline checks

Python 3.11/3.12 and Node 22 match Windows/Linux CI. macOS CI uses Python 3.12
and the macOS 26 Swift toolchain.

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -v
node --check desktop/plugin.js
node --test tests/test_desktop.mjs
```

`npm test` runs the same checks. Run one Python module with, for example,
`python -m unittest tests.test_catalog_export -v`. These checks use fixtures;
platform-dependent tests may skip. They do not establish provider, hardware, or
visual readiness.

For an offline source-mapping demonstration:

```sh
python ask.py --docs sample-docs --project acme --provider fake --question "When is finance sign-off needed?"
python demo_standalone.py
```

`python -m tests.verify_native_backpressure` uses Swift for a device-free writer
probe; it does not establish loss-free capture. The `tests.verify_overlay_*`
modules are development mutation checks. See [native backpressure](native-backpressure.md)
and [overlay lifecycle](overlay-lifecycle.md).

## Plugin validation

Use an isolated Hermes validation environment; these commands do not test a live
provider or device:

```sh
hermes plugins doctor /path/to/meeting-copilot --ci
hermes plugins validate /path/to/meeting-copilot --install-deps
```

Review the complete scanner output, including private API and development
findings. See [catalog preflight](catalog-readiness.md) and
[compatibility](catalog-compatibility.md).

## Runtime source export

From a clean, committed checkout:

```sh
python -m scripts.export_catalog_runtime /new/output/directory
```

The distinct allowlisted tree includes runtime files, text guides, sample
documents, Windows audio requirements, the license, and the README hero image.
Tests and mutation probes stay in the source commit. Export is a packaging
boundary, not scanner suppression or publication; validate the actual export
independently. A catalog pin must identify the published tree that was validated.

For manual checks obtain permission before recording and use fictional content.
Short synthetic runs cannot establish long-call reliability. See [BYOK](byok.md),
[contributing](../CONTRIBUTING.md), and the [demo](demo.md).
