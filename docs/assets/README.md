# Documentation assets

The supplied hero has been read and visually inspected from the local PNG.
It is an abstract audio/document illustration, with no product UI, private
meeting information, or embedded product claims. It is used in the README
and brand guide without cropping or changing the original PNG bytes.

- [meeting-copilot-hero.png](meeting-copilot-hero.png): supplied GPT-generated
  conceptual README artwork, 2172 × 724 pixels. It is not a product screenshot.
- [overlay-controls.png](overlay-controls.png): production native SwiftUI overlay
  rendered through `NSHostingView` on macOS 27 (arm64), using only fictional
  transcript strings. It shows the interaction grip, six placement buttons and
  recording-policy toggle. The 880 × 760 PNG is a 2× rendering of a 440 × 380
  panel from source `32680e03db334a07dcb904cf63acbff2b8eb25cf`; no native overlay
  source changed between that commit and this packaging audit. It is not a
  live meeting or proof of recorder exclusion. The PNG contains no textual
  metadata and no unrelated application UI.

The workflow remains editable Mermaid text, and the wordmark uses plain text.
No generated UI is presented as a real product screenshot.

Hero provenance: supplied GPT-generated conceptual artwork, manually saved by
the project owner as a local file for this documentation update. The original
Library item is unchanged. No exact model version is asserted here.

SHA-256 of the preserved PNG:

```text
67a8626c939b236bac5e1e90ee399772fa90629ce880087f571f167e2e9ea036
```

Use meaningful alt text describing the waveform and layered document forms,
and include the visible caption **Generated conceptual artwork; not a product
screenshot.** Keep any future background derivative separate from this source.

## Packaging

The runtime exporter explicitly includes the hero and reviewed overlay render so
its documentation retains working relative references. Other assets are excluded
unless deliberately allowlisted. See [development](../development.md#runtime-source-export).

Follow the [brand guide](../brand-guide.md). Inspiration:
[Nous Research](https://nousresearch.com/). No Nous seal, logo, illustration,
or font file is bundled, and the project does not claim endorsement.

## Workflow diagram

[workflow.svg](workflow.svg) is an editable, hand-authored vector diagram using
the project palette. It illustrates four deliberate user actions, not product UI
or a performance claim. Its text is also represented in the main README. No
external resources, scripts, fonts, or user data are embedded.
