"""Explicit Google preview admission. No network, ambient auth or credential files.

The durable OS-user ledger is shared across profiles and releases. Reservations
are conservative local accounting, NOT a provider bill or guaranteed dollar cap.
Uncertain attempts are never refunded. There is intentionally no reset route.
"""
import importlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat

KEY_NAME = 'MEETING_COPILOT_GEMINI_API_KEY'
MAX_STREAMS = 4
RESERVATION_USD_PER_STREAM = 1
MAX_AUDIO_SECONDS = 60


def _byok():
    return importlib.import_module('.byok', __package__) if __package__ else importlib.import_module('byok')


def scoped_key():
    return _byok().secret(KEY_NAME)


def _require_scope():
    if not _byok()._scope_available():
        raise RuntimeError('Google preview needs a host-bound profile scope.')


def _path():
    # Not a profile path: changing profile or updating the plugin cannot reset
    # the shared preview allowance. This file contains counts only, never keys.
    return Path.home() / '.meeting-copilot' / 'google-preview-v1.json'


def _streams(source):
    if source not in ('microphone', 'system', 'both'):
        raise ValueError('Unsupported Google audio source.')
    return 2 if source == 'both' else 1


def _count(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {'version', 'reserved_streams'} or type(value['version']) is not int or value['version'] != 1:
        raise ValueError()
    count = value['reserved_streams']
    if type(count) is not int or not 0 <= count <= MAX_STREAMS:
        raise ValueError()
    return count


def _open(*, create=False):
    import fcntl
    path = _path()
    if create:
        try:
            path.parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        directory = path.parent.lstat()
    except FileNotFoundError:
        if not create:
            return None, False
        raise
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid():
        raise ValueError()
    flags = os.O_RDWR if create else os.O_RDONLY
    flags |= os.O_NOFOLLOW
    fresh = False
    if create:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            fresh = True
        except FileExistsError:
            fd = os.open(path, flags)
    else:
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None, False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError()
        if create:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Read-only checks are advisory snapshots, never admission. Do not let
        # a reader lock the just-created empty ledger ahead of its initializer.
        # A concurrent/partial read fails closed; reserve rechecks under both
        # exclusive locks before any worker or provider call.
        return fd, fresh
    except BaseException:
        os.close(fd)
        raise


def budget_status(source='microphone'):
    """Read-only, local availability. Never creates a file or contacts Google."""
    fd = None
    try:
        _require_scope()
        required = _streams(source)
        fd, _ = _open()
        used = 0 if fd is None else _count(os.read(fd, 4097))
        return {'available': used + required <= MAX_STREAMS, 'reserved_usd': used,
                'remaining_reservation_usd': MAX_STREAMS - used, 'max_audio_seconds': MAX_AUDIO_SECONDS,
                'detail': ('Google preview: 60 seconds per source; source labels only. Local reservations are not actual charges.'
                           if used + required <= MAX_STREAMS else
                           'Google preview allowance exhausted. Review usage before any separately authorized extension.')}
    except Exception:
        return {'available': False, 'max_audio_seconds': MAX_AUDIO_SECONDS,
                'detail': 'Google preview allowance could not be verified. Check profile scope and the local reservation ledger; no call is permitted.'}
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def _admission_lock():
    """Lock before ledger creation, so competing first calls cannot poison it.

    A stable sidecar is necessary: locking only the newly created empty ledger
    allows another opener to take its lock before the creator initializes it.
    The sidecar contains no data. Read-only preflight never creates either file.
    """
    import fcntl
    path = _path()
    try:
        path.parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    directory = path.parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid():
        raise ValueError()
    fd = os.open(path.with_suffix(path.suffix + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError()
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def reserve(source='microphone'):
    """No scope/invalid source may create even the empty admission lock."""
    try:
        _require_scope()
        _streams(source)
        with _admission_lock():
            return _reserve_locked(source)
    except Exception:
        raise RuntimeError('Google preview allowance is unavailable or exhausted. No provider call was admitted.') from None


def _reserve_locked(source='microphone'):
    """Durably reserve every planned stream before any socket/worker attempt."""
    fd = None
    try:
        _require_scope()
        required = _streams(source)
        fd, fresh = _open(create=True)
        used = 0 if fresh else _count(os.read(fd, 4097))
        if used + required > MAX_STREAMS:
            raise ValueError()
        value = {'version': 1, 'reserved_streams': used + required}
        payload = json.dumps(value, separators=(',', ':')).encode('ascii')
        os.lseek(fd, 0, os.SEEK_SET)
        if os.write(fd, payload) != len(payload):
            raise OSError()
        os.ftruncate(fd, len(payload))
        os.fsync(fd)
        if fresh:
            parent = os.open(_path().parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        return {'reservation_usd': required, 'reserved_usd': used + required,
                'remaining_reservation_usd': MAX_STREAMS - used - required}
    except Exception:
        raise RuntimeError('Google preview allowance is unavailable or exhausted. No provider call was admitted.') from None
    finally:
        if fd is not None:
            os.close(fd)
