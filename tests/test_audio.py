"""Unit tests: no hardware, network, or recording without explicit invocation."""
import unittest
import importlib
import os
import sys


class AudioTests(unittest.TestCase):
    def test_devices_reports_ids_and_defaults_without_recording(self):
        from types import SimpleNamespace as NS
        from audio import AudioCapture
        mic = NS(id='mic', name='Mic')
        out = NS(id='out', name='Output')
        backend = NS(all_microphones=lambda: [mic], all_speakers=lambda: [out],
                     default_microphone=lambda: mic, default_speaker=lambda: out)
        result = AudioCapture(backend=backend).devices()
        self.assertEqual(result['inputs'], [{'id': 'mic', 'name': 'Mic'}])
        self.assertEqual(result['outputs'], [{'id': 'out', 'name': 'Output'}])
        self.assertEqual(result['default_input'], 'mic')
        self.assertEqual(result['default_output'], 'out')

    def test_warmup_loads_cpu_int8_english_model_once(self):
        from audio import AudioCapture
        calls = []
        model = object()
        def factory(*args, **kwargs):
            calls.append((args, kwargs))
            return model
        capture = AudioCapture(model_size='base.en', model_factory=factory)
        self.assertIs(capture.warmup(), model)
        self.assertIs(capture.warmup(), model)
        self.assertEqual(calls, [(('base.en',), {'device': 'cpu', 'compute_type': 'int8', 'cpu_threads': 4})])

    def test_start_captures_both_sources_and_stop_releases_recorders(self):
        import threading
        import time
        import numpy as np
        from types import SimpleNamespace as NS
        from audio import AudioCapture
        opened, closed, selections = [], [], []
        class Recorder:
            def __init__(self, name): self.name = name
            def __enter__(self): opened.append(self.name); return self
            def __exit__(self, *args): closed.append(self.name)
            def record(self, numframes):
                time.sleep(.005)
                return np.full((numframes, 2), .1, dtype=np.float32)
        class Device:
            def __init__(self, name): self.id = name
            def recorder(self, **kw):
                self.kw = kw
                return Recorder(self.id)
        mic, out = Device('mic-id'), Device('out-id')
        def get_microphone(id, include_loopback=False):
            selections.append((id, include_loopback))
            return out if include_loopback else mic
        backend = NS(get_microphone=get_microphone,
                     default_microphone=lambda: mic, default_speaker=lambda: out)
        class Model:
            def transcribe(self, samples, **kwargs):
                self.samples = samples
                self.kwargs = kwargs
                return iter([NS(text=' Test speech. ')]), None
        model = Model()
        capture = AudioCapture(backend=backend, model_factory=lambda *a, **kw: model,
                               input_id='mic-id', output_id='out-id', chunk_seconds=.2)
        segments, statuses = [], []
        done = threading.Event()
        def on_segment(segment):
            segments.append(segment)
            if {s['speaker'] for s in segments} == {'remote', 'you'}: done.set()
        capture.start(on_segment, statuses.append)
        self.assertTrue(done.wait(3), capture.status())
        capture.stop()
        self.assertCountEqual(opened, ['mic-id', 'out-id'])
        self.assertCountEqual(closed, opened)
        self.assertCountEqual(selections, [('mic-id', False), ('out-id', True)])
        self.assertFalse(capture.status()['running'])
        self.assertEqual(capture.status()['state'], 'stopped')
        self.assertEqual(segments[0]['text'], 'Test speech.')
        self.assertIsInstance(segments[0]['timestamp'], float)
        self.assertEqual(model.samples.ndim, 1)
        self.assertEqual(model.kwargs['language'], 'en')
        self.assertTrue(statuses)

    def test_stop_during_loading_does_not_open_devices_or_allow_restart(self):
        import threading
        from audio import AudioCapture
        loading, release = threading.Event(), threading.Event()
        def factory(*args, **kwargs):
            loading.set()
            release.wait(3)
            return object()
        capture = AudioCapture(model_factory=factory, backend=object())
        capture.start(lambda segment: self.fail('late callback'))
        self.assertTrue(loading.wait(1))
        with self.assertRaises(RuntimeError): capture.start(lambda segment: None)
        capture.stop(timeout=.01)
        self.assertEqual(capture.status()['state'], 'stopping')
        with self.assertRaises(RuntimeError): capture.start(lambda segment: None)
        release.set()
        capture.stop(timeout=2)
        self.assertEqual(capture.status()['state'], 'stopped')
        self.assertFalse(capture.status()['running'])
        self.assertIsNone(capture.status()['error'])

    def test_queue_is_bounded_drops_oldest_and_discards_after_stop(self):
        import numpy as np
        from audio import AudioCapture
        capture = AudioCapture(queue_size=2)
        for i in range(5): capture._enqueue(('remote', np.zeros(4), float(i)))
        self.assertEqual(capture.status()['queued'], 2)
        self.assertEqual(capture.status()['dropped'], 3)
        self.assertEqual(capture._queue.get_nowait()[2], 3.0)
        capture.stop()
        capture._enqueue(('remote', np.zeros(4), 7.0))
        self.assertEqual(capture.status()['queued'], 0)

    def test_model_failure_is_explicit_and_status_callback_failure_is_contained(self):
        import threading
        from audio import AudioCapture
        observed = threading.Event()
        def broken(*args, **kwargs): raise OSError('model unavailable')
        def status_callback(status):
            if status['state'] == 'error':
                observed.set()
                raise ValueError('UI closed')
        capture = AudioCapture(model_factory=broken)
        capture.start(lambda segment: None, status_callback)
        self.assertTrue(observed.wait(1))
        capture.stop()
        self.assertEqual(capture.status()['state'], 'error')
        self.assertIn('model unavailable', capture.status()['error'])
        self.assertIn('UI closed', capture.status()['callback_error'])
        self.assertFalse(capture.status()['running'])

    def test_invalid_limits_and_callbacks_fail_before_recording(self):
        from audio import AudioCapture
        for kwargs in ({'queue_size': 0}, {'queue_size': -1}, {'queue_size': 100},
                       {'queue_size': 1.5}, {'chunk_seconds': 0}, {'chunk_seconds': float('nan')},
                       {'chunk_seconds': 1000}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): AudioCapture(**kwargs)
        with self.assertRaises(TypeError): AudioCapture().start(None)
        with self.assertRaises(TypeError): AudioCapture().start(print, on_status=7)

    def test_stopped_capture_does_not_open_a_device_or_emit_late_text(self):
        from audio import AudioCapture
        capture = AudioCapture()
        capture.stop()
        # A cancelled startup must not open a recorder, even before its first read.
        capture._capture('remote', object())
        self.assertEqual(capture.status()['state'], 'stopped')
        self.assertIsNone(capture.status()['error'])
        capture._notify('running')
        self.assertEqual(capture.status()['state'], 'stopped')

    def test_stop_discards_inflight_transcript_and_returns_with_bounded_wait(self):
        import threading
        import time
        import numpy as np
        from types import SimpleNamespace as NS
        from audio import AudioCapture
        entered, release = threading.Event(), threading.Event()
        class Model:
            def transcribe(self, *args, **kwargs):
                entered.set()
                release.wait(3)
                return iter([NS(text='must not escape stop')]), None
        class Capture(AudioCapture):
            def _capture(self, speaker, device):
                self._enqueue((speaker, np.zeros(16000, dtype=np.float32), time.time()))
                self._stop.wait(3)
        backend = NS(default_microphone=lambda: NS(id='mic'), default_speaker=lambda: NS(id='out'),
                     get_microphone=lambda **kw: NS(id=kw['id']))
        capture = Capture(backend=backend, model_factory=lambda *a, **kw: Model())
        segments = []
        capture.start(segments.append)
        self.assertTrue(entered.wait(1))
        start = time.monotonic()
        capture.stop(timeout=.01)
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(capture.status()['state'], 'stopping')
        release.set()
        capture.stop(timeout=2)
        self.assertEqual(segments, [])
        self.assertEqual(capture.status()['queued'], 0)
        self.assertFalse(capture.status()['running'])

    def test_chosen_device_ids_never_fall_back_to_a_fuzzy_match(self):
        import threading
        from types import SimpleNamespace as NS
        from audio import AudioCapture
        failed = threading.Event()
        backend = NS(get_microphone=lambda **kw: NS(id='other-device'),
                     default_speaker=lambda: NS(id='out'))
        capture = AudioCapture(input_id='exact-mic', backend=backend,
                               model_factory=lambda *a, **kw: object())
        capture.start(lambda segment: None,
                      lambda status: failed.set() if status['state'] == 'error' else None)
        self.assertTrue(failed.wait(1))
        capture.stop()
        self.assertIn('exact-mic', capture.status()['error'])
        self.assertFalse(capture.status()['running'])

    def test_construct_is_idle_and_does_not_load_model(self):
        audio = importlib.import_module('audio')
        calls = []
        capture = audio.AudioCapture(model_factory=lambda *a, **kw: calls.append(kw))
        self.assertEqual(capture.status()['state'], 'idle')
        self.assertEqual(calls, [])
        self.assertFalse(capture.status()['running'])


