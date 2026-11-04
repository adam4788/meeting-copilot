"""Synthetic, local-only tests for the read-only Meetily adapter."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from meetily import MeetilyBridge


class MeetilyTests(unittest.TestCase):
    def setUp(self):
        # Hermes on Windows may expose an MSYS TMPDIR that native Python ignores.
        # Prefer its existing managed scratch without hard-coding a user's path.
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def write(self, folder, name, value):
        target = folder / name
        temp = folder / ('.' + name + '.tmp')
        temp.write_text(json.dumps(value), encoding='utf-8')
        # Windows MoveFileEx replacement can briefly fail while a reader has the
        # destination open, even with FILE_SHARE_DELETE. Emulate a robust writer:
        # retry only transient Windows sharing/access errors, with a hard deadline.
        deadline = time.monotonic() + 1
        while True:
            try:
                os.replace(temp, target)
                break
            except PermissionError as exc:
                if os.name != 'nt' or getattr(exc, 'winerror', None) not in (5, 32) or time.monotonic() >= deadline:
                    raise
                time.sleep(.005)

    def recording(self, name='synthetic', status='recording'):
        folder = self.root / name
        folder.mkdir()
        self.write(folder, 'metadata.json', {'version': '1.0', 'meeting_name': 'Synthetic example', 'status': status, 'created_at': '2026-01-01T00:00:00Z'})
        return folder

    def snapshot(self, folder, segments, version='1.0'):
        self.write(folder, 'transcripts.json', {'version': version, 'segments': segments, 'total_segments': len(segments), 'last_updated': '2026-01-01T00:00:01Z'})

    def segment(self, seq=1, text='Synthetic words'):
        return {'id': str(seq), 'text': text, 'audio_start_time': float(seq), 'audio_end_time': float(seq + 1), 'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': seq}

    def wait(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.005)
        self.fail('Timed out waiting for adapter')

    def test_macos_default_matches_meetily_snapshot_folder(self):
        with patch('sys.platform', 'darwin'), patch('meetily.Path.home', return_value=self.root):
            self.assertEqual(MeetilyBridge.default_root(), self.root / 'Movies' / 'meetily-recordings')
            self.assertEqual(MeetilyBridge(root=self.root).root, self.root)

    def test_live_snapshot_correction_and_dedup(self):
        folder = self.recording()
        self.snapshot(folder, [self.segment()])
        seen, statuses = [], []
        self.bridge.attach(folder.name, seen.append, statuses.append)
        self.wait(lambda: len(seen) == 1)
        self.assertEqual(seen[0], {'id': 'meetily:synthetic:1', 'speaker': 'meeting', 'text': 'Synthetic words', 'timestamp': 1., 'sequence_id': 1})
        time.sleep(.05)
        self.assertEqual(len(seen), 1)
        self.snapshot(folder, [self.segment(text='Corrected synthetic words')])
        self.wait(lambda: len(seen) == 2)
        self.assertEqual(seen[0]['id'], seen[1]['id'])
        self.assertEqual(seen[1]['text'], 'Corrected synthetic words')
        self.assertTrue(any(s['status'] == 'listening' for s in statuses))

    def test_open_snapshot_permits_source_rename_while_reading(self):
        folder = self.recording()
        self.snapshot(folder, [self.segment(text='Before rename')])
        path = folder / 'transcripts.json'
        with self.bridge._open_snapshot(path) as reader:
            path.rename(folder / 'previous.json')
            self.snapshot(folder, [self.segment(text='New snapshot')])
            old = json.loads(reader.read())
        self.assertEqual(old['segments'][0]['text'], 'Before rename')
        self.assertEqual(self.bridge._read(folder, 'transcripts.json')['segments'][0]['text'], 'New snapshot')

    def test_snapshot_handle_is_closed_before_json_parsing(self):
        folder = self.recording()
        self.snapshot(folder, [self.segment(text='Before replacement')])
        parse = json.loads
        def parser(raw):
            self.snapshot(folder, [self.segment(text='After replacement')])
            return parse(raw)
        with patch('meetily.json.loads', side_effect=parser):
            old = self.bridge._read(folder, 'transcripts.json')
        self.assertEqual(old['segments'][0]['text'], 'Before replacement')
        self.assertEqual(self.bridge._read(folder, 'transcripts.json')['segments'][0]['text'], 'After replacement')

    def test_invalid_segment_types_fail_closed(self):
        folder = self.recording()
        self.snapshot(folder, [self.segment(seq=True)])
        seen = []
        self.bridge.attach(folder.name, seen.append, lambda s: None)
        self.wait(lambda: self.bridge.status()['error'] is not None)
        self.assertEqual(seen, [])

    def test_windows_junction_rejected(self):
        if os.name != 'nt':
            self.skipTest('Windows reparse-point test')
        import subprocess
        folder = self.recording()
        link = self.root / 'junction'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(folder)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        with self.assertRaises(ValueError):
            self.bridge.attach('junction', lambda s: None, lambda s: None)
        self.assertNotIn('junction', [r['id'] for r in self.bridge.recordings()])

    def test_missing_initial_snapshot_retries(self):
        folder = self.recording()
        seen = []
        self.bridge.attach(folder.name, seen.append, lambda s: None)
        self.wait(lambda: self.bridge.status()['error'] == 'snapshot_unavailable')
        self.snapshot(folder, [self.segment()])
        self.wait(lambda: len(seen) == 1)

    def test_status_callback_failure_stops_cleanly(self):
        folder = self.recording()
        self.snapshot(folder, [self.segment()])
        def broken(value):
            raise RuntimeError('Synthetic callback failure')
        self.bridge.attach(folder.name, lambda s: None, broken)
        self.wait(lambda: self.bridge.status()['error'] == 'callback_failed')

    def test_traversal_and_inactive_rejected(self):
        self.recording('finished', 'completed')
        for value in ('../outside', '..', '.', '/outside', 'a/b', 'a\\b', 'C:outside', '', 'finished'):
            with self.subTest(value=value), self.assertRaises((ValueError, OSError)):
                self.bridge.attach(value, lambda s: None, lambda s: None)

    def test_symlink_folder_and_file_rejected(self):
        folder = self.recording()
        link = self.root / 'linked'
        try:
            link.symlink_to(folder, target_is_directory=True)
        except OSError:
            self.skipTest('OS does not permit symlinks')
        with self.assertRaises(ValueError):
            self.bridge.attach('linked', lambda s: None, lambda s: None)
        self.assertNotIn('linked', [r['id'] for r in self.bridge.recordings()])
        (folder / 'transcripts.json').symlink_to(folder / 'metadata.json')
        with self.assertRaises(ValueError):
            self.bridge._read(folder, 'transcripts.json')

    def test_transient_invalid_json_then_completed_final(self):
        folder = self.recording()
        (folder / 'transcripts.json').write_text('{', encoding='utf-8')
        seen = []
        self.bridge.attach(folder.name, seen.append, lambda s: None)
        self.wait(lambda: self.bridge.status()['error'] == 'snapshot_unavailable')
        self.snapshot(folder, [self.segment()])
        self.wait(lambda: len(seen) == 1)
        self.snapshot(folder, [self.segment(), self.segment(2, 'Final synthetic words')])
        self.write(folder, 'metadata.json', {'status': 'completed'})
        self.wait(lambda: self.bridge.status()['status'] == 'completed')
        self.assertEqual([s['sequence_id'] for s in seen], [1, 2])

    def test_unknown_version_and_oversize(self):
        folder = self.recording()
        self.snapshot(folder, [self.segment()], version=1)
        seen = []
        self.bridge.attach(folder.name, seen.append, lambda s: None)
        self.wait(lambda: self.bridge.status()['error'] == 'unsupported_schema')
        self.assertEqual(seen, [])
        (folder / 'transcripts.json').write_bytes(b' ' * (MeetilyBridge.MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            self.bridge._read(folder, 'transcripts.json')

    def test_initial_backfill_40_and_full_detached_recovery(self):
        folder = self.recording()
        self.snapshot(folder, [self.segment(i) for i in range(200)])
        seen = []
        self.bridge.attach(folder.name, seen.append, lambda s: None)
        self.wait(lambda: len(seen) >= 40)
        time.sleep(.05)
        self.assertEqual([s['sequence_id'] for s in seen], list(range(160, 200)))
        # Growth beyond the old 120-segment scan window: every genuinely new
        # segment must emit — a detached gap must never silently vanish.
        self.snapshot(folder, [self.segment(i) for i in range(400)])
        self.wait(lambda: len(seen) >= 240)
        self.assertEqual([s['sequence_id'] for s in seen[40:]], list(range(200, 400)))

    def test_stop_suppresses_inflight_read_callback(self):
        folder = self.recording()
        self.snapshot(folder, [self.segment()])
        entered, release = threading.Event(), threading.Event()
        original = self.bridge._read
        def slow_read(folder, name):
            data = original(folder, name)
            if name == 'transcripts.json':
                entered.set()
                release.wait(3)
            return data
        seen, statuses = [], []
        with patch.object(self.bridge, '_read', slow_read):
            self.bridge.attach(folder.name, seen.append, statuses.append)
            self.assertTrue(entered.wait(2))
            start = time.monotonic()
            self.bridge.stop()
            self.assertLess(time.monotonic() - start, 1.5)
            count = len(statuses)
            release.set()
            self.bridge._thread.join(2)
            self.assertEqual(seen, [])
            self.assertEqual(len(statuses), count)
            self.assertEqual(self.bridge.status()['status'], 'idle')

    def test_default_root_preferences_and_fallback(self):
        prefs = self.root / 'com.meetily.ai'
        prefs.mkdir()
        self.write(prefs, 'recording_preferences.json', {'save_folder': str(self.root / 'recordings'), 'unrelated': 'ignored'})
        from types import SimpleNamespace
        fake_os = SimpleNamespace(name='nt', environ={'APPDATA': str(self.root)})
        with patch('meetily.os', fake_os), patch('sys.platform', 'win32'):
            self.assertEqual(MeetilyBridge.default_root(), self.root / 'recordings')
            (prefs / 'recording_preferences.json').write_text('{', encoding='utf-8')
            self.assertEqual(MeetilyBridge.default_root(), Path.home() / 'Music' / 'meetily-recordings')

    def test_stopped_callback_cannot_change_status(self):
        folder = self.recording()
        self.snapshot(folder, [self.segment()])
        entered, release = threading.Event(), threading.Event()
        def blocking(value):
            entered.set()
            release.wait(3)
            raise RuntimeError('Synthetic delayed failure')
        self.bridge.attach(folder.name, blocking, lambda s: None)
        self.assertTrue(entered.wait(2))
        self.bridge.stop()
        release.set()
        self.bridge._thread.join(2)
        self.assertEqual(self.bridge.status()['status'], 'idle')

    def test_lists_without_autoattaching(self):
        self.recording()
        self.recording('finished', 'completed')
        rows = self.bridge.recordings()
        self.assertEqual({r['id'] for r in rows}, {'synthetic', 'finished'})
        self.assertEqual(set(rows[0]), {'id', 'title', 'status', 'updated_at'})
        self.assertEqual(self.bridge.status()['status'], 'idle')
        self.assertEqual(self.bridge.status()['backend'], 'meetily')


if __name__ == '__main__':
    unittest.main()


class ReattachTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def write(self, folder, name, value):
        (folder / name).write_text(json.dumps(value), encoding='utf-8')

    def test_reattach_after_clear_does_not_restore_cleared_words(self):
        folder = self.root / 'synthetic'
        folder.mkdir()
        self.write(folder, 'metadata.json', {'version': '1.0', 'meeting_name': 'Synthetic', 'status': 'recording', 'created_at': '2026-01-01T00:00:00Z'})
        seg = {'id': '1', 'text': 'pre-clear words', 'audio_start_time': 1.0, 'audio_end_time': 2.0, 'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': 1}
        self.write(folder, 'transcripts.json', {'version': '1.0', 'segments': [seg], 'total_segments': 1, 'last_updated': '2026-01-01T00:00:01Z'})
        first, statuses = [], []
        self.bridge.attach('synthetic', first.append, statuses.append)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(first) < 1:
            time.sleep(.005)
        self.assertEqual(len(first), 1)
        # Clear-equivalent: stop, signal the boundary, then re-attach the
        # same recording. The already-delivered words must not come back.
        self.bridge.stop()
        self.bridge.note_cleared()
        second = []
        self.bridge.attach('synthetic', second.append, statuses.append)
        time.sleep(.2)
        self.assertEqual(second, [])
        # Genuinely new speech still flows.
        seg2 = dict(seg, sequence_id=2, audio_start_time=3.0, text='post-clear words')
        self.write(folder, 'transcripts.json', {'version': '1.0', 'segments': [seg, seg2], 'total_segments': 2, 'last_updated': '2026-01-01T00:00:02Z'})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(second) < 1:
            time.sleep(.005)
        self.assertEqual([s['text'] for s in second], ['post-clear words'])


class CorrectionFlipFlopTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def test_repeated_correction_back_to_earlier_text_still_emits(self):
        folder = self.root / 'synthetic'
        folder.mkdir()
        (folder / 'metadata.json').write_text(json.dumps({'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'}), encoding='utf-8')
        def snap(text):
            (folder / 'transcripts.json').write_text(json.dumps({'version': '1.0', 'segments': [
                {'id': '1', 'text': text, 'audio_start_time': 1.0, 'audio_end_time': 2.0,
                 'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': 1}],
                'total_segments': 1, 'last_updated': 'x'}), encoding='utf-8')
        snap('alpha')
        seen = []
        self.bridge.attach('synthetic', seen.append, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen) < 1:
            time.sleep(.005)
        snap('beta')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen) < 2:
            time.sleep(.005)
        snap('alpha')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen) < 3:
            time.sleep(.005)
        self.assertEqual([s['text'] for s in seen], ['alpha', 'beta', 'alpha'])


class ClearBeyondWindowTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def test_clear_suppresses_beyond_first_window(self):
        folder = self.root / 'synthetic'
        folder.mkdir()
        (folder / 'metadata.json').write_text(json.dumps({'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'}), encoding='utf-8')
        def seg(i):
            return {'id': str(i), 'text': f'words {i}', 'audio_start_time': float(i), 'audio_end_time': float(i + 1),
                    'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': i}
        (folder / 'transcripts.json').write_text(json.dumps({'version': '1.0', 'segments': [seg(i) for i in range(60)], 'total_segments': 60, 'last_updated': 'x'}), encoding='utf-8')
        first = []
        self.bridge.attach('synthetic', first.append, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(first) < 40:
            time.sleep(.005)
        self.assertEqual(len(first), 40)
        # Clear-equivalent re-attach: the 20 older words must stay cleared.
        self.bridge.stop()
        self.bridge.note_cleared()
        second = []
        self.bridge.attach('synthetic', second.append, lambda *a: None)
        time.sleep(.2)
        self.assertEqual(second, [])


class DetachGrowResumeTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def test_detached_period_speech_arrives_on_resume(self):
        folder = self.root / 'synthetic'
        folder.mkdir()
        (folder / 'metadata.json').write_text(json.dumps({'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'}), encoding='utf-8')
        def snap(n):
            segs = [{'id': str(i), 'text': f'words {i}', 'audio_start_time': float(i), 'audio_end_time': float(i + 1),
                     'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': i} for i in range(1, n + 1)]
            (folder / 'transcripts.json').write_text(json.dumps({'version': '1.0', 'segments': segs, 'total_segments': n, 'last_updated': 'x'}), encoding='utf-8')
        snap(1)
        first = []
        self.bridge.attach('synthetic', first.append, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(first) < 1:
            time.sleep(.005)
        self.assertEqual([s['text'] for s in first], ['words 1'])
        self.bridge.stop()
        snap(2)  # speech recorded while detached
        second = []
        self.bridge.attach('synthetic', second.append, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(second) < 1:
            time.sleep(.005)
        # Only the genuinely new detached-period speech arrives; the
        # already-delivered segment is not repeated.
        self.assertEqual([s['text'] for s in second], ['words 2'])


class InterruptionRecoveryTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def test_interrupted_initial_batch_resumes_without_loss(self):
        folder = self.root / 'synthetic'
        folder.mkdir()
        (folder / 'metadata.json').write_text(json.dumps({'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'}), encoding='utf-8')
        segs = [{'id': str(i), 'text': f'words {i}', 'audio_start_time': float(i), 'audio_end_time': float(i + 1),
                 'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': i} for i in range(50)]
        (folder / 'transcripts.json').write_text(json.dumps({'version': '1.0', 'segments': segs, 'total_segments': 50, 'last_updated': 'x'}), encoding='utf-8')
        first = []
        def gated(segment):
            first.append(segment)
            if len(first) == 5:
                # Interrupt mid-batch: reentrant stop is supported.
                self.bridge.stop()
        self.bridge.attach('synthetic', gated, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.bridge._event.is_set():
            time.sleep(.005)
        time.sleep(.1)
        delivered_first = len(first)
        self.assertGreaterEqual(delivered_first, 1)
        # Resume with no clear: every undelivered segment must still arrive.
        second = []
        self.bridge.attach('synthetic', second.append, lambda *a: None)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(second) < 50 - delivered_first:
            time.sleep(.005)
        got = sorted(s['sequence_id'] for s in first + second)
        self.assertEqual(got, list(range(50)))


class InterruptedFlipFlopTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def test_flip_flop_after_interrupted_delivery_is_not_lost(self):
        folder = self.root / 'synthetic'
        folder.mkdir()
        (folder / 'metadata.json').write_text(json.dumps({'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'}), encoding='utf-8')
        def snap(text):
            (folder / 'transcripts.json').write_text(json.dumps({'version': '1.0', 'segments': [
                {'id': '1', 'text': text, 'audio_start_time': 1.0, 'audio_end_time': 2.0,
                 'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': 1}],
                'total_segments': 1, 'last_updated': 'x'}), encoding='utf-8')
        snap('alpha')
        first = []
        self.bridge.attach('synthetic', first.append, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(first) < 1:
            time.sleep(.005)
        self.assertEqual([s['text'] for s in first], ['alpha'])
        # Beta is delivered, then the bridge stops mid-scan: the baseline
        # stays at alpha while the delivery record moves to beta. Stop the
        # first poll thread first so it cannot consume beta before the
        # re-attach (a load-dependent race, not test intent).
        self.bridge.stop()
        snap('beta')
        second = []
        def gated(segment):
            second.append(segment)
            self.bridge.stop()
        self.bridge.attach('synthetic', gated, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.bridge._event.is_set():
            time.sleep(.005)
        time.sleep(.1)
        self.assertEqual([s['text'] for s in second], ['beta'])
        # The snapshot flips back: alpha is new information again.
        snap('alpha')
        third = []
        self.bridge.attach('synthetic', third.append, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(third) < 1:
            time.sleep(.005)
        self.assertEqual([s['text'] for s in third], ['alpha'])


class ClearMemoryReleaseTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def _write(self, folder, name, value):
        (folder / name).write_text(json.dumps(value), encoding='utf-8')

    def test_note_cleared_releases_recording_text_immediately(self):
        folder = self.root / 'synthetic'
        folder.mkdir()
        self._write(folder, 'metadata.json', {'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'})
        self._write(folder, 'transcripts.json', {'version': '1.0', 'segments': [
            {'id': '1', 'text': 'secret words', 'audio_start_time': 1.0, 'audio_end_time': 2.0,
             'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': 1}],
            'total_segments': 1, 'last_updated': 'x'})
        seen = []
        self.bridge.attach('synthetic', seen.append, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen) < 1:
            time.sleep(.005)
        self.bridge.note_cleared('synthetic')
        held = [json.dumps(v) for store in (self.bridge._withheld, self.bridge._emitted_seq) for v in store.values()]
        self.assertFalse(any('secret words' in blob for blob in held))

    def test_note_cleared_without_readable_snapshot_still_purges_and_fails_closed(self):
        folder = self.root / 'synthetic'
        folder.mkdir()
        self._write(folder, 'metadata.json', {'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'})
        self._write(folder, 'transcripts.json', {'version': '1.0', 'segments': [
            {'id': '1', 'text': 'secret words', 'audio_start_time': 1.0, 'audio_end_time': 2.0,
             'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': 1}],
            'total_segments': 1, 'last_updated': 'x'})
        seen = []
        self.bridge.attach('synthetic', seen.append, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen) < 1:
            time.sleep(.005)
        (folder / 'transcripts.json').unlink()
        self.bridge.note_cleared('synthetic')
        held = [json.dumps(v) for store in (self.bridge._withheld, self.bridge._emitted_seq) for v in store.values()]
        self.assertFalse(any('secret words' in blob for blob in held))
        self.assertIn('synthetic', self.bridge._pending_clear)


class WithheldFlipFlopTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def _write(self, folder, name, value):
        (folder / name).write_text(json.dumps(value), encoding='utf-8')

    def _snap(self, folder, texts):
        self._write(folder, 'transcripts.json', {'version': '1.0', 'segments': [
            {'id': str(i), 'text': t, 'audio_start_time': float(i), 'audio_end_time': float(i + 1),
             'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': i}
            for i, t in enumerate(texts)], 'total_segments': len(texts), 'last_updated': 'x'})

    def test_withheld_correction_flip_flop_still_emits(self):
        folder = self.root / 'synthetic'
        folder.mkdir()
        self._write(folder, 'metadata.json', {'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'})
        base = [f'words {i}' for i in range(41)]
        self._snap(folder, base)
        seen = []
        self.bridge.attach('synthetic', seen.append, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen) < 40:
            time.sleep(.005)
        self.assertEqual(len(seen), 40)
        # Correct withheld sequence 0, then flip it back to the original.
        fixed = list(base)
        fixed[0] = 'corrected opener'
        self._snap(folder, fixed)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen) < 41:
            time.sleep(.005)
        self.assertEqual(seen[-1]['text'], 'corrected opener')
        self._snap(folder, base)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen) < 42:
            time.sleep(.005)
        self.assertEqual(seen[-1]['text'], 'words 0')


class EvictedClearBoundaryTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def _write(self, folder, name, value):
        (folder / name).write_text(json.dumps(value), encoding='utf-8')

    def _recording(self, name, texts):
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        self._write(folder, 'metadata.json', {'version': '1.0', 'meeting_name': name, 'status': 'recording', 'created_at': 'x'})
        self._write(folder, 'transcripts.json', {'version': '1.0', 'segments': [
            {'id': str(i), 'text': t, 'audio_start_time': float(i), 'audio_end_time': float(i + 1),
             'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': i}
            for i, t in enumerate(texts)], 'total_segments': len(texts), 'last_updated': 'x'})
        return folder

    def test_eviction_never_resurrects_cleared_content(self):
        self._recording('a', [f'a{i}' for i in range(5)])
        seen = []
        self.bridge.attach('a', seen.append, lambda *a: None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen) < 5:
            time.sleep(.005)
        self.bridge.note_cleared('a')
        # Push 'a' out of the bounded bulk caches with other recordings.
        for i in range(33):
            name = f'filler{i}'
            self._recording(name, [f'{name} words'])
            got = []
            self.bridge.attach(name, got.append, lambda *a: None)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and len(got) < 1:
                time.sleep(.005)
        self.assertNotIn('a', self.bridge._folder_order)
        # Re-attaching the cleared recording emits nothing old.
        second = []
        self.bridge.attach('a', second.append, lambda *a: None)
        time.sleep(.2)
        self.assertEqual(second, [])
        # Genuinely new speech still flows.
        self._recording('a', [f'a{i}' for i in range(5)] + ['a new'])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(second) < 1:
            time.sleep(.005)
        self.assertEqual([s['text'] for s in second], ['a new'])


class InFlightClearTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def _write(self, folder, name, value):
        (folder / name).write_text(json.dumps(value), encoding='utf-8')

    def test_in_flight_callback_is_not_rerecorded_while_clear_pending(self):
        import threading
        folder = self.root / 'synthetic'
        folder.mkdir()
        self._write(folder, 'metadata.json', {'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'})
        self._write(folder, 'transcripts.json', {'version': '1.0', 'segments': [
            {'id': '1', 'text': 'pre-clear words', 'audio_start_time': 1.0, 'audio_end_time': 2.0,
             'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': 1}],
            'total_segments': 1, 'last_updated': 'x'})
        release = threading.Event()
        entered = threading.Event()
        delivered = []
        def blocking(segment):
            delivered.append(segment)
            entered.set()
            release.wait(5)
        self.bridge.attach('synthetic', blocking, lambda *a: None)
        self.assertTrue(entered.wait(5))
        # Snapshot goes unreadable, then Clear lands mid-callback.
        (folder / 'transcripts.json').unlink()
        self.bridge.note_cleared('synthetic')
        self.assertIn('synthetic', self.bridge._pending_clear)
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.bridge._event.is_set():
            time.sleep(.01)
        self.assertEqual(len(delivered), 1)
        held = [json.dumps(v) for v in self.bridge._emitted_seq.values()]
        self.assertFalse(any('pre-clear words' in blob for blob in held),
                         'in-flight cleared text must not re-enter the delivery record')


class WithheldPurgeTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def test_completed_scan_after_clear_leaves_no_cleared_text(self):
        import threading
        folder = self.root / 'synthetic'
        folder.mkdir()
        (folder / 'metadata.json').write_text(json.dumps(
            {'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'}), encoding='utf-8')
        texts = [f'words {i}' for i in range(41)]
        (folder / 'transcripts.json').write_text(json.dumps(
            {'version': '1.0', 'segments': [
                {'id': str(i), 'text': t, 'audio_start_time': float(i), 'audio_end_time': float(i + 1),
                 'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': i}
                for i, t in enumerate(texts)],
             'total_segments': 41, 'last_updated': 'x'}), encoding='utf-8')
        release = threading.Event()
        entered = threading.Event()
        delivered = []
        def blocking(segment):
            delivered.append(segment)
            if len(delivered) == 1:
                entered.set()
                release.wait(5)
        self.bridge.attach('synthetic', blocking, lambda *a: None)
        self.assertTrue(entered.wait(5))
        self.bridge.note_cleared('synthetic')
        release.set()
        # Only the pre-clear callback (already invoking when Clear landed)
        # is delivered; the retired scan invokes nothing further, and later
        # scans suppress everything at/below the mark.
        time.sleep(.5)
        self.assertEqual(len(delivered), 1)
        held = [json.dumps(v) for v in list(self.bridge._withheld.values()) + list(self.bridge._emitted_seq.values())]
        self.assertFalse(any('words 0' in blob for blob in held),
                         'cleared text must not linger in withheld or delivery memory')


class PendingConsumeTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def _snap(self, folder, texts):
        (folder / 'transcripts.json').write_text(json.dumps(
            {'version': '1.0', 'segments': [
                {'id': str(i), 'text': t, 'audio_start_time': float(i), 'audio_end_time': float(i + 1),
                 'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': i}
                for i, t in enumerate(texts)],
             'total_segments': len(texts), 'last_updated': 'x'}), encoding='utf-8')

    def test_stale_scan_cannot_consume_pending_with_stale_max(self):
        import threading
        folder = self.root / 'synthetic'
        folder.mkdir()
        (folder / 'metadata.json').write_text(json.dumps(
            {'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'}), encoding='utf-8')
        self._snap(folder, ['old words 0', 'old words 1'])
        gate = threading.Event()
        entered = threading.Event()
        real_read = self.bridge._read
        def gated_read(f, name):
            data = real_read(f, name)
            if name == 'transcripts.json' and not entered.is_set():
                entered.set()
                gate.wait(5)
            return data
        delivered = []
        self.bridge._read = gated_read
        try:
            poll_stop = threading.Event()
            worker = threading.Thread(target=self.bridge._poll,
                                      args=(folder, poll_stop, delivered.append, lambda *a: None),
                                      daemon=True)
            worker.start()
            self.assertTrue(entered.wait(5))
            # Clear lands while the scan holds its pre-clear snapshot; the
            # snapshot is unreadable at Clear time, then grows.
            (folder / 'transcripts.json').unlink()
            self.bridge._recording_id = 'synthetic'
            self.bridge.note_cleared('synthetic')
            self.assertIn('synthetic', self.bridge._pending_clear)
            (folder / 'transcripts.json').write_text(json.dumps(
                {'version': '1.0', 'segments': [
                    {'id': 'k1', 'text': 'old words 1', 'audio_start_time': 1.0,
                     'audio_end_time': 2.0, 'duration': 1, 'display_time': '00:01',
                     'confidence': .9, 'sequence_id': 1},
                    {'id': 'k2', 'text': 'gap words 2', 'audio_start_time': 2.0,
                     'audio_end_time': 3.0, 'duration': 1, 'display_time': '00:02',
                     'confidence': .9, 'sequence_id': 2}],
                 'total_segments': 2, 'last_updated': 'x'}), encoding='utf-8')
            gate.set()
            # Let one fresh scan run so it (not the stale one) consumes the
            # pending boundary, then stop the worker promptly.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and 'synthetic' in self.bridge._pending_clear:
                time.sleep(.01)
            poll_stop.set()
            worker.join(5)
        finally:
            poll_stop.set()
            self.bridge._read = real_read
        # The stale scan must not consume the boundary with max 1: the next
        # valid snapshot sets the cutoff to its own max, suppressing the
        # gap speech as potentially pre-clear context. The retired scan
        # invokes nothing — not even its already-read old speech.
        self.assertNotIn('synthetic', self.bridge._pending_clear)
        self.assertEqual(self.bridge._clear_high_water.get('synthetic'), 2)
        self.assertEqual(delivered, [])


class NotifyGapTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def test_clear_between_eligibility_and_callback_suppresses_invocation(self):
        import threading
        folder = self.root / 'synthetic'
        folder.mkdir()
        (folder / 'metadata.json').write_text(json.dumps(
            {'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'}), encoding='utf-8')
        (folder / 'transcripts.json').write_text(json.dumps(
            {'version': '1.0', 'segments': [
                {'id': '1', 'text': 'cleared speech', 'audio_start_time': 1.0, 'audio_end_time': 2.0,
                 'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': 1}],
             'total_segments': 1, 'last_updated': 'x'}), encoding='utf-8')
        entered = threading.Event()
        gate = threading.Event()
        delivered = []
        real_notify = self.bridge._notify
        notify_done = threading.Event()
        def hooked(event, callback, value, scan_epoch=None, folder_name=None):
            parked = isinstance(value, dict) and 'sequence_id' in value
            if parked:
                # Park only segment delivery (not the initial status ping):
                # this segment passed eligibility and is parked in the gap
                # between the check and the invocation.
                entered.set()
                gate.wait(5)
            try:
                return real_notify(event, callback, value, scan_epoch, folder_name)
            finally:
                if parked:
                    notify_done.set()
        self.bridge._notify = hooked
        try:
            poll_stop = threading.Event()
            worker = threading.Thread(target=self.bridge._poll,
                                      args=(folder, poll_stop, delivered.append, lambda *a: None),
                                      daemon=True)
            worker.start()
            # The scan passed eligibility and parked inside _notify: Clear now.
            self.assertTrue(entered.wait(5))
            self.bridge._recording_id = 'synthetic'
            self.bridge.note_cleared('synthetic')
            gate.set()
            # The suppression must come from the epoch gate with the worker
            # still running — not from the stop event below, which is set
            # only after the notify call has completed.
            self.assertTrue(notify_done.wait(5))
            poll_stop.set()
            worker.join(5)
        finally:
            poll_stop.set()
            self.bridge._notify = real_notify
        self.assertEqual(delivered, [])


class ConcurrentClearTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def test_concurrent_clears_leave_newest_boundary(self):
        import threading
        folder = self.root / 'synthetic'
        folder.mkdir()
        (folder / 'metadata.json').write_text(json.dumps(
            {'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'}), encoding='utf-8')
        (folder / 'transcripts.json').write_text(json.dumps(
            {'version': '1.0', 'segments': [
                {'id': str(i), 'text': f'words {i}', 'audio_start_time': float(i),
                 'audio_end_time': float(i + 1), 'duration': 1, 'display_time': '00:01',
                 'confidence': .9, 'sequence_id': i} for i in range(11)],
             'total_segments': 11, 'last_updated': 'x'}), encoding='utf-8')
        self.bridge._recording_id = 'synthetic'
        # A slow first Clear holds the boundary lock while a second Clear
        # arrives on an unreadable snapshot: the lock serializes them, so the
        # second (newer) boundary installs last and must win.
        slow = threading.Event()
        go = threading.Event()
        real_read = self.bridge._read
        def gated_read(f, name):
            data = real_read(f, name)
            if name == 'transcripts.json' and not slow.is_set():
                slow.set()
                go.wait(5)
            return data
        self.bridge._read = gated_read
        try:
            first = threading.Thread(target=self.bridge.note_cleared, args=('synthetic',), daemon=True)
            first.start()
            self.assertTrue(slow.wait(5))
            (folder / 'transcripts.json').unlink()
            # The second Clear runs on its own thread while the first is
            # parked in its read holding the boundary lock.
            second = threading.Thread(target=self.bridge.note_cleared, args=('synthetic',), daemon=True)
            second.start()
            # Give an unserialized second Clear time to finish: if it is not
            # lock-blocked behind the first, it completes here and the
            # assertion below catches the missing serialization.
            time.sleep(0.5)
            self.assertTrue(second.is_alive(), 'second Clear must block on the boundary lock')
            go.set()
            first.join(5)
            second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
        finally:
            self.bridge._read = real_read
        # Newest boundary wins: pending from the unreadable Clear survives;
        # the stale readable cutoff must not discard it.
        self.assertIn('synthetic', self.bridge._pending_clear)


class PastTheCheckContractTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def test_invocation_past_final_check_completes(self):
        # Contract pin: suppression is decided at the last pre-invocation
        # check. A callback already past that check when Clear lands still
        # completes (standard callback semantics — Clear cannot un-invoke
        # entered user code, and holding the cache lock across arbitrary
        # callbacks would stall Clear behind a stuck consumer). Do not
        # "fix" this into lock-held invocation: the deadlock runs
        # clear-path server-lock -> cache-lock against callback-path
        # cache-lock -> server-lock.
        import threading
        folder = self.root / 'synthetic'
        folder.mkdir()
        (folder / 'metadata.json').write_text(json.dumps(
            {'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'}), encoding='utf-8')
        (folder / 'transcripts.json').write_text(json.dumps(
            {'version': '1.0', 'segments': [
                {'id': '1', 'text': 'entered speech', 'audio_start_time': 1.0, 'audio_end_time': 2.0,
                 'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': 1}],
             'total_segments': 1, 'last_updated': 'x'}), encoding='utf-8')
        entered = threading.Event()
        gate = threading.Event()
        delivered = []
        real_notify = self.bridge._notify
        def hooked(event, callback, value, scan_epoch=None, folder_name=None):
            if isinstance(value, dict) and 'sequence_id' in value:
                def parking(value, _inner=callback):
                    entered.set()
                    gate.wait(5)
                    _inner(value)
                callback = parking
            return real_notify(event, callback, value, scan_epoch, folder_name)
        self.bridge._notify = hooked
        try:
            poll_stop = threading.Event()
            worker = threading.Thread(target=self.bridge._poll,
                                      args=(folder, poll_stop, delivered.append, lambda *a: None),
                                      daemon=True)
            worker.start()
            self.assertTrue(entered.wait(5))
            # Clear lands after the invocation entered user code.
            self.bridge._recording_id = 'synthetic'
            self.bridge.note_cleared('synthetic')
            gate.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not delivered:
                time.sleep(.01)
            poll_stop.set()
            worker.join(5)
        finally:
            poll_stop.set()
            self.bridge._notify = real_notify
        self.assertEqual([s.get('text') for s in delivered], ['entered speech'])
