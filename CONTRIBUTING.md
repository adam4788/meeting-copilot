# Contributing

1. Open an issue describing the actual user problem and your platform.
2. Keep changes small and add a failing regression test before fixing behavior.
3. Run `python -m unittest discover -v` and `node --check desktop/plugin.js`.
4. Separate deterministic tests from explicit, opt-in device/model/network tests.
5. State exactly what you tested. Do not label mocked output as hardware proof.

Never commit credentials, local paths containing personal details, meeting transcripts,
recordings, project files, or session databases. Use synthetic fixtures.

Preserve source notices when incorporating upstream code. Do not copy proprietary
application assets or suggest an affiliation with referenced products.

Capture must stay opt-in. Do not introduce automatic speaking, sending messages,
external-system writes, or transcript-derived public search queries.
