"""Synthetic PCM only. No devices, provider calls, or OS permission prompts."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np
from recording_audio import AudioArchive, RecordingAudioError, recording_directory


class RecordingAudioTests(unittest.TestCase):
    def test_per_source_wav_and_timeline_survive_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive = AudioArchive(root)
            archive.write('you', np.array([0, .5, -1], dtype=np.float32), 123.5)
            archive.write('remote', np.array([1, -2], dtype=np.float32), 124)
            archive.write('you', np.zeros(4, dtype=np.float32), 125)
            # Header is usable even before graceful close.
            with wave.open(str(root / 'microphone.wav')) as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()), (1, 2, 16000, 7))
            archive.close()
            archive.close()
            with wave.open(str(root / 'system.wav')) as wav:
                self.assertEqual(wav.getnframes(), 2)
            entries = [json.loads(line) for line in (root / 'audio-timeline.jsonl').read_text().splitlines()]
            self.assertEqual([item['sample_offset'] for item in entries], [0, 0, 3])
            self.assertEqual(entries[0]['timestamp'], 123.5)
            if os.name == 'posix':
                self.assertEqual((root / 'microphone.wav').stat().st_mode & 0o777, 0o600)

    def test_limit_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive = AudioArchive(root, max_bytes=4)
            archive.write('you', np.zeros(2), 1)
            with self.assertRaises(RecordingAudioError):
                archive.write('you', np.zeros(1), 2)
            archive.close()
            other = AudioArchive(root)
            with self.assertRaises(RecordingAudioError):
                other.write('you', np.zeros(2), 1)
            other.close()
            with wave.open(str(root / 'microphone.wav')) as wav:
                self.assertEqual(wav.getnframes(), 2)

    def test_bad_pcm_names_and_closed_archive_rejected(self):
        for source, values, timestamp in [('you', [float('nan')], 1), ('../escape', [0], 1), ('you', [0], float('inf'))]:
            with tempfile.TemporaryDirectory() as tmp:
                archive = AudioArchive(Path(tmp).resolve())
                with self.assertRaises(RecordingAudioError):
                    archive.write(source, np.array(values), timestamp)
                archive.close()
                self.assertFalse(list(Path(tmp).iterdir()))
        with tempfile.TemporaryDirectory() as tmp:
            archive = AudioArchive(Path(tmp).resolve())
            archive.close()
            with self.assertRaises(RecordingAudioError):
                archive.write('you', np.zeros(1), 1)

    def test_directory_and_adapter_opt_in(self):
        self.assertIsNone(recording_directory(None))
        with self.assertRaises(RecordingAudioError):
            recording_directory('.')
        from audio import AudioCapture
        from macos_audio import MacOSAudioCapture
        for capture in [AudioCapture(), MacOSAudioCapture()]:
            self.assertIsNone(capture._recording_directory)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                capture.set_recording_directory(root)
                self.assertEqual(capture._recording_directory, root)
                self.assertFalse(list(root.iterdir()))
                capture.set_recording_directory(None)
                self.assertIsNone(capture._recording_directory)

    def test_worker_archives_short_tail_without_asr_text(self):
        import base64
        import subprocess
        import sys
        from unittest.mock import patch
        from audio_worker import run_capture
        frames = [{'type': 'pcm', 'source': source, 'rate': 16000,
                   'samples': 100, 'timestamp': 10 + i,
                   'pcm': base64.b64encode(np.zeros(100, dtype='<f4').tobytes()).decode()}
                  for i, source in enumerate(['microphone', 'system', 'microphone'])]
        lines = ''.join(json.dumps(frame) + '\n' for frame in frames)
        script = 'import sys; sys.stdout.write(' + repr(lines) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, '-c', script], **kw)):
                run_capture(object(), 'both', 'en', helper=Path('/fixture'), recording_directory=root)
            with wave.open(str(root / 'microphone.wav')) as wav:
                self.assertEqual(wav.getnframes(), 200)
            with wave.open(str(root / 'system.wav')) as wav:
                self.assertEqual(wav.getnframes(), 100)
            self.assertFalse(any(event.get('type') == 'segment' for event in outputs))
            self.assertTrue(any(event.get('sub_second_dropped_chunks') == 2 for event in outputs))

    def test_worker_storage_failure_surfaces_recording_error(self):
        import base64
        import subprocess
        import sys
        from unittest.mock import patch
        from audio_worker import run_capture
        line = json.dumps({'type': 'pcm', 'source': 'microphone', 'rate': 16000,
                           'samples': 100, 'timestamp': 1,
                           'pcm': base64.b64encode(np.zeros(100, dtype='<f4').tobytes()).decode()}) + '\n'
        script = 'import sys; sys.stdout.write(' + repr(line) + '); sys.stdout.flush()'
        real_popen = subprocess.Popen
        outputs = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'audio-timeline.jsonl').write_text('existing data')
            with patch('audio_worker.emit', outputs.append), patch('audio_worker.subprocess.Popen', lambda c, **kw: real_popen([sys.executable, '-c', script], **kw)):
                run_capture(object(), 'microphone', 'en', helper=Path('/fixture'), recording_directory=root)
            self.assertTrue(any(event.get('type') == 'error' and event.get('code') == 'recording' for event in outputs), outputs)
            self.assertEqual((root / 'audio-timeline.jsonl').read_text(), 'existing data')

    def test_native_stop_reports_discarded_queue_exactly_once(self):
        from audio import AudioCapture
        with tempfile.TemporaryDirectory() as tmp:
            capture = AudioCapture()
            capture._archive = AudioArchive(Path(tmp).resolve())
            statuses = []
            capture._on_status = statuses.append
            capture._enqueue(('you', np.zeros(16000), 1))
            capture.stop()
            capture.stop()
            self.assertEqual(capture.status()['worker_dropped_chunks'], 1)
            self.assertIn('Some words may be missing', statuses[-1]['warning'])

    def test_native_stop_accounts_for_residual_source_tail(self):
        from audio import AudioCapture
        from types import SimpleNamespace as NS
        with tempfile.TemporaryDirectory() as tmp:
            capture = AudioCapture()
            capture._archive = AudioArchive(Path(tmp).resolve())
            class Recorder:
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def record(self, **kwargs):
                    capture._stop.set()
                    return np.zeros((1600, 1), dtype=np.float32)
            capture._capture('you', NS(recorder=lambda **kwargs: Recorder()))
            capture.stop()
            self.assertEqual(capture.status()['sub_second_dropped_chunks'], 1)
            self.assertEqual(capture.status()['worker_dropped_chunks'], 0)
            with wave.open(str(Path(tmp) / 'microphone.wav')) as wav:
                self.assertEqual(wav.getnframes(), 1600)

    def test_native_cancelled_inference_is_reported_after_bounded_stop(self):
        from audio import AudioCapture
        from types import SimpleNamespace as NS
        import threading
        entered, release = threading.Event(), threading.Event()
        class Model:
            def transcribe(self, *args, **kwargs):
                entered.set()
                release.wait(3)
                return iter([NS(text='synthetic unfinished sentence')]), None
        class Capture(AudioCapture):
            def _capture(self, speaker, device):
                self._enqueue((speaker, np.zeros(16000), 1))
                self._stop.wait(3)
        backend = NS(default_microphone=lambda: NS(id='mic'), get_microphone=lambda **kw: NS(id=kw['id']))
        capture = Capture(source='microphone', backend=backend, model_factory=lambda *a, **kw: Model())
        with tempfile.TemporaryDirectory() as tmp:
            capture.set_recording_directory(Path(tmp).resolve())
            segments = []
            try:
                capture.start(segments.append)
                self.assertTrue(entered.wait(1))
                capture.stop(timeout=.01)
                self.assertEqual(capture.status()['state'], 'stopping')
            finally:
                release.set()
                capture.stop(timeout=2)
            self.assertFalse(segments)
            self.assertEqual(capture.status()['worker_dropped_chunks'], 1)
            self.assertIn('Some words may be missing', capture.status()['warning'])
