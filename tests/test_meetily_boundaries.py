"""Real snapshot adapter boundaries; no devices, provider, or user data."""
from tests.thread_test_support import mock_capture_preflight
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from meetily import MeetilyBridge
import server


class SnapshotBoundaryTests(unittest.TestCase):
    def setUp(self):
        mock_capture_preflight(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def snapshot(self, recording, count, suffix=''):
        folder = self.root / recording
        folder.mkdir(exist_ok=True)
        (folder / 'metadata.json').write_text(json.dumps({'status': 'recording'}))
        payload = {'version': '1.0', 'segments': [
            {'sequence_id': i, 'audio_start_time': float(i), 'text': f'{recording} words {i}{suffix}'}
            for i in range(count)]}
        staged = folder / 'next.json'
        staged.write_text(json.dumps(payload))
        # The bridge polls transcripts.json continuously; on Windows the
        # atomic replace can briefly collide with an open reader (WinError
        # 5/32) even though readers use share-tolerant handles. Retry only
        # transient sharing violations with a hard deadline, like the
        # test_meetily.py writer helper does.
        import os as _os
        import time as _time
        deadline = _time.monotonic() + 2
        while True:
            try:
                staged.replace(folder / 'transcripts.json')
                break
            except PermissionError as exc:
                if _os.name != 'nt' or getattr(exc, 'winerror', None) not in (5, 32) or _time.monotonic() >= deadline:
                    raise
                _time.sleep(.005)

    def wait_for(self, predicate):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.005)
        self.fail('Snapshot delivery did not reach expected state')

    def test_paused_clear_preserves_speech_added_before_resume(self):
        self.snapshot('a', 3)
        seen = []
        self.bridge.attach('a', seen.append, lambda info: None)
        self.wait_for(lambda: len(seen) == 3)
        self.bridge.stop()
        self.bridge.note_cleared()
        self.snapshot('a', 5)
        resumed = []
        self.bridge.attach('a', resumed.append, lambda info: None)
        self.wait_for(lambda: len(resumed) == 2)
        self.assertEqual([x['sequence_id'] for x in resumed], [3, 4])

    def test_clear_applies_to_its_recording_not_next_selected_recording(self):
        self.snapshot('a', 3)
        self.snapshot('b', 2)
        seen = []
        self.bridge.attach('a', seen.append, lambda info: None)
        self.wait_for(lambda: len(seen) == 3)
        self.bridge.stop()
        self.bridge.note_cleared()
        other = []
        self.bridge.attach('b', other.append, lambda info: None)
        self.wait_for(lambda: len(other) == 2)
        self.assertEqual([x['sequence_id'] for x in other], [0, 1])

    def test_new_meeting_on_same_recording_does_not_backfill_old_speech(self):
        self.snapshot('a', 60)
        state = server.MeetingState(engine_factory=lambda: None)
        state.bridge = self.bridge
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            body = {'consent': True, 'backend': 'meetily', 'recording_id': 'a'}
            self.assertEqual(client.post('/start', json=body).status_code, 200)
            self.wait_for(lambda: len(state.segments) == 40)
            self.assertEqual(client.post('/pause').status_code, 200)
            self.assertEqual(client.post('/start', json=dict(body, new_meeting=True)).status_code, 200)
            time.sleep(.1)
            self.assertEqual(client.get('/status').json()['segments'], [])
            self.snapshot('a', 61)
            self.wait_for(lambda: len(state.segments) == 1)
            self.assertEqual(state.segments[0]['id'], 'meetily:a:60')

    def test_real_clear_route_large_snapshot_never_restores_cleared_words(self):
        self.snapshot('a', 5001)
        state = server.MeetingState(engine_factory=lambda: None)
        state.bridge = self.bridge
        app = FastAPI()
        app.include_router(server.make_router(state))
        with TestClient(app) as client:
            self.assertEqual(client.post('/start', json={
                'consent': True, 'backend': 'meetily', 'recording_id': 'a'}).status_code, 200)
            self.wait_for(lambda: len(state.segments) == 40)
            self.assertEqual(client.post('/clear').status_code, 200)
            self.wait_for(lambda: not getattr(self.bridge, '_pending_clear', False))
            # Force several real snapshot polls after cache eviction pressure.
            time.sleep(.1)
            self.assertEqual(client.get('/status').json()['segments'], [])
            self.snapshot('a', 5002)
            self.wait_for(lambda: len(state.segments) == 1)
            self.assertEqual(state.segments[0]['id'], 'meetily:a:5001')
            self.assertEqual(client.post('/pause').status_code, 200)
            self.assertEqual(client.post('/start', json={
                'consent': True, 'backend': 'meetily', 'recording_id': 'a'}).status_code, 200)
            time.sleep(.1)
            self.assertEqual([x['id'] for x in state.segments], ['meetily:a:5001'])


class LargeInterruptedResumeTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' / 'cache' / 'scratch'
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = MeetilyBridge(self.root, poll_interval=.01)
        self.addCleanup(self.bridge.stop)

    def test_interrupted_6000_segment_scan_resumes_without_duplicates_or_loss(self):
        folder = self.root / 'synthetic'
        folder.mkdir()
        (folder / 'metadata.json').write_text(json.dumps({'version': '1.0', 'meeting_name': 'S', 'status': 'recording', 'created_at': 'x'}), encoding='utf-8')
        total = 6000
        segs = [{'id': str(i), 'text': f'words {i}', 'audio_start_time': float(i), 'audio_end_time': float(i + 1),
                 'duration': 1, 'display_time': '00:01', 'confidence': .9, 'sequence_id': i} for i in range(total)]
        (folder / 'transcripts.json').write_text(json.dumps({'version': '1.0', 'segments': segs[:40], 'total_segments': 40, 'last_updated': 'x'}), encoding='utf-8')
        first = []
        halt_at = [None]
        def gated(segment):
            first.append(segment)
            if halt_at[0] is not None and len(first) >= halt_at[0]:
                self.bridge.stop()
        self.bridge.attach('synthetic', gated, lambda *a: None)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(first) < 40:
            time.sleep(.01)
        (folder / 'transcripts.json').write_text(json.dumps({'version': '1.0', 'segments': segs, 'total_segments': total, 'last_updated': 'x'}), encoding='utf-8')
        halt_at[0] = 40 + 5100
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not self.bridge._event.is_set():
            time.sleep(.01)
        time.sleep(.3)
        self.assertGreaterEqual(len(first), 5140)
        cutoff = len(first)
        second = []
        self.bridge.attach('synthetic', second.append, lambda *a: None)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(second) < total - cutoff:
            time.sleep(.01)
        got = sorted(s['sequence_id'] for s in first + second)
        self.assertEqual(len(got), len(set(got)), 'resumed scan must not duplicate delivered segments')
        self.assertEqual(got, list(range(total)))
