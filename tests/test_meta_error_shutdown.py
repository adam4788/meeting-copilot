"""Portable synthetic tests for Meta helper and fatal-error retirement."""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import audio_worker
import meta_asr


ROOT = Path(__file__).resolve().parents[1]


def _frame(kind, **fields):
    return (json.dumps({'type': kind, **fields}) + '\n').encode()


class _HelperPipe:
    def __init__(self, process):
        self.process = process
        self.initial_read = threading.Event()
        self.closed = False

    def __iter__(self):
        yield _frame('status', state='running', native_dropped_chunks=self.process.initial_drops)
        self.initial_read.set()
        if not self.process.terminating.wait(3):
            return
        if self.process.finish_delay is None:
            self.process.interrupted.wait(3)
            return
        if not self.process.interrupted.wait(self.process.finish_delay):
            yield _frame('status', state='stopped', native_dropped_chunks=7)
            self.process.exited.set()

    def close(self):
        self.closed = True


class _SyntheticHelper:
    """First terminate settles; a second terminate or kill loses the tail."""
    def __init__(self, finish_delay, initial_drops):
        self.finish_delay = finish_delay
        self.initial_drops = initial_drops
        self.terminating = threading.Event()
        self.interrupted = threading.Event()
        self.exited = threading.Event()
        self.stdout = _HelperPipe(self)
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts = []

    def poll(self):
        return 0 if self.exited.is_set() else None

    def terminate(self):
        self.terminate_calls += 1
        self.terminating.set()
        if self.terminate_calls > 1:
            self.interrupted.set()
            self.exited.set()

    def kill(self):
        self.kill_calls += 1
        self.interrupted.set()
        self.exited.set()

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired('synthetic-helper', timeout if timeout is not None else 0)
        return 0


