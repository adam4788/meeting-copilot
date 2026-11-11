"""Fail-closed capture admission checks with a separate explicit permission action.

check() is read-only and never requests permission.

The check subprocess only loads installed libraries and local model weights.
Explicit --prepare-model can download public weights; explicit permission setup
can prompt. Neither action records audio or installs dependencies.
"""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

MODELS = ('tiny.en', 'base.en', 'small.en')
SOURCES = ('microphone', 'system', 'both')
MODES = ('ENDPOINTING', 'DIARIZATION', 'PUSH_TO_TALK')


def _module(name):
    return importlib.import_module('.' + name, __package__) if __package__ else importlib.import_module(name)


def _row(identifier, label, status, detail):
    return dict(id=identifier, label=label, status=status, detail=detail)


def _json_command(command, env, timeout=20):
    # Only our fixed helper/probe commands are passed here, never user commands.
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env, timeout=timeout, check=False)
    if result.returncode != 0 or len(result.stdout) > 16384:
        raise ValueError('Probe failed')
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('Unsupported probe')
    return value


def _runtime_probe(model, provider, windows=False):
    """Executed in the actual audio interpreter, with offline HF settings."""
    result = {'version': 1, 'dependencies': False, 'model': False}
    try:
        importlib.import_module('numpy')
        if windows:
            # Import only; do not enumerate or open devices.
            importlib.import_module('soundcard')
        if provider == 'meta':
            importlib.import_module('websockets.asyncio.client')
            result['dependencies'] = True
            return result
        whisper = importlib.import_module('faster_whisper')
        result['dependencies'] = True
        # local_files_only prohibits Hub fetching, including missing weights.
        # Offline environment also protects optional tokenizer resolution.
        hub = importlib.import_module('huggingface_hub')
        cached = Path(hub.snapshot_download('Systran/faster-whisper-' + model,
                                           local_files_only=True))
        # Whisper otherwise falls back to Tokenizer.from_pretrained, which may
        # perform a request even when the model's local_files_only flag is set.
        for filename in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.json'):
            with (cached / filename).open('rb') as stream:
                if not stream.read(1):
                    return result
        whisper.WhisperModel(str(cached), device='cpu', compute_type='int8', cpu_threads=1,
                             local_files_only=True)
        result['model'] = True
    except Exception:
        pass  # Never expose exception text, paths, credentials or model contents.
    return result


def _prepare_model_worker(model):
    """Explicit CLI preparation only: download public weights, never audio."""
    if model not in MODELS:
        return {'version': 1, 'prepared': False}
    # Public model preparation must not use stored Hugging Face credentials.
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    try:
        whisper = importlib.import_module('faster_whisper')
        whisper.WhisperModel(model, device='cpu', compute_type='int8', cpu_threads=1)
        return {'version': 1, 'prepared': True}
    except Exception:
        return {'version': 1, 'prepared': False}


def prepare_model(model):
    """Explicit macOS CLI recovery; no server route calls this function.

    Uses the installed version's prepared interpreter, including legacy layouts.
    It can download public model weights into the normal Hugging Face cache.
    """
    if model not in MODELS or sys.platform != 'darwin':
        return {'version': 1, 'prepared': False,
                'detail': 'Model preparation requires macOS and tiny.en, base.en or small.en.'}
    try:
        root = Path(__file__).resolve().parent
        mac = _module('macos_audio')
        artifacts = _module('native_runtime').artifact_dir(root)
        python = mac.worker_python(root, artifacts)
        env = mac.worker_env('local')
        env.update(HF_HUB_DISABLE_IMPLICIT_TOKEN='1', HF_HUB_DISABLE_TELEMETRY='1',
                   PYTHONDONTWRITEBYTECODE='1')
        # This separate explicit command may access the network; check() never
        # takes this branch. Preserve the user's HF_HUB_OFFLINE preference.
        result = _json_command([python, '-B', str(root / 'capture_preflight.py'),
                                '--prepare-model-worker', model], env, timeout=600)
        if result.get('prepared') is not True:
            raise ValueError('Preparation failed')
        return {'version': 1, 'prepared': True,
                'detail': 'Selected model prepared. Reopen the pane and run Recheck required settings again.'}
    except Exception:
        return {'version': 1, 'prepared': False,
                'detail': 'Model preparation failed or timed out. Check the prepared audio runtime, network access, free disk space and offline cache settings, then retry explicitly. No capture was started.'}


