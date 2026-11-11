"""Content-free diagnostics exercised only with synthetic samples and fake models."""
import base64
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch
import numpy as np
from audio_worker import AudioDiagnostics, run_capture
from macos_audio import clean_audio_diagnostics


class AudioDiagnosticTests(unittest.TestCase):
    def test_source_levels_are_cumulative_and_content_free(self):
        d = AudioDiagnostics('both')
        d.receive('you', np.array([0, .5, -.5, 0]))
        d.receive('remote', np.zeros(4))
        d.inference('you')
        d.inference('you', {'text': 'synthetic words never retained'})
        d.inference('remote')
        d.inference('remote', {'text': ''})
        out = d.snapshot()
        self.assertEqual(out['microphone']['received_samples'], 4)
        self.assertAlmostEqual(out['microphone']['rms'], .35355339059)
        self.assertEqual(out['microphone']['peak'], .5)
        self.assertEqual(out['system']['rms'], 0)
        self.assertEqual(out['system']['empty'], 1)
        self.assertNotIn('synthetic', json.dumps(out))
        self.assertEqual(set(out['microphone']), {'received_samples', 'peak', 'rms', 'submitted', 'completed', 'empty'})

    def test_missing_samples_are_explicit_zero_count_not_inferred_silence(self):
        out = AudioDiagnostics('system').snapshot()
        self.assertEqual(set(out), {'system'})
        self.assertEqual(out['system']['received_samples'], 0)

    def test_reporting_rate_limit(self):
        with patch('audio_worker.time.monotonic', return_value=0):
            d = AudioDiagnostics('microphone')
        with patch('audio_worker.emit') as emit:
            for now in (0, 1, 2, 2.1, 3, 4):
                with patch('audio_worker.time.monotonic', return_value=now):
                    d.report()
            self.assertEqual(emit.call_count, 2)

    def test_reports_do_not_assert_native_running(self):
        from macos_audio import MacOSAudioCapture
        from server import MeetingState
        for lifecycle in ('starting', 'loading', 'stopping', 'stopped', 'error'):
            state = MeetingState()
            state.capture_info = {'state': lifecycle, 'error': 'retained'}
            state.capture_error = 'retained'
            state.listening = False
            capture = MacOSAudioCapture(source='microphone')
            capture._state = lifecycle
            delivered = []
            capture._on_status = lambda event: (delivered.append(event), state.audio_status(event))
            with patch.object(capture, '_notify') as notify:
                capture._diagnostics_update({'type': 'diagnostics', 'state': 'running',
                                             'audio_diagnostics': {'microphone': {'received_samples': 0}}})
                notify.assert_not_called()
            self.assertNotIn('state', delivered[0])
            self.assertEqual(capture._state, lifecycle)
            self.assertEqual(state.capture_info['state'], lifecycle)
            self.assertEqual(state.capture_error, 'retained')
            self.assertFalse(state.listening)

    def test_racing_report_emissions_cannot_reorder_snapshots(self):
        with patch('audio_worker.time.monotonic', return_value=0):
            d = AudioDiagnostics('microphone')
        d.receive('you', np.ones(1))
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        events = []
        def emit(event):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(2))
            events.append(event)
        def later_report():
            second_started.set()
            d.receive('you', np.ones(1))
            d.report()
        with patch('audio_worker.emit', emit), patch('audio_worker.time.monotonic', side_effect=[2, 4]):
            first = threading.Thread(target=d.report)
            first.start()
            self.assertTrue(entered.wait(2))
            second = threading.Thread(target=later_report)
            second.start()
            self.assertTrue(second_started.wait(2))
            self.assertEqual(events, [])
            release.set()
            first.join(2)
            second.join(2)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
        self.assertEqual([e['audio_diagnostics']['microphone']['received_samples'] for e in events], [1, 2])
        self.assertTrue(all(e['type'] == 'diagnostics' and 'state' not in e for e in events))

    def test_sanitizer_rejects_nonfinite_unknown_and_unbounded_values(self):
        raw = {'microphone': {'received_samples': True, 'peak': float('nan'), 'rms': float('inf'),
                              'submitted': -1, 'completed': 10**13, 'empty': 2, 'text': 'private'},
               'system': {'received_samples': 4, 'peak': .5}, 'secret': 'private'}
        self.assertEqual(clean_audio_diagnostics(raw, 'microphone'), {'microphone': {'empty': 2}})
        json.dumps(clean_audio_diagnostics(raw), allow_nan=False)

    def test_fake_capture_preserves_final_empty_transcription_diagnostics(self):
        pcm = np.full(16000, .25, dtype='<f4')
        frame = {'type': 'pcm', 'source': 'microphone', 'rate': 16000, 'samples': len(pcm),
                 'timestamp': 1, 'pcm': base64.b64encode(pcm.tobytes()).decode()}
        proc = Mock(stdout=io.BytesIO(json.dumps(frame).encode() + b'\n'))
        proc.poll.return_value = 0
        model = Mock()
        model.transcribe.return_value = ([], None)
        events = []
        with patch('audio_worker.subprocess.Popen', return_value=proc), patch('audio_worker.emit', events.append), patch('audio_worker.signal.signal'):
            run_capture(model, 'microphone', 'en', helper='/synthetic/helper', stop=threading.Event())
        final = next(e['audio_diagnostics'] for e in events if e.get('state') == 'stopped')
        self.assertEqual(final['microphone']['received_samples'], 16000)
        self.assertEqual(final['microphone']['completed'], 1)
        self.assertEqual(final['microphone']['empty'], 1)
        self.assertEqual(final['microphone']['peak'], .25)

    def test_server_latches_only_sanitized_diagnostics(self):
        from server import MeetingState
        state = MeetingState()
        state.audio_status({'state': 'running', 'audio_diagnostics': {'microphone': {'received_samples': 10, 'text': 'private'}}})
        state.audio_status({'state': 'stopped'})
        self.assertEqual(state.capture_info['audio_diagnostics'], {'microphone': {'received_samples': 10}})
        state.pause()
        self.assertEqual(state.capture_info['audio_diagnostics'], {'microphone': {'received_samples': 10}})