class MetaErrorShutdownTests(unittest.TestCase):
    def run_synthetic(self, process, session, stop):
        events = []
        with patch.object(audio_worker.subprocess, 'Popen', return_value=process), \
             patch.object(audio_worker, 'emit', side_effect=events.append), \
             patch.object(meta_asr, 'run_session', side_effect=session):
            started = time.monotonic()
            try:
                audio_worker.run_capture_meta('microphone', 'ENDPOINTING', 'synthetic-key',
                                              'synthetic-model', helper=Path('synthetic'), stop=stop)
            finally:
                stop.set()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5)
        return events

    def test_stop_sends_one_termination_and_retains_final_delta(self):
        # The watcher must initiate stop while the helper is still settling;
        # retirement then races with it, as it does with the real Swift helper.
        process = _SyntheticHelper(finish_delay=.40, initial_drops=2)
        stop = threading.Event()
        stop.set()

        async def drain(queue, *_args):
            while await queue.get() is not None:
                pass

        events = self.run_synthetic(process, drain, stop)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)
        self.assertTrue(process.wait_timeouts)
        self.assertGreaterEqual(process.wait_timeouts[0], .6)
        stopped = [event for event in events if event.get('state') == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['native_dropped_chunks'], 9)
        self.assertFalse(stopped[0]['reader_incomplete'])
        self.assertTrue(process.stdout.closed)
        self.assertEqual(events[-1], stopped[0])

    def test_forced_kill_marks_tail_uncertain_even_when_pipe_reaches_eof(self):
        process = _SyntheticHelper(finish_delay=None, initial_drops=0)
        stop = threading.Event()

        async def early_session(_queue, *_args):
            while not process.stdout.initial_read.is_set():
                await asyncio.sleep(.001)

        events = self.run_synthetic(process, early_session, stop)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertTrue(process.stdout.closed, 'finished reader pipe must be closed even on forced kill')
        stopped = [event for event in events if event.get('state') == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['native_dropped_chunks'], 0)
        self.assertTrue(stopped[0]['reader_incomplete'])
        self.assertIn('tail accounting may be incomplete', stopped[0]['warning'].lower())
        self.assertEqual(events[-1], stopped[0])

    def test_zero_loss_native_error_still_follows_stop_summary(self):
        class Process:
            stdout = io.BytesIO(_frame('error', code='microphone_permission',
                                       message='synthetic private native detail'))
            def poll(self):
                return 0

        async def drain(queue, *_args):
            while await queue.get() is not None:
                pass

        process = Process()
        events = self.run_synthetic(process, drain, threading.Event())
        stopped = [event for event in events if event.get('state') == 'stopped']
        errors = [event for event in events if event.get('type') == 'error']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['native_dropped_chunks'], 0)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]['code'], 'microphone_permission')
        self.assertLess(events.index(stopped[0]), events.index(errors[0]))
        self.assertNotIn('synthetic private', json.dumps(events))
        self.assertTrue(process.stdout.closed)

    def test_worst_case_helper_reader_and_session_shutdown_is_bounded(self):
        # Exercise all timeout budgets together, not just each one separately.
        script = r'''
import asyncio, json, subprocess, threading, time
from pathlib import Path
from unittest.mock import patch
import audio_worker, meta_asr
class Pipe:
    closed = False
    def __iter__(self):
        yield (json.dumps({'type':'status', 'native_dropped_chunks':2}) + '\n').encode()
        threading.Event().wait(10)
    def close(self): self.closed = True
class Process:
    stdout = Pipe()
    terminations = 0
    kills = 0
    def poll(self): return None
    def terminate(self): self.terminations += 1
    def kill(self): self.kills += 1
    def wait(self, timeout=None):
        time.sleep(timeout)
        raise subprocess.TimeoutExpired('synthetic-helper', timeout)
process, events, cancelled = Process(), [], []
stop = threading.Event()
stop.set()
async def session(_queue, *_args):
    try: await asyncio.Event().wait()
    finally: cancelled.append(True)
started = time.monotonic()
with patch.object(audio_worker.subprocess, 'Popen', return_value=process), \
     patch.object(audio_worker, 'emit', side_effect=events.append), \
     patch.object(meta_asr, 'run_session', side_effect=session):
    audio_worker.run_capture_meta('microphone', 'ENDPOINTING', 'synthetic-key',
                                  'synthetic-model', helper=Path('synthetic'), stop=stop)
print(json.dumps({'events':events, 'cancelled':cancelled, 'closed':process.stdout.closed,
                  'terminations':process.terminations, 'kills':process.kills,
                  'elapsed':time.monotonic()-started}))
'''
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        try:
            result = subprocess.run([sys.executable, '-u', '-c', script], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=4)
        except subprocess.TimeoutExpired:
            self.fail('combined helper, reader and session retirement exceeded the stop bound')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertLess(payload['elapsed'], 2.5)
        self.assertEqual(payload['terminations'], 1)
        self.assertEqual(payload['kills'], 1)
        self.assertEqual(payload['cancelled'], [True])
        self.assertFalse(payload['closed'], 'do not close a pipe while its reader is blocked')
        stopped = [event for event in payload['events'] if event.get('state') == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['native_dropped_chunks'], 2)
        self.assertTrue(stopped[0]['reader_incomplete'])
        self.assertIn('tail accounting may be incomplete', stopped[0]['warning'].lower())
        self.assertIn('session drain was incomplete', stopped[0]['warning'].lower())
        self.assertEqual(payload['events'][-1], stopped[0])

    def test_native_error_wakes_and_cancels_hung_sessions_in_subprocess(self):
        # A subprocess makes a regression deterministic and bounded even if
        # run_capture_meta never returns. No real process, audio or provider.
        script = r'''
import asyncio, io, json, threading, time
from pathlib import Path
from unittest.mock import patch
import audio_worker, meta_asr
row = (json.dumps({'type':'error', 'code':'synthetic-private-code',
                   'message':'synthetic private message', 'native_dropped_chunks':6}) + '\n').encode()
class Process:
    stdout = io.BytesIO(row)
    def poll(self): return 0
process = Process()
events, cancelled = [], []
async def session(_queue, _emit, _key, _model, _mode, label):
    try:
        await asyncio.Event().wait()
    finally:
        cancelled.append(label)
started = time.monotonic()
with patch.object(audio_worker.subprocess, 'Popen', return_value=process), \
     patch.object(audio_worker, 'emit', side_effect=events.append), \
     patch.object(meta_asr, 'run_session', side_effect=session):
    audio_worker.run_capture_meta('both', 'ENDPOINTING', 'synthetic-key',
                                  'synthetic-model', helper=Path('synthetic'), stop=threading.Event())
print(json.dumps({'events':events, 'cancelled':cancelled,
                  'closed':process.stdout.closed, 'elapsed':time.monotonic()-started}))
'''
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        try:
            result = subprocess.run([sys.executable, '-u', '-c', script], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=4)
        except subprocess.TimeoutExpired:
            self.fail('fatal native error did not wake the hung Meta sessions')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertLess(payload['elapsed'], 2.5)
        self.assertCountEqual(payload['cancelled'], ['you', 'remote'])
        self.assertTrue(payload['closed'])
        events = payload['events']
        stopped = [event for event in events if event.get('state') == 'stopped']
        errors = [event for event in events if event.get('type') == 'error']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['native_dropped_chunks'], 6)
        self.assertIn('session drain was incomplete', stopped[0]['warning'].lower())
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]['code'], 'capture')
        self.assertLess(events.index(stopped[0]), events.index(errors[0]))
        self.assertEqual(events[-1], errors[0])
        self.assertNotIn('synthetic-private-code', result.stdout)
        self.assertNotIn('synthetic private message', result.stdout)
        self.assertNotIn('synthetic-key', result.stdout)


if __name__ == '__main__':
    unittest.main()
