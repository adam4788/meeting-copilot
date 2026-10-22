# Contributing

1. Open an issue describing the actual user problem and your platform.
2. Keep changes small and add a failing regression test before fixing behavior.
3. Run `npm test` and `npm run check`.
4. Separate deterministic tests from explicit, opt-in device/model/network tests.
5. State exactly what you tested. Do not label mocked output as hardware proof.

Never commit credentials, local paths containing personal details, meeting transcripts,
recordings, project files, or session databases. Use synthetic fixtures.

Preserve source notices when incorporating upstream code. Do not copy proprietary
application assets or suggest an affiliation with referenced products.

Capture must stay opt-in. Do not introduce automatic speaking, sending messages,
external-system writes, or raw transcript/source excerpts as public search queries.
Automatic web fallback must stay visible and disableable, draft only generic public
queries, preserve input redaction, and fail closed when planning or filtering fails.
