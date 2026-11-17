"""Synthetic storage/lifecycle coverage; no devices or real meeting content."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
from tests.thread_test_support import mock_capture_preflight

from recording_session import RecordingSession


class FakeCapture:
    def __init__(self):
        self.directory = None
        self.active = False
        self.stuck = False

    def set_recording_directory(self, directory):
        self.directory = directory

    def start(self, on_segment, on_status):
        self.active = True
        self.segment = on_segment
        if self.directory:
            with wave.open(str(self.directory / 'microphone.wav'), 'wb') as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                out.writeframes(b'\0\0' * 160)
        self.segment({'text': 'Synthetic first segment', 'timestamp': 1, 'speaker': 'you'})

    def stop(self):
        if self.stuck:
            raise RuntimeError('not stopped')
        self.active = False
        if hasattr(self, 'segment'):
            self.segment({'text': 'Synthetic tail', 'timestamp': 2, 'speaker': 'you'})

    def status(self):
        return {'running': self.active, 'stop_confirmed': not self.active}


class SavedRecordingTests(unittest.TestCase):
    def setUp(self):
        mock_capture_preflight(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'recordings'
        self.factory = patch('recording_session.RecordingSession',
            side_effect=lambda **kw: RecordingSession(root=self.root, **kw))
        self.factory.start()
        self.addCleanup(self.factory.stop)
        import server
        self.state = server.MeetingState(audio_factory=FakeCapture)
        self.addCleanup(self.state.pause)

    def start(self, **kw):
        return self.state.start(True, backend='native', **kw)

    def test_listening_without_opt_in_never_creates_storage(self):
        self.start()
        self.state.pause()
        self.assertFalse(self.root.exists())

    def test_record_saves_transcript_tail_and_audio_and_reports_directory(self):
        self.start(save_recording=True)
        record = self.state.saved_recording
        self.assertEqual(record.status()['state'], 'recording')
        self.state.pause()
        self.assertEqual(record.status()['state'], 'saved')
        rows = [json.loads(x) for x in (record.directory / 'transcript.jsonl').read_text().splitlines()]
        self.assertEqual([r['timestamp'] for r in rows], [1, 2])
        manifest = json.loads((record.directory / 'manifest.json').read_text())
        self.assertEqual(manifest['audio_files'], ['microphone.wav'])
        self.assertEqual(self.state.status()['saved_recording']['id'], record.id)
        self.start()  # Listening again must not append to or record into old bundle.
        self.assertIsNone(self.state.audio.directory)
        self.state.pause()
        self.assertEqual(len((record.directory / 'transcript.jsonl').read_text().splitlines()), 2)

    def test_recordings_are_unique_and_prior_bundle_survives(self):
        self.start(save_recording=True)
        first = self.state.saved_recording.directory
        self.state.pause()
        self.start(save_recording=True)
        self.assertNotEqual(first, self.state.saved_recording.directory)
        self.assertTrue((first / 'manifest.json').exists())

    def test_meetily_rejected_and_consent_required(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self.state.start(False, backend='native', save_recording=True)
        with self.assertRaises(HTTPException):
            self.state.start(True, backend='meetily', recording_id='synthetic', save_recording=True)
        self.assertFalse(self.root.exists())

    def test_clear_stops_record_and_does_not_delete_saved_bundle(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import server
        app = FastAPI()
        app.include_router(server.make_router(self.state))
        self.start(save_recording=True)
        record = self.state.saved_recording
        with TestClient(app) as client:
            self.assertEqual(client.post('/clear').status_code, 200)
        self.assertFalse(self.state.listening)
        self.assertFalse(record.active)
        self.assertTrue((record.directory / 'transcript.jsonl').exists())
        self.assertFalse(self.state.segments)

    def test_incomplete_transcription_is_not_presented_as_complete(self):
        self.start(save_recording=True)
        self.state.capture_info['worker_dropped_chunks'] = 1
        self.state.pause()
        self.assertEqual(self.state.saved_recording.status()['state'], 'partial')

    def test_uncertain_stop_does_not_finalize_recording(self):
        self.start(save_recording=True)
        self.state.audio.stuck = True
        with patch.object(self.state, '_start_stop_retry_locked'):
            self.assertFalse(self.state.pause()['stop_confirmed'])
        self.assertTrue(self.state.saved_recording.active)
        self.state.audio.stuck = False
        self.state.pause()
        self.assertFalse(self.state.saved_recording.active)

    def test_transcript_bound_and_no_audio_report_partial(self):
        record = RecordingSession(root=self.root, backend='native', source='microphone', provider='local')
        with patch('recording_session.MAX_TRANSCRIPT_BYTES', 1):
            with self.assertRaises(RuntimeError):
                record.append({'text': 'Synthetic'})
        record.finish()
        self.assertEqual(record.status()['state'], 'partial')

    def test_rejects_symbolic_storage_root(self):
        link = Path(self.temp.name) / 'link'
        link.symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(ValueError):
            RecordingSession(root=link, backend='native', source='microphone', provider='local')

    def test_archive_preserves_segment_beyond_live_buffer_limit(self):
        self.start(save_recording=True)
        self.state.audio.segment({'text': 'X' * 8000, 'timestamp': 3})
        self.state.pause()
        rows = [json.loads(x) for x in (self.state.saved_recording.directory / 'transcript.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows[1]['text']), 8000)

    def test_finalize_io_failure_does_not_claim_audio_still_active(self):
        self.start(save_recording=True)
        record = self.state.saved_recording
        original = record.stream
        class BrokenStream:
            def flush(self):
                raise OSError('synthetic disk full')
            def close(self):
                original.close()
                raise OSError('synthetic close error')
        record.stream = BrokenStream()
        self.assertTrue(self.state.pause()['stop_confirmed'])
        self.assertEqual(record.status()['state'], 'partial')
