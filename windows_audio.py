"""Isolated Windows-only local capture adapter. Construction never opens devices."""
import os
from pathlib import Path
import re
import subprocess
import sys

try:
    from .macos_audio import MacOSAudioCapture
except ImportError:
    from macos_audio import MacOSAudioCapture


def worker_environment():
    # Exact names only: never forward host tokens, Python startup hooks, proxy
    # credentials, broad HF_HUB_* prefixes, or dynamic-loader configuration.
    allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'USERPROFILE',
               'LOCALAPPDATA', 'APPDATA', 'HOME', 'LANG', 'LC_ALL',
               'HF_HOME', 'HF_HUB_CACHE'}
    env = {key.upper(): value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(HF_HUB_DISABLE_IMPLICIT_TOKEN='1', HF_HUB_DISABLE_TELEMETRY='1',
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONNOUSERSITE='1',
               PYTHONDONTWRITEBYTECODE='1', TOKENIZERS_PARALLELISM='false')
    return env


def worker_python(root=None):
    """The gateway's actual interpreter; no PATH resolution or ambient override."""
    executable = Path(sys.executable)
    if (not executable.is_absolute() or not executable.is_file()
            or not re.fullmatch(r'python(?:w)?(?:\d+(?:\.\d+)*)?(?:\.exe)?', executable.name, re.IGNORECASE)):
        raise RuntimeError('Windows audio requires a Python gateway interpreter.')
    return str(executable)


class WindowsAudioCapture(MacOSAudioCapture):
    def __init__(self, model_size='base.en', source='microphone', language='en',
                 provider='local', asr_mode='ENDPOINTING'):
        if provider != 'local':
            raise ValueError('Windows capture supports prepared local transcription only.')
        super().__init__(model_size, source, language, provider, asr_mode)
        self._control_sent = False

    def _notify(self, state, code=None):
        if code == 'runtime':
            code = 'windows_runtime'
        elif code == 'capture':
            code = 'windows_capture'
        return super()._notify(state, code)

    def devices(self):
        return {'backend': 'windows-isolated', 'sources': ['microphone', 'system', 'both'],
                'default_source': 'microphone', 'models': ['tiny.en', 'base.en', 'small.en'],
                'providers': ['local'], 'permissions_checked': False}

    def _worker_artifacts(self, root):
        return None

    def _worker_environment(self):
        return worker_environment()

    def _worker_command(self, root, artifacts):
        self._control_sent = False
        return [worker_python(root), '-B', '-u', str(root / 'windows_audio_worker.py'),
                '--model', self.model_size, '--source', self.source]

    def _worker_stdin(self, credential_frame):
        if credential_frame is not None:
            raise RuntimeError('Windows local capture never accepts credentials.')
        return subprocess.PIPE

    def _worker_process_options(self):
        # No shell or inherited console; Windows worker does not create children.
        return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}

    def _request_worker_stop(self, process):
        with self._termination_lock:
            if self._control_sent or process.poll() is not None:
                return
            self._control_sent = True
            if process.stdin is not None:
                process.stdin.write(b'{"action":"stop"}\n')
                process.stdin.flush()
                process.stdin.close()

    def _terminate(self, process, timeout):
        # Unlike POSIX terminate_group, Windows has no child tree to signal:
        # this fixed worker does not launch helpers. Forced kill is a fallback
        # only, after parent.stop's private-pipe graceful window has elapsed.
        with self._termination_lock:
            with self._lock:
                self._cleanup_confirmed = False
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=max(.1, timeout))
            except subprocess.TimeoutExpired:
                raise RuntimeError('Windows audio worker termination is unconfirmed') from None
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            with self._lock:
                self._cleanup_confirmed = True
