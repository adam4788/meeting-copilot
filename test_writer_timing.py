"""Timing-only native diagnostics through the real worker; synthetic helper only."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import audio_worker
from macos_audio import clean_writer_timing


class WriterTimingTests(unittest.TestCase):
    def test_only_finite_allowlisted_milliseconds_survive(self):
        self.assertEqual(clean_writer_timing({'max_queue_ms': 12, 'max_encode_ms': .2,
                                             'max_write_ms': float('nan'), 'private': 'sentinel'}),
                         {'max_queue_ms': 12., 'max_encode_ms': .2})
        for invalid in (None, 'sentinel', {'max_queue_ms': True}, {'max_queue_ms': -1},
                        {'max_write_ms': float('inf')}, {'max_write_ms': 60001},
                        {'max_queue_ms': 10**400}):
            self.assertEqual(clean_writer_timing(invalid), {})

    def test_terminal_timing_survives_without_inventing_loss(self):
        self.run_worker(0)

    def test_terminal_timing_does_not_erase_reported_loss(self):
        self.run_worker(3)

    def test_pause_preserves_timing_and_loss_without_starting_devices(self):
        from server import MeetingState
        state=MeetingState()
        state.capture_info={'native_writer_timing_ms':{'max_write_ms':150.},
                            'native_dropped_chunks':3,'warning':'synthetic loss'}
        self.assertTrue(state.pause()['stop_confirmed'])
        self.assertEqual(state.capture_info['native_writer_timing_ms'],{'max_write_ms':150.})
        self.assertEqual(state.capture_info['native_dropped_chunks'],3)

    def run_worker(self, losses):
        header = {'type': 'status', 'state': 'stopped', 'native_dropped_chunks': losses,
                  'native_writer_timing_ms': {'max_queue_ms': 140, 'max_write_ms': .5,
                                            'untrusted': 'synthetic-sensitive'}}
        outputs=[]
        real_popen=subprocess.Popen
        with tempfile.TemporaryDirectory() as tmp:
            script=Path(tmp)/'helper.py'
            script.write_text('print('+repr(json.dumps(header))+',flush=True)')
            with patch('audio_worker.emit',outputs.append), \
                    patch('audio_worker.subprocess.Popen',lambda cmd,**kw:real_popen([sys.executable,str(script)],**kw)):
                audio_worker.run_capture(object(),'microphone','en',helper=Path('/synthetic'))
        summary=next(e for e in outputs if e.get('state')=='stopped')
        self.assertEqual(summary['native_dropped_chunks'],losses)
        self.assertEqual(summary['native_writer_timing_ms'],{'max_queue_ms':140.,'max_write_ms':.5})
        self.assertEqual('warning' in summary,bool(losses))
        self.assertNotIn('synthetic-sensitive',json.dumps(outputs))