def request_permissions(source):
    """Explicit user action only. This path can show OS prompts, never capture.

    Callers must confirm the source and permission action, and fence the pane
    lifecycle. Never call this from status, preflight, startup or retry logic.
    """
    def report(status, detail):
        return {'version': 1, 'status': status, 'capture_started': False, 'detail': detail}
    if source not in SOURCES or sys.platform != 'darwin':
        return report('unavailable', 'Permission setup requires macOS and a supported audio source.')
    try:
        root = Path(__file__).resolve().parent
        mac = _module('macos_audio')
        artifacts = _module('native_runtime').artifact_dir(root)
        helper = mac.capture_helper(root, artifacts)
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise OSError('Missing helper')
    except Exception:
        return report('unavailable', 'Prepare or rebuild the current macOS audio helper before requesting permissions.')
    try:
        probe = _json_command([str(helper), '--request-permissions', '--source', source],
                              mac.worker_env(), timeout=60)
        if (probe.get('source') != source or probe.get('capture_started') is not False
                or probe.get('permission_request_completed') is not True):
            raise ValueError('Invalid permission response')
        return report('completed', 'Permission setup returned without recording. Run Recheck required settings again; denied permissions may need System Settings > Privacy & Security and an app restart.')
    except Exception:
        return report('unknown', 'Permission setup did not confirm completion. Finish or dismiss any existing OS prompt, then check setup. Rebuild the helper if its permission command is unavailable.')


