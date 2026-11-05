# Demo guide

Use fictional documents and speech. The offline demo verifies retrieval and
source mapping without a live provider or audio capture. The Desktop walkthrough
is a procedure to try, not a claim that a live account or Windows meeting flow
has passed.

## Offline: one document question

From a source checkout with Python 3.11 or 3.12:

```sh
python ask.py --docs sample-docs --list-projects
python ask.py --docs sample-docs --project acme --search "budget approval"
python ask.py --docs sample-docs --project acme --provider fake --question "When is finance sign-off needed?"
```

The included [launch plan](../sample-docs/acme/launch.md) says that budget approval
needs finance sign-off before August. Compare the returned extract and source
with that file. The `fake` provider is an extractive demonstration, not a local
language model or a live account test. No microphone or API key is needed.

For a fuller synthetic fixture demonstration:

```sh
python demo_standalone.py
```

That script creates temporary sample documents and transcript strings, injects
a fake completion provider, disables history, and guards against web calls. It
checks citations, source mapping, safe provider failures, and path boundaries.
Its printed assertions are the verification result; there is no recorded meeting.

## Desktop: ask about a fictional project

1. Complete [installation and BYOK setup](getting-started.md). Keep capture
   paused. Choose a provider and an exact model ID in **Answer setup**.
2. Create `copilot-sample/launch.md` beneath your configured project root using
   only this fictional text:

   ```text
   # Sample launch plan
   The sample launch review is on Friday.
   Finance approval is required before the sample launch review.
   ```

3. Select **copilot-sample** in **Search scope**. Keep **Exclude past sessions**
   checked, automatic web fallback off, and the specific web query empty.
4. Ask **What approval is required before the sample launch review?** and choose
   **Suggest answer**. This sends a provider request with selected context and
   may incur billing.
5. Open **Evidence**. Confirm the sample file is present and the answer matches
   its text. A citation alone does not establish correctness. If the provider
   fails, inspect setup and readiness instead of substituting a fake success.

## Optional: add fictional meeting context

Only on a prepared platform with participant permission, select the intended
transcript source, confirm **Permission check**, and explicitly start capture.
Say: **The sample review moved to Monday. Finance still needs to approve it.**
Then type **When is the sample review, and what approval is required?** and ask.

Inspect the transcript and evidence for the changed date and any conflict with
the document. This is a manual accuracy check; no exact answer is promised.
Pause afterward and verify stop status. Do not capture a private conversation
or treat this short exercise as a long-meeting reliability test.

Windows native capture remains experimental. macOS native checks have reported
dropped frames. See [status](../README.md#current-status),
[native loss](native-backpressure.md), and [privacy](../SECURITY.md).

## Images and demo evidence

Generated hero art is conceptual project artwork, not an actual Hermes screen
or a verified feature demonstration. Product screenshots, if added, must come
from an observed interface with synthetic data and identify platform and revision.
Do not substitute generated UI for a real screenshot.
