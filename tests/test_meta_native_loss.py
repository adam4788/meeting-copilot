"""Synthetic Meta-worker loss-accounting regressions; no cloud or devices."""
import base64
import io
import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import audio_worker
import meta_asr


def frame(kind, drops=None, **fields):
    payload = {'type': kind, **fields}
    if drops is not None:
        payload['native_dropped_chunks'] = drops
    return (json.dumps(payload) + '\n').encode()


class MetaNativeLossTests(unittest.TestCase):
    def run_worker(self, rows):
        class FakeProcess:
            stdout = io.BytesIO(b''.join(rows))
            def poll(self): return 0
        received, events = [], []
        stop = threading.Event()
        async def fake_session(queue, _emit, _key, _model, _mode, label):
            while True:
                item = await queue.get()
                if item is None:
                    return
                received.append((label, item))
        with patch.object(audio_worker.subprocess, 'Popen', return_value=FakeProcess()), \
             patch.object(audio_worker, 'emit', side_effect=events.append), \
             patch.object(meta_asr, 'run_session', side_effect=fake_session):
            try:
                audio_worker.run_capture_meta('microphone', 'ENDPOINTING', 'synthetic-key',
                                              'synthetic-model', helper=Path('/synthetic'), stop=stop)
            finally:
                stop.set()
        return events, received

    def test_control_and_pcm_losses_are_reported_once_during_and_after_capture(self):
        pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
        events, received = self.run_worker([
            frame('status', 3, state='running', warning='untrusted synthetic text'),
            frame('pcm', 2, source='microphone', rate=16000, samples=171, timestamp=1.0, pcm=pcm),
            frame('status', 4, state='stopped'),
        ])
        updates = [e for e in events if e.get('state') == 'running' and e.get('native_dropped_chunks')]
        self.assertEqual([e['native_dropped_chunks'] for e in updates], [3, 5, 9])
        self.assertTrue(all('some' in e['warning'].lower() for e in updates))
        terminal = [e for e in events if e.get('state') == 'stopped']
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]['native_dropped_chunks'], 9)
        self.assertEqual(terminal[0]['dropped_chunks'], 9)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], 'you')
        self.assertNotIn('untrusted synthetic text', json.dumps(events))
        self.assertNotIn('synthetic-key', json.dumps(events))

    def test_error_frame_loss_is_preserved_without_copying_untrusted_text(self):
        events, received = self.run_worker([
            frame('error', 6, code='capture', message='SYNTHETIC SECRET MESSAGE'),
        ])
        running = [e for e in events if e.get('state') == 'running' and e.get('native_dropped_chunks')]
        self.assertEqual([e['native_dropped_chunks'] for e in running], [6])
        self.assertEqual(next(e for e in events if e.get('state') == 'stopped')['native_dropped_chunks'], 6)
        self.assertEqual([e.get('code') for e in events if e.get('type') == 'error'], ['capture'])
        self.assertLess(next(i for i, e in enumerate(events) if e.get('state') == 'stopped'),
                        next(i for i, e in enumerate(events) if e.get('type') == 'error'),
                        'the adapter stops reading after an error, so publish loss first')
        self.assertNotIn('SYNTHETIC SECRET MESSAGE', json.dumps(events))
        self.assertEqual(received, [])

    def test_native_permission_error_is_visible_and_unknown_codes_are_sanitized(self):
        for native_code, expected in [('microphone_permission', 'microphone_permission'),
                                      ('synthetic-private-code', 'capture')]:
            with self.subTest(native_code=native_code):
                events, _ = self.run_worker([frame('error', code=native_code,
                                                   message='synthetic-private-message')])
                errors = [e for e in events if e.get('type') == 'error']
                self.assertEqual([e.get('code') for e in errors], [expected])
                self.assertNotIn('synthetic-private', json.dumps(events))

    def test_invalid_status_counters_do_not_create_fake_loss(self):
        pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
        events, _ = self.run_worker([
            frame('status', True, state='running'), frame('status', -1, state='stopped'),
            frame('status', '8', state='running'), frame('status', 0, state='running'),
            frame('pcm', True, source='microphone', rate=16000, samples=171, timestamp=1.0, pcm=pcm),
        ])
        self.assertFalse(any(e.get('native_dropped_chunks') for e in events))

    def test_early_session_end_drains_reader_before_the_final_summary(self):
        pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
        gate, events = threading.Event(), []
        class DelayedPipe:
            def __iter__(self):
                yield frame('pcm', 2, source='microphone', rate=16000, samples=171, timestamp=1.0, pcm=pcm)
                gate.wait(2)
                time.sleep(.15)
                yield frame('status', 3, state='stopped')
        class FakeProcess:
            stdout = DelayedPipe()
            def poll(self): return 0 if gate.is_set() else None
            def terminate(self): gate.set()
            def wait(self, timeout=None): return 0
        async def early_session(queue, *_args):
            await queue.get()
        stop = threading.Event()
        with patch.object(audio_worker.subprocess, 'Popen', return_value=FakeProcess()), \
             patch.object(audio_worker, 'emit', side_effect=events.append), \
             patch.object(meta_asr, 'run_session', side_effect=early_session):
            try:
                audio_worker.run_capture_meta('microphone', 'ENDPOINTING', 'synthetic-key',
                                              'synthetic-model', helper=Path('/synthetic'), stop=stop)
                time.sleep(.3)
            finally:
                stop.set()
        statuses = [e for e in events if e.get('type') == 'status' and e.get('native_dropped_chunks')]
        self.assertEqual(statuses[-1]['state'], 'stopped')
        self.assertEqual(statuses[-1]['native_dropped_chunks'], 5)

    def test_asr_error_preserves_drained_loss_summary(self):
        pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
        terminated, events = threading.Event(), []
        class DelayedPipe:
            def __iter__(self):
                yield frame('pcm', 2, source='microphone', rate=16000, samples=171, timestamp=1.0, pcm=pcm)
                terminated.wait(2)
                yield frame('status', 3, warning='synthetic private warning')
        class FakeProcess:
            stdout = DelayedPipe()
            def poll(self): return 0 if terminated.is_set() else None
            def terminate(self): terminated.set()
            def wait(self, timeout=None): return 0
        async def failing_session(queue, *_args):
            await queue.get()
            raise meta_asr.MetaAsrError('synthetic private provider message')
        stop = threading.Event()
        with patch.object(audio_worker.subprocess, 'Popen', return_value=FakeProcess()), \
             patch.object(audio_worker, 'emit', side_effect=events.append), \
             patch.object(meta_asr, 'run_session', side_effect=failing_session):
            try:
                with self.assertRaises(meta_asr.MetaAsrError):
                    audio_worker.run_capture_meta('microphone', 'ENDPOINTING', 'synthetic-key',
                                                  'synthetic-model', helper=Path('/synthetic'), stop=stop)
            finally:
                stop.set()
        stopped = [e for e in events if e.get('state') == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['native_dropped_chunks'], 5)
        self.assertEqual(events[-1], stopped[0])
        self.assertNotIn('synthetic private', json.dumps(events))
        self.assertNotIn('synthetic-key', json.dumps(events))

    def test_unexpected_session_failure_is_not_silenced_and_keeps_summary(self):
        pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
        events = []
        class FakeProcess:
            stdout = io.BytesIO(frame('pcm', 2, source='microphone', rate=16000,
                                      samples=171, timestamp=1.0, pcm=pcm))
            def poll(self): return 0
        async def failing_session(queue, *_args):
            await queue.get()
            raise RuntimeError('synthetic private provider message')
        stop = threading.Event()
        with patch.object(audio_worker.subprocess, 'Popen', return_value=FakeProcess()), \
             patch.object(audio_worker, 'emit', side_effect=events.append), \
             patch.object(meta_asr, 'run_session', side_effect=failing_session):
            try:
                with self.assertRaises(RuntimeError):
                    audio_worker.run_capture_meta('microphone', 'ENDPOINTING', 'synthetic-key',
                                                  'synthetic-model', helper=Path('/synthetic'), stop=stop)
            finally:
                stop.set()
        stopped = [e for e in events if e.get('state') == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['native_dropped_chunks'], 2)
        self.assertEqual(events[-1], stopped[0])
        self.assertNotIn('synthetic private', json.dumps(events))
        self.assertNotIn('synthetic-key', json.dumps(events))

    def test_unfinished_reader_is_bounded_and_cannot_publish_after_summary(self):
        pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
        blocked, release, threads = threading.Event(), threading.Event(), []
        events, thread_errors = [], []
        class BlockedPipe:
            def __iter__(self):
                threads.append(threading.current_thread())
                yield frame('pcm', 2, source='microphone', rate=16000, samples=171, timestamp=1.0, pcm=pcm)
                blocked.set()
                release.wait(5)
                yield frame('status', 31, warning='synthetic private warning')
        class FakeProcess:
            stdout = BlockedPipe()
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): return 0
        async def early_session(queue, *_args):
            await queue.get()
        stop = threading.Event()
        with patch.object(audio_worker.subprocess, 'Popen', return_value=FakeProcess()), \
             patch.object(audio_worker, 'emit', side_effect=events.append), \
             patch.object(meta_asr, 'run_session', side_effect=early_session), \
             patch.object(threading, 'excepthook', side_effect=thread_errors.append):
            try:
                started = time.monotonic()
                audio_worker.run_capture_meta('microphone', 'ENDPOINTING', 'synthetic-key',
                                              'synthetic-model', helper=Path('/synthetic'), stop=stop)
                self.assertLess(time.monotonic() - started, 2.5)
                self.assertTrue(blocked.is_set())
                final_events = list(events)
                stopped = [e for e in events if e.get('state') == 'stopped']
                self.assertEqual(len(stopped), 1)
                self.assertTrue(stopped[0]['reader_incomplete'])
                self.assertEqual(stopped[0]['native_dropped_chunks'], 2)
                self.assertIn('incomplete', stopped[0]['warning'].lower())
            finally:
                release.set()
                stop.set()
                for thread in threads:
                    thread.join(timeout=2)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(events, final_events)
            self.assertEqual(thread_errors, [])
            self.assertNotIn('synthetic private warning', json.dumps(events))
            self.assertNotIn('synthetic-key', json.dumps(events))

    def test_early_session_end_reports_queued_audio_it_cannot_send(self):
        pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
        terminated, events, received = threading.Event(), [], []
        class DelayedPipe:
            def __iter__(self):
                yield frame('pcm', 2, source='microphone', rate=16000, samples=171, timestamp=1.0, pcm=pcm)
                terminated.wait(2)
                for _ in range(3):
                    yield frame('pcm', source='microphone', rate=16000, samples=171, timestamp=1.0, pcm=pcm)
                yield frame('status', 4, warning='synthetic private warning')
        class FakeProcess:
            stdout = DelayedPipe()
            def poll(self): return 0 if terminated.is_set() else None
            def terminate(self): terminated.set()
            def wait(self, timeout=None): return 0
        async def early_session(queue, *_args):
            received.append(await queue.get())
        stop = threading.Event()
        with patch.object(audio_worker.subprocess, 'Popen', return_value=FakeProcess()), \
             patch.object(audio_worker, 'emit', side_effect=events.append), \
             patch.object(meta_asr, 'run_session', side_effect=early_session):
            try:
                audio_worker.run_capture_meta('microphone', 'ENDPOINTING', 'synthetic-key',
                                              'synthetic-model', helper=Path('/synthetic'), stop=stop)
            finally:
                stop.set()
        self.assertEqual(len(received), 1)
        stopped = [e for e in events if e.get('state') == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['native_dropped_chunks'], 6)
        self.assertEqual(stopped[0]['worker_dropped_chunks'], 3)
        self.assertEqual(stopped[0]['dropped_chunks'], 9)
        self.assertIn('worker', stopped[0]['warning'].lower())
        self.assertNotIn('synthetic private', json.dumps(events))
        self.assertNotIn('synthetic-key', json.dumps(events))

    def test_early_session_exit_warns_even_without_known_dropped_frames(self):
        pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
        terminated, events = threading.Event(), []
        class DelayedPipe:
            def __iter__(self):
                yield frame('pcm', source='microphone', rate=16000, samples=171, timestamp=1.0, pcm=pcm)
                terminated.wait(2)
        class FakeProcess:
            stdout = DelayedPipe()
            def poll(self): return 0 if terminated.is_set() else None
            def terminate(self): terminated.set()
            def wait(self, timeout=None): return 0
        async def early_session(queue, *_args):
            await queue.get()
        stop = threading.Event()
        with patch.object(audio_worker.subprocess, 'Popen', return_value=FakeProcess()), \
             patch.object(audio_worker, 'emit', side_effect=events.append), \
             patch.object(meta_asr, 'run_session', side_effect=early_session):
            try:
                audio_worker.run_capture_meta('microphone', 'ENDPOINTING', 'synthetic-key',
                                              'synthetic-model', helper=Path('/synthetic'), stop=stop)
            finally:
                stop.set()
        stopped = [e for e in events if e.get('state') == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['dropped_chunks'], 0)
        self.assertIn('session ended before capture', stopped[0]['warning'].lower())
        self.assertIn('words may be missing', stopped[0]['warning'].lower())
        self.assertEqual(events[-1], stopped[0])

    def test_stop_unblocks_session_when_reader_never_reaches_eof(self):
        script = r'''
import base64, json, threading
from pathlib import Path
from unittest.mock import patch
import audio_worker, meta_asr
pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
stop=threading.Event()
class Pipe:
    def __iter__(self):
        yield (json.dumps({'type':'pcm','source':'microphone','rate':16000,'samples':171,
                           'timestamp':1.0,'pcm':pcm,'native_dropped_chunks':2})+'\n').encode()
        threading.Event().wait(10)
class Process:
    stdout=Pipe()
    def poll(self): return None
    def terminate(self): pass
    def wait(self, timeout=None): return 0
events=[]
async def session(q, *_args):
    await q.get()
    stop.set()
    assert await q.get() is None
with patch.object(audio_worker.subprocess,'Popen',return_value=Process()), \
     patch.object(audio_worker,'emit',side_effect=events.append), \
     patch.object(meta_asr,'run_session',side_effect=session):
    audio_worker.run_capture_meta('microphone','ENDPOINTING','synthetic-key','fake',helper=Path('/synthetic'),stop=stop)
print(json.dumps(events))
'''
        try:
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=4)
        except subprocess.TimeoutExpired:
            self.fail('stop stranded a Meta session behind a blocked reader')
        self.assertEqual(result.returncode, 0, result.stderr)
        events = json.loads(result.stdout)
        stopped = [e for e in events if e.get('state') == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertTrue(stopped[0]['reader_incomplete'])
        self.assertEqual(stopped[0]['native_dropped_chunks'], 2)
        self.assertEqual(events[-1], stopped[0])

    def test_stop_summary_arrives_within_default_macos_grace_window(self):
        # MacOSAudioCapture.stop defaults to a 2.5s graceful worker window.
        script = r'''
import asyncio, base64, io, json, os, signal, sys, threading
from pathlib import Path
from unittest.mock import patch
import audio_worker, meta_asr
pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
line = (json.dumps({'type':'pcm','source':'microphone','rate':16000,'samples':171,
                    'timestamp':1.0,'pcm':pcm,'native_dropped_chunks':2})+'\n').encode()
class Process:
    stdout=io.BytesIO(line)
    def poll(self): return 0
stop=threading.Event()
if os.name == 'nt' or os.environ.get('MC_TEST_STOP_VIA_STDIN') == '1':
    # Windows terminate() is an unconditional TerminateProcess, not SIGTERM.
    # Exercise the same graceful stop event via a private stdin control byte.
    threading.Thread(target=lambda: (sys.stdin.buffer.read(1), stop.set()), daemon=True).start()
else:
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
async def session(q, *_args):
    await q.get()
    print('SYNTHETIC_READY', flush=True)
    await asyncio.Event().wait()
with patch.object(audio_worker.subprocess,'Popen',return_value=Process()), \
     patch.object(meta_asr,'run_session',side_effect=session):
    audio_worker.run_capture_meta('microphone','ENDPOINTING','synthetic-key','fake',helper=Path('/synthetic'),stop=stop)
'''
        import os
        proc = subprocess.Popen([sys.executable, '-u', '-c', script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdin, stdout, stderr_pipe = proc.stdin, proc.stdout, proc.stderr
        assert stdin is not None and stdout is not None and stderr_pipe is not None
        ready = threading.Event()
        rows = []
        def read_output():
            for line in stdout:
                rows.append(line)
                if line.strip() == b'SYNTHETIC_READY':
                    ready.set()
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            self.assertTrue(ready.wait(5), 'synthetic worker did not become ready')
            if os.name == 'nt' or os.environ.get('MC_TEST_STOP_VIA_STDIN') == '1':
                stdin.write(b'x')
                stdin.flush()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=2.5)
            except subprocess.TimeoutExpired:
                self.fail('worker did not emit its summary within the default graceful stop window')
            reader.join(timeout=2)
            self.assertFalse(reader.is_alive(), 'worker output reader did not reach EOF')
            stderr = stderr_pipe.read()
            self.assertEqual(proc.returncode, 0, stderr.decode())
            events = [json.loads(row) for row in rows if row.strip() != b'SYNTHETIC_READY']
            stopped = [e for e in events if e.get('state') == 'stopped']
            self.assertEqual(len(stopped), 1)
            self.assertEqual(stopped[0]['native_dropped_chunks'], 2)
            self.assertIn('session drain was incomplete', stopped[0]['warning'].lower())
            self.assertEqual(events[-1], stopped[0])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
            reader.join(timeout=2)
            stdin.close()
            stdout.close()
            stderr_pipe.close()

    def test_ended_source_bounds_a_stuck_other_session_and_warns(self):
        script = r'''
import asyncio, base64, io, json, threading
from pathlib import Path
from unittest.mock import patch
import audio_worker, meta_asr
pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
line = (json.dumps({'type':'pcm','source':'microphone','rate':16000,'samples':171,
                    'timestamp':1.0,'pcm':pcm})+'\n').encode()
class Process:
    stdout=io.BytesIO(line)
    def poll(self): return 0
events=[]
cancelled=[]
async def session(q, _emit, _key, _model, _mode, label):
    if label == 'you':
        await q.get()
        return
    try: await asyncio.Event().wait()
    finally: cancelled.append(True)
stop=threading.Event()
with patch.object(audio_worker.subprocess,'Popen',return_value=Process()), \
     patch.object(audio_worker,'emit',side_effect=events.append), \
     patch.object(meta_asr,'run_session',side_effect=session), \
     patch.object(audio_worker,'META_SESSION_DRAIN_TIMEOUT',.1,create=True):
    try: audio_worker.run_capture_meta('both','ENDPOINTING','synthetic-key','fake',helper=Path('/synthetic'),stop=stop)
    finally: stop.set()
print(json.dumps({'events':events,'cancelled':cancelled}))
'''
        try:
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=4)
        except subprocess.TimeoutExpired:
            self.fail('ended Meta source stranded a second session')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['cancelled'], [True])
        stopped = [e for e in payload['events'] if e.get('state') == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertIn('session', stopped[0]['warning'].lower())
        self.assertIn('incomplete', stopped[0]['warning'].lower())
        self.assertEqual(payload['events'][-1], stopped[0])

    def test_one_source_error_retires_helper_and_drains_other_source(self):
        script = r'''
import base64, json, threading
from pathlib import Path
from unittest.mock import patch
import audio_worker, meta_asr
pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
def row(source, drops):
    return (json.dumps({'type':'pcm','source':source,'rate':16000,'samples':171,
                        'timestamp':1.0,'pcm':pcm,'native_dropped_chunks':drops})+'\n').encode()
terminated=threading.Event()
class Pipe:
    def __iter__(self):
        yield row('microphone', 2)
        terminated.wait(10)
        yield row('system', 3)
class Process:
    stdout=Pipe()
    def poll(self): return 0 if terminated.is_set() else None
    def terminate(self): terminated.set()
    def wait(self, timeout=None): return 0
events=[]
remote=[]
async def session(q, _emit, _key, _model, _mode, label):
    if label == 'you':
        await q.get()
        raise meta_asr.MetaAsrError('synthetic private error')
    while True:
        item = await q.get()
        if item is None: break
        remote.append(item)
stop=threading.Event()
with patch.object(audio_worker.subprocess,'Popen',return_value=Process()), \
     patch.object(audio_worker,'emit',side_effect=events.append), \
     patch.object(meta_asr,'run_session',side_effect=session):
    try:
        try: audio_worker.run_capture_meta('both','ENDPOINTING','synthetic-key','fake',helper=Path('/synthetic'),stop=stop)
        except meta_asr.MetaAsrError: pass
        else: raise AssertionError('Meta error was suppressed')
    finally: stop.set()
print(json.dumps({'events':events,'remote_count':len(remote),'terminated':terminated.is_set()}))
'''
        try:
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=4)
        except subprocess.TimeoutExpired:
            self.fail('one ended Meta source stranded the other source and helper')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload['terminated'])
        self.assertEqual(payload['remote_count'], 1)
        stopped = [e for e in payload['events'] if e.get('state') == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['native_dropped_chunks'], 5)
        self.assertEqual(stopped[0]['worker_dropped_chunks'], 0)
        self.assertFalse(stopped[0]['reader_incomplete'])
        self.assertEqual(payload['events'][-1], stopped[0])
        self.assertNotIn('synthetic private', result.stdout)
        self.assertNotIn('synthetic-key', result.stdout)

    def test_full_queue_still_delivers_eof_and_reports_evictions(self):
        script = r'''
import asyncio, base64, io, json, threading
from pathlib import Path
from unittest.mock import patch
import audio_worker, meta_asr
pcm = base64.b64encode(bytes(171 * 4)).decode('ascii')
line = (json.dumps({'type':'pcm','source':'microphone','rate':16000,'samples':171,'timestamp':1.0,'pcm':pcm}) + '\n').encode()
class FakeProcess:
    stdout = io.BytesIO((json.dumps({'type':'status','native_dropped_chunks':2})+'\n').encode() + line * 70)
    def poll(self): return 0
events=[]
received=[]
async def slow_session(q, *_args):
    # Wait until the producer has enqueued EOF before consuming anything.
    while None not in q._queue:
        await asyncio.sleep(.001)
    while True:
        item = await q.get()
        if item is None: break
        received.append(item)
stop=threading.Event()
with patch.object(audio_worker.subprocess,'Popen',return_value=FakeProcess()), \
     patch.object(audio_worker,'emit',side_effect=events.append), \
     patch.object(meta_asr,'run_session',side_effect=slow_session):
    try: audio_worker.run_capture_meta('microphone','ENDPOINTING','fake','fake',helper=Path('/synthetic'),stop=stop)
    finally: stop.set()
print(json.dumps({'events':events,'received':len(received)}))
'''
        try:
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=4)
        except subprocess.TimeoutExpired:
            self.fail('full Meta queue stranded the terminal sentinel')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        events = payload['events']
        stopped = [e for e in events if e.get('state') == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['native_dropped_chunks'], 2)
        # Six normal offers and the EOF each evict one of 70 frames.
        self.assertEqual(stopped[0]['worker_dropped_chunks'], 7)
        self.assertEqual(payload['received'] + stopped[0]['worker_dropped_chunks'], 70)
        self.assertEqual(stopped[0]['dropped_chunks'], 9)
        updates = [e for e in events if e.get('state') == 'running' and e.get('worker_dropped_chunks')]
        self.assertTrue(updates)
        self.assertEqual(updates[-1]['worker_dropped_chunks'], 7)
        self.assertTrue(all(e['native_dropped_chunks'] == 2 and 'some' in e['warning'].lower() for e in updates))
        combined = [e['dropped_chunks'] for e in events if 'dropped_chunks' in e]
        self.assertEqual(combined, sorted(combined))


if __name__ == '__main__':
    unittest.main()