def check(backend, recording_id, transcription_model, audio_source,
          transcription_provider, asr_mode, adapter=None):
    """Return a short-lived readiness observation, not a capture guarantee."""
    rows = []
    def add(identifier, label, ready, detail, unknown=False):
        rows.append(_row(identifier, label, 'ready' if ready else 'unknown' if unknown else 'blocked', detail))
    def finish():
        return {'version': 1, 'ready': bool(rows) and all(r['status'] == 'ready' for r in rows),
                'checks': rows, 'checked_at': time.time(), 'valid_for_seconds': 15}

    if backend == 'meetily':
        try:
            selected = next((r for r in adapter.recordings() if isinstance(r, dict)
                             and r.get('id') == recording_id and r.get('status') == 'recording'), None) if adapter and recording_id else None
            # Listing verifies accessible bounded metadata through the adapter.
            add('recording', 'Active Meetily recording', bool(selected),
                'Selected recording is active and accessible.' if selected else
                'Select an accessible active Meetily recording, then check again.')
        except Exception:
            add('recording', 'Active Meetily recording', False,
                'Cannot read the selected Meetily recording. Check its folder access.', unknown=True)
        return finish()

    valid = (backend in ('macos', 'native') and transcription_model in MODELS
             and audio_source in SOURCES and transcription_provider in ('local', 'meta')
             and asr_mode in MODES and not (backend == 'native' and transcription_provider != 'local'))
    add('selection', 'Capture settings', valid,
        'Capture settings are supported.' if valid else 'Choose supported capture, audio source and transcription settings.')
    if not valid:
        return finish()
    platform_ok = sys.platform == ('darwin' if backend == 'macos' else 'win32')
    add('platform', 'Operating system', platform_ok,
        'Selected capture platform matches this computer.' if platform_ok else 'Choose the capture source for this computer.')
    if not platform_ok:
        return finish()
    mac = _module('macos_audio')
    env = mac.worker_env(transcription_provider)
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
               HF_HUB_DISABLE_TELEMETRY='1', PYTHONDONTWRITEBYTECODE='1')
    root = Path(__file__).resolve().parent
    python = sys.executable
    if backend == 'macos':
        try:
            artifacts = _module('native_runtime').artifact_dir(root)
            python = mac.worker_python(root, artifacts)
            helper = mac.capture_helper(root, artifacts)
            if not helper.is_file() or not os.access(helper, os.X_OK):
                raise OSError('Missing helper')
            probe = _json_command([str(helper), '--preflight', '--source', audio_source], env, timeout=5)
            if probe.get('source') != audio_source or probe.get('capture_started') is not False:
                raise ValueError('Invalid preflight')
            add('helper', 'Prepared native helper', True, 'Native helper supports permission checks without capture.')
            if audio_source in ('microphone', 'both'):
                available = probe.get('microphone_available')
                add('microphone_device', 'Default microphone', available is True,
                    'A default microphone is available.' if available is True else
                    'No default microphone was confirmed. Connect and select a microphone in macOS Sound settings, then Recheck required settings.',
                    unknown=type(available) is not bool)
                allowed = probe.get('microphone') == 'authorized' and probe.get('microphone_usage_description') is True
                add('microphone_permission', 'Microphone permission', allowed,
                    'Microphone access is authorized.' if allowed else
                    'Allow Microphone access for the capture helper in System Settings > Privacy & Security; rebuild the helper if its usage description is missing.')
            if audio_source in ('system', 'both'):
                available = probe.get('display_available')
                add('system_display', 'Active display for system audio', available is True,
                    'An active display is available for system audio capture.' if available is True else
                    'No active display was confirmed. Connect or activate a display, then Recheck required settings.',
                    unknown=type(available) is not bool)
                allowed = probe.get('system') == 'authorized'
                add('system_permission', 'Screen and system audio permission', allowed,
                    'Screen capture permission is authorized.' if allowed else
                    'Allow Screen & System Audio Recording for the capture helper in System Settings > Privacy & Security, then restart the app.')
        except Exception:
            add('helper', 'Prepared native helper', False,
                'Native runtime or permission check is unavailable. Prepare or rebuild the current macOS audio helper, then check again.', unknown=True)
            return finish()
    else:
        add('windows_access', 'Windows audio access', False,
            'Windows device and privacy readiness cannot yet be verified without capture. Check microphone privacy and default audio devices in Windows Settings; use an active Meetily recording until a supported readiness probe is available.', unknown=True)
    try:
        args = [python, '-B', str(root / 'capture_preflight.py'), '--runtime-probe', transcription_model, transcription_provider]
        if backend == 'native':
            args.append('--windows')
        probe = _json_command(args, env)
        add('dependencies', 'Audio runtime dependencies', probe.get('dependencies') is True,
            'Required audio libraries load successfully.' if probe.get('dependencies') is True else
            'Prepare the audio runtime dependencies, then check again.')
        if transcription_provider == 'local':
            add('model_cache', 'Cached local transcription model', probe.get('model') is True,
                'Selected Whisper weights load from the local cache.' if probe.get('model') is True else
                (f'From the installed plugin runtime root, run python -B capture_preflight.py --prepare-model {transcription_model}, then reopen the pane and run Recheck required settings. Preparation may download public weights; this check never downloads them.' if backend == 'macos' else
                 'Prepare the selected Whisper cache in the Windows gateway interpreter using the Windows setup guide. Native Windows access checks remain unsupported; use an active Meetily recording.'))
    except Exception:
        add('runtime', 'Audio runtime and model cache', False,
            'Audio runtime check failed or timed out. Prepare the runtime and selected local model, then check again.', unknown=True)
    if transcription_provider == 'meta':
        present = bool(mac._model_api_key())
        add('transcription_key', 'Meta transcription credential', present,
            'A scoped Meta key is configured; account and network access are not tested.' if present else
            'Configure a Meta key in Hermes plugin settings, then reopen the pane.')
    return finish()


if __name__ == '__main__':
    args = sys.argv[1:]
    if (len(args) in (3, 4) and args[0] == '--runtime-probe' and args[1] in MODELS
            and args[2] in ('local', 'meta') and (len(args) == 3 or args[3] == '--windows')):
        print(json.dumps(_runtime_probe(args[1], args[2], len(args) == 4)))
    elif len(args) == 2 and args[0] == '--prepare-model' and args[1] in MODELS:
        result = prepare_model(args[1])
        print(json.dumps(result))
        sys.exit(0 if result['prepared'] else 1)
    elif len(args) == 2 and args[0] == '--prepare-model-worker' and args[1] in MODELS:
        print(json.dumps(_prepare_model_worker(args[1])))
    else:
        sys.exit(2)
