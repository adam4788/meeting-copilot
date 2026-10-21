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

    def test_initial_backfill_40_and_subsequent_window_120(self):
        folder = self.recording()
        self.snapshot(folder, [self.segment(i) for i in range(200)])
        seen = []
        self.bridge.attach(folder.name, seen.append, lambda s: None)
        self.wait(lambda: len(seen) >= 40)
        time.sleep(.05)
        self.assertEqual([s['sequence_id'] for s in seen], list(range(160, 200)))
        self.snapshot(folder, [self.segment(i) for i in range(400)])
        self.wait(lambda: len(seen) >= 160)
        self.assertEqual([s['sequence_id'] for s in seen[40:]], list(range(280, 400)))

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
        with patch('meetily.os', fake_os):
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
