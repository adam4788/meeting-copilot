# Meeting Copilot visual direction

This is Meeting Copilot's own project guide, inspired by
[Nous Research's website](https://nousresearch.com/): cobalt, off-white,
compressed display type, fine frames, technical labels, generous whitespace,
and halftone texture. It is not an official Nous brand manual. Meeting Copilot
is an independent community plugin and does not imply Nous endorsement.

## Identity and voice

Use **Meeting Copilot** in prose, **MEETING COPILOT** for display, and
`meeting-copilot-live` only when the plugin ID helps. The short descriptor is
**Meeting answers grounded in your project files.**

Describe concrete actions: choose a project, ask a question, review the sources.
Keep experimental status visible. Avoid promises of perfect recall, instant
answers, lossless recording, universal compatibility, or guaranteed privacy.
Use synthetic information in examples and visuals.

The proposed project mark is a framed **MC** monogram drawn independently from
plain letterforms, paired with the full name. It is a direction pending artwork,
not an existing verified asset. Do not copy Nous's seal, marks, illustrations,
or proprietary fonts.

## Palette

| Token | Value | Use |
| --- | --- | --- |
| Cobalt | `#0000F2` | Artwork, headings, fine frames, emphasis |
| Paper | `#F2F2F2` | Background and reversed text on cobalt |
| Ink | `#181818` | Body text and technical information |
| Muted | `#565656` | Supporting copy on paper |

Keep text on flat, high-contrast backgrounds. Texture must not reduce
legibility. GitHub controls README typography/colors; these tokens apply to
owned artwork and previews rather than forced README styling.

## Typography and composition

- Display: `Impact, 'Arial Narrow', sans-serif`, uppercase, short headings.
- Body: `system-ui, 'Segoe UI', Arial, sans-serif`, natural sentence case.
- Technical labels: `ui-monospace, Consolas, monospace`, short and restrained.

These are system font choices; no font file is bundled or redistributed.
Use one focal point, broad margins, and thin cobalt frames. Keep labels readable
at GitHub content width. For a 1600 px canvas, leave at least 48 px outer margin
and extra breathing room around titles. Keep essential facts in accessible prose.

Reserve halftone for decorative areas connecting abstract meeting signals,
documents, and evidence. Keep dense patterns away from text. Favor a limited
palette and editorial spacing over glow effects or fabricated dashboards.

## Image roles and evidence

The supplied GPT-generated hero is conceptual artwork. Its caption must say
**Generated conceptual artwork; not a product screenshot.** Inspect its actual
pixels before writing alt text or selecting a crop.

Real UI images must show an observed interface with synthetic data and identify
platform/revision. Never label generated UI as a screenshot or show fabricated
test results. Exclude private meetings, client details, keys, and personal paths.
Add no success badge without a real linked result.

See [asset notes](assets/README.md) for planned filenames, dimensions, and
provenance. Preserve the source Library item; repository assets are copies.
No additional image generation is needed for the current documentation scope.