@unittest.skipUnless(sys.platform == 'win32' and os.environ.get('MEETING_COPILOT_AUDIO_TEST') == '1',
                     'Windows hardware capture is opt-in: MEETING_COPILOT_AUDIO_TEST=1')
class WindowsAudioIntegrationTests(unittest.TestCase):
    def test_real_device_enumeration_and_short_capture(self):
        import numpy as np
        import soundcard as sc
        from audio import AudioCapture
        devices = AudioCapture().devices()
        self.assertTrue(devices['inputs'])
        self.assertTrue(devices['outputs'])
        mic = sc.default_microphone()
        loopback = sc.get_microphone(id=sc.default_speaker().id, include_loopback=True)
        for source in (mic, loopback):
            samples = source.record(samplerate=16000, numframes=16000)
            self.assertEqual(samples.shape[0], 16000)
            self.assertTrue(np.isfinite(samples).all())
        # Deliberately no assertion that ambient microphone noise is speech.

    def test_windows_sapi_loopback_and_local_transcription(self):
        import pathlib
        import subprocess
        import tempfile
        import threading
        import time
        import wave
        import numpy as np
        import soundcard as sc
        from audio import AudioCapture
        scratch = pathlib.Path(os.environ.get('MEETING_COPILOT_TEST_SCRATCH',
                                             str(pathlib.Path.home() / '.cache' / 'meeting-copilot-tests')))
        # Synthetic speech only; no captured audio is saved. Keep artifacts outside the repo.
        scratch.mkdir(parents=True, exist_ok=True)
        phrase = 'The project review is on Friday. Please explain the deployment plan and the next action items.'
        with tempfile.TemporaryDirectory(prefix='synthetic-', dir=scratch) as temp:
            path = pathlib.Path(temp) / 'speech.wav'
            script = ("Add-Type -AssemblyName System.Speech; "
                      "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Rate = -1; "
                      "$s.SetOutputToWaveFile('" + str(path).replace("'", "''") + "'); "
                      "$s.Speak('" + phrase + "'); $s.Dispose();")
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                           check=True, capture_output=True, timeout=30)
            capture = AudioCapture()
            model = capture.warmup()  # This opt-in test may download base.en.
            direct, _ = model.transcribe(str(path), language='en', beam_size=1, vad_filter=True)
            direct_text = ' '.join(s.text for s in direct).lower()
            self.assertIn('project review', direct_text)
            with wave.open(str(path)) as wav:
                self.assertEqual(wav.getsampwidth(), 2)
                rate = wav.getframerate()
                playback = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
                playback = playback.astype(np.float32).reshape(-1, wav.getnchannels()) / 32768
            heard, segments = threading.Event(), []
            def on_segment(segment):
                segments.append(segment)
                if segment['speaker'] == 'remote' and 'project' in segment['text'].lower():
                    heard.set()
            try:
                capture.start(on_segment)
                time.sleep(1)
                sc.default_speaker().play(playback, samplerate=rate)
                self.assertTrue(heard.wait(18), capture.status())
            finally:
                capture.stop(timeout=5)
            self.assertIsNone(capture.status()['error'])
            self.assertFalse(capture.status()['running'])
            print('Synthetic direct transcription:', direct_text)
            print('Synthetic loopback transcription:', [s['text'] for s in segments if s['speaker'] == 'remote'])


if __name__ == '__main__':
    unittest.main()
