# Documentation assets

The supplied hero has been read and visually inspected from the local PNG.
It is an abstract audio/document illustration, with no product UI, private
meeting information, or embedded product claims. It is used in the README
and brand guide without cropping or changing the original PNG bytes.

| Repository file | Role | Dimensions |
| --- | --- | --- |
| [meeting-copilot-hero.png](meeting-copilot-hero.png) | Supplied GPT-generated conceptual README artwork | 2172 × 724 pixels, 3:1 aspect ratio, PNG, 2,557,766 bytes |

The workflow remains editable Mermaid text, and the wordmark uses plain text.
No additional generated illustration or fake screenshot is needed. Later real
screenshots should identify platform/revision and contain only synthetic data.

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

## Packaging caveat

The repository README uses a relative image path. The current runtime exporter
copies text documentation but omits PNG/SVG assets; its exported README will
therefore have a missing image unless packaging is updated separately. This
documentation branch does not change the runtime exporter. See
[development notes](../development.md#runtime-source-export).

Follow the [brand guide](../brand-guide.md). Inspiration:
[Nous Research](https://nousresearch.com/). No Nous seal, logo, illustration,
or font file is bundled, and the project does not claim endorsement.
