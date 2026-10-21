# Meetily live JSON bridge

`meetily.py` is an original, unofficial, read-only integration with [Meetily](https://github.com/Zackriya-Solutions/meetily). It uses Python's standard library, not Tauri IPC, a remote API, SQLite polling, audio capture, or a bundled Meetily binary. It does not start or control Meetily.

## Supported persistence format

Source inspection targets Meetily **v0.4.1**, commit `a2cb62e827da7ef59f65064c97233efb2313878e`, specifically [`recording_saver.rs`](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/audio/recording_saver.rs). This is an observed persistence format, not an official versioned external API. No upstream implementation was copied. Meetily's community repository is MIT licensed; its branding, binaries, models and commercial products are not bundled here.

Each immediate recording directory contains `metadata.json` and a complete, atomically replaced `transcripts.json` snapshot. The latter's root `version` must be the string **`"1.0"`**, not numeric `1`. Unknown/missing versions stop delivery with `error="unsupported_schema"`. Metadata supplies `meeting_name`, `status`, `created_at`, and optional `completed_at`; `meeting_id` is not required. Segment fields used are `sequence_id` (nonnegative integer), `text` (string), and `audio_start_time` (finite nonnegative seconds). There is no speaker identity in this format.

## API and opt-in lifecycle

```python
from meetily import MeetilyBridge

bridge = MeetilyBridge()  # or root=an_explicit_local_directory
candidates = bridge.recordings()
# Present active candidates to the user; do not choose the newest automatically.
# After the user explicitly selects a candidate whose status is "recording":
# bridge.attach(selected_id, on_segment, on_status)
# ...
bridge.stop()
```

- `MeetilyBridge(root=None, poll_interval=.5)` does not attach automatically.
- `default_root()` on Windows reads only the `save_folder` setting from `%APPDATA%/com.meetily.ai/recording_preferences.json`. It never logs preferences or accesses other preference values. Missing/invalid preferences fall back to `Path.home()/"Music"/"meetily-recordings"`. Other platforms use that fallback; an explicit root works cross-platform.
- `recordings()` returns dictionaries with exactly `id`, `title`, `status`, `updated_at`. ID is the folder name, title is `meeting_name` or the folder name, updated time is `completed_at` or `created_at` (possibly `None`). Listing includes historical entries for display; callers should filter `status == "recording"`. Listing alone never reads transcripts. Invalid/unreadable entries are skipped.
- `attach(recording_id, on_segment, on_status)` validates a direct child ID and requires current metadata status `recording`; otherwise it raises `ValueError` or a filesystem error. Successful attachment replaces any previous attachment. It starts one daemon polling thread. Callbacks execute on that thread: use a thread-safe handoff to an async/UI loop, keep callbacks short, and do not concurrently manage the lifecycle from multiple threads.
- `on_segment` receives `{id, speaker, text, timestamp, sequence_id}`. ID is `"meetily:" + folder_name + ":" + sequence_id`, speaker is always `"meeting"`, timestamp is `audio_start_time`. **Upsert by ID**: corrected text for a sequence reuses the ID. Unchanged values are not resent. Treat IDs/folder names as private local identifiers.
- Initial attachment emits at most the **last 40** segments, not the whole meeting. Subsequent polls inspect/cache at most the **last 120** entries. A correction outside that window is not emitted. The initial preceding 80 entries are cached silently so they are not inadvertently backfilled on poll two. Reattachment explicitly repeats the initial window; the consumer should still upsert.
- Every JSON file read is bounded to 8 MiB. Snapshots exceeding this limit are not delivered. This limits parsing, but long recordings can consequently become unavailable. No file content is sent over a network by this module.
- Windows reads request read/write/delete sharing and close handles before parsing/callbacks. Windows `MoveFileEx` can nevertheless reject replacing an open destination briefly, even with delete sharing. Synthetic writers use bounded retries; this adapter cannot add retries to an external Meetily build. A real installed-app test must check for upstream snapshot-write errors. No guarantee of zero contention is made.
- Missing files, permission/atomic-replacement races and invalid JSON trigger a retry next poll with `status="listening"`, `error="snapshot_unavailable"`. A successful poll clears that error. No private exception text or paths are put in status messages.
- A `completed`/`error` metadata status drains the current valid snapshot before reporting the terminal status and ending polling. Invalid final snapshots continue retrying until readable or explicitly stopped; this avoids silently dropping final words. Empty recordings with no snapshot require explicit stop.
- `status()` returns a copy with `status`, `backend="meetily"`, `message`, and `error`. Status values are `idle`, `listening`, `completed`, `error`; errors also include `recording_error` and `callback_failed`.
- `stop()` signals cancellation, suppresses callbacks from in-flight reads and joins for at most one second. A callback already running cannot be forcibly interrupted; it may finish afterward. It cannot then change the bridge status on a delayed exception. Reentrant `stop()` from a callback is supported. Lifecycle cancellation does not stop Meetily itself.

## Safety and privacy boundaries

IDs containing separators, drive syntax or traversal are rejected. Recording folders and JSON files that are symlinks or Windows reparse points (including directory junctions) are rejected; containment is rechecked on each read. Use a trusted recordings root: this is not a sandbox against a malicious local process swapping filesystem paths concurrently. The adapter never writes, deletes or renames Meetily files and does not open its database, audio, WebView state, API keys or model data.

**Meetily separately persists its recordings and transcripts.** An in-memory copilot pane does not disable, remove or change that persistence. Listing reveals local meeting names; attaching reads transcript text and invokes caller callbacks. A caller may send that text to another service: require explicit informed consent before any remote-model or web disclosure. Do not publish real recording folders, titles, preferences or transcript fixtures.

## Verification

Run `python -B -m unittest test_meetily -v`. Tests use only synthetic temporary directories (honoring `TMPDIR`/the host's temporary-directory policy) and atomic synthetic snapshot replacement. They cover selection/traversal, symlinks where permitted, actual Windows junction rejection, corrections/upsert, deduplication, missing/invalid snapshots, schema/size bounds, recent-window behavior, final draining, callback errors and cancellation. Windows may skip the symlink test without Developer Mode/admin privileges; the junction test requires no symlink privilege. No test starts Meetily or records audio.

Source/schema compatibility and synthetic runtime tests do not constitute a live recording test of an installed application. A user-approved active recording test remains necessary to verify the installed application's incremental persistence end to end.
