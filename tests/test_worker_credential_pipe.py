"""Synthetic checks of the private, one-shot Meta worker credential handoff."""
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import audio_worker
import macos_audio


class WorkerCredentialPipeTests(unittest.TestCase):
    def test_bootstrap_is_bounded_and_rejects_invalid_frames(self):
        frame = macos_audio._meta_worker_frame('synthetic-scoped-key')
        self.assertEqual(audio_worker._read_meta_worker_key(io.BytesIO(frame)), 'synthetic-scoped-key')
        for bad in (b'', b'{}\n', b'{"type":"scoped_meta_key","key":"x"}',
                    b'{"type":"scoped_meta_key","key":"x","extra":1}\n', b'x' * 4097):
            with self.subTest(frame=bad[:30]):
                self.assertEqual(audio_worker._read_meta_worker_key(io.BytesIO(bad)), '')
        for key in ('', 'x\ny', 'x' * 5000):
            with self.subTest(length=len(key)), self.assertRaises(RuntimeError):
                macos_audio._meta_worker_frame(key)

    def test_missing_host_scope_cannot_spawn_meta_worker(self):
        capture = macos_audio.MacOSAudioCapture(provider='meta')
        with patch.dict(os.environ, {'MODEL_API_KEY': 'synthetic-launch-key'}), \
             patch('macos_audio._model_api_key', return_value=''), \
             patch('subprocess.Popen', side_effect=AssertionError('must not spawn')):
            with self.assertRaisesRegex(RuntimeError, 'profile-scoped Hermes key'):
                capture.start(lambda _: None)
        self.assertEqual(capture.status()['state'], 'idle')

    def test_parent_sends_scoped_key_over_pipe_without_env_or_output_leak(self):
        script = (
            "import json,os,sys,time; "
            "frame=json.loads(sys.stdin.buffer.readline(4097)); "
            "assert frame=={'type':'scoped_meta_key','key':'synthetic-scoped-key'}; "
            "assert 'MODEL_API_KEY' not in os.environ; "
            "print(json.dumps({'type':'status','state':'running'}),flush=True); time.sleep(30)"
        )
        real_popen = subprocess.Popen
        calls = []
        def spawn(command, **kwargs):
            calls.append((command, dict(kwargs.get('env') or {})))
            return real_popen([sys.executable, '-u', '-c', script], **kwargs)
        capture = macos_audio.MacOSAudioCapture(provider='meta')
        started = threading.Event()
        events = []
        with patch.dict(os.environ, {'MODEL_API_KEY': 'synthetic-wrong-key',
                                     'MEETING_COPILOT_AUDIO_PYTHON': sys.executable}), \
             patch('macos_audio._model_api_key', return_value='synthetic-scoped-key'), \
             patch('subprocess.Popen', side_effect=spawn):
            try:
                capture.start(lambda _: None, lambda event: (events.append(event),
                    started.set() if event['state'] == 'running' else None))
                self.assertTrue(started.wait(4), capture.status())
            finally:
                capture.stop(timeout=1)
        self.assertNotIn('MODEL_API_KEY', calls[0][1])
        self.assertNotIn('synthetic-scoped-key', repr(calls))
        self.assertNotIn('synthetic-scoped-key', json.dumps(events))

    def test_worker_ignores_environment_and_consumes_only_private_frame(self):
        script = (
            "import audio_worker,meta_asr,sys; "
            "meta_asr.transcribe_file=lambda path,key,*a: "
            "[{'type':'segment','speaker':'you','text':'synthetic','key_ok':key=='scoped-fixture'}]; "
            "raise SystemExit(audio_worker.main(['--file',sys.argv[1],'--provider','meta']))"
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'synthetic.wav'
            path.write_bytes(b'fixture; no audio')
            env = dict(os.environ, MODEL_API_KEY='synthetic-launch-key')
            for frame, expected in ((b'', 1), (macos_audio._meta_worker_frame('scoped-fixture'), 0)):
                with self.subTest(frame_present=bool(frame)):
                    result = subprocess.run([sys.executable, '-c', script, str(path)],
                                            input=frame, capture_output=True, env=env, timeout=10)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    events = [json.loads(line) for line in result.stdout.splitlines()]
                    if frame:
                        self.assertTrue(any(e.get('key_ok') for e in events))
                    else:
                        self.assertTrue(any(e.get('code') == 'asr_auth' for e in events))
                    self.assertNotIn(b'scoped-fixture', result.stdout + result.stderr)
                    self.assertNotIn(b'synthetic-launch-key', result.stdout + result.stderr)

    @unittest.skipUnless(importlib.util.find_spec('agent'), 'requires Hermes scope API')
    def test_new_capture_uses_rotated_scope_and_deletion_denies_new_worker(self):
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home().resolve())
        real_popen = subprocess.Popen
        expected = iter(('synthetic-old', 'synthetic-new'))
        calls = []
        def spawn(command, **kwargs):
            key = next(expected)
            script = ("import json,sys,time; p=json.loads(sys.stdin.buffer.readline(4097)); "
                      f"assert p['key']=={key!r}; "
                      "print(json.dumps({'type':'status','state':'running'}),flush=True); time.sleep(30)")
            calls.append(command)
            return real_popen([sys.executable, '-u', '-c', script], **kwargs)
        old, new = (macos_audio.MacOSAudioCapture(provider='meta') for _ in range(2))
        ready_old, ready_new = threading.Event(), threading.Event()
        with patch.dict(os.environ, {'MODEL_API_KEY': 'synthetic-wrong',
                                     'MEETING_COPILOT_AUDIO_PYTHON': sys.executable}), \
             patch('subprocess.Popen', side_effect=spawn):
            try:
                token = set_secret_scope({'MODEL_API_KEY': 'synthetic-old'}, profile_home=home)
                try:
                    old.start(lambda _: None, lambda e: ready_old.set() if e['state'] == 'running' else None)
                finally:
                    reset_secret_scope(token)
                self.assertTrue(ready_old.wait(4), old.status())
                token = set_secret_scope({'MODEL_API_KEY': 'synthetic-new'}, profile_home=home)
                try:
                    new.start(lambda _: None, lambda e: ready_new.set() if e['state'] == 'running' else None)
                finally:
                    reset_secret_scope(token)
                self.assertTrue(ready_new.wait(4), new.status())
                self.assertTrue(old.status()['running'])
                token = set_secret_scope({}, profile_home=home)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'profile-scoped Hermes key'):
                        macos_audio.MacOSAudioCapture(provider='meta').start(lambda _: None)
                finally:
                    reset_secret_scope(token)
                self.assertEqual(len(calls), 2)
            finally:
                old.stop(timeout=1)
                new.stop(timeout=1)
