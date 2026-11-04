"""Owner-only, disk-only MODEL_API_KEY editor for Hermes Desktop.

Uses Hermes' existing .env lock/atomic writer, not a second secret store.
No environment publication, provider probe, reveal, logging or background reload.
See docs/meta-api-key.md for the deliberately narrow trust/compatibility boundary.
"""
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

KEY = 'MODEL_API_KEY'
_revision_secret = secrets.token_bytes(32)
_KEY_SHAPE = re.compile(r'[A-Za-z0-9._~+/=-]{8,4096}\Z')
_UNAVAILABLE = 'Key settings unavailable. Use Hermes Settings → Plugins → Meeting Copilot, or check the active home permissions and host version.'


def _host():
    # Import only on an owner-authorized request. Older/hosted/multiplexed
    # hosts fail closed, never fall back to an unscoped file or custom lock.
    from agent.secret_scope import serves_routed_profile
    from hermes_cli import config
    from hermes_constants import get_hermes_home
    if serves_routed_profile() or Path(get_hermes_home()).resolve() != _home().resolve():
        raise RuntimeError('Unsupported profile scope')
    for name in ('_env_write_lock', '_write_env_lines', 'require_env_writable', 'invalidate_env_cache'):
        if not callable(getattr(config, name, None)):
            raise TypeError('Unsupported host version')
    return config


def _home():
    return Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes').absolute()


def _check(path, directory=False, optional=False, confidential=True):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return
        raise
    if (info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode)
            or (directory and (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022))
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & (0o077 if confidential else 0o022)))):
        raise RuntimeError('Unsafe credential path')


def _path():
    home = _home()
    # Reject writable/symlink ancestry too: no cross-user parent swaps.
    for parent in (home, *home.parents):
        info = parent.lstat()
        trusted_sticky = (parent != home and info.st_mode & stat.S_ISVTX
                          and info.st_uid in (0, os.getuid()))
        if (info.st_uid not in (0, os.getuid()) or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or (info.st_mode & 0o022 and not trusted_sticky)):
            raise RuntimeError('Unsafe credential directory')
    _check(home, directory=True)
    path = home / '.env'
    _check(path, optional=True)
    _check(home / '.env.lock', optional=True, confidential=False)
    return path


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return b''
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise RuntimeError('Unsafe credential file')
        raw = file.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise RuntimeError('Credential file too large')
    return raw


def _bindings(raw):
    # Parse with Hermes' dotenv dependency but never expand references, log
    # malformed input, normalize other lines, or expose another key's value.
    from dotenv.parser import parse_stream
    text = raw.decode('utf-8')
    bindings = list(parse_stream(io.StringIO(text)))
    if any(b.error for b in bindings):
        raise RuntimeError('Malformed credential file')
    return bindings


def _saved(bindings):
    values = [b.value or '' for b in bindings if b.key == KEY]
    return values[-1] if values else ''


def _revision(path, raw):
    # Keyed opaque revision, not a public credential hash or partial preview.
    return hmac.new(_revision_secret, str(path).encode() + b'\0' + raw, hashlib.sha256).hexdigest()


def _status(path, raw):
    saved = _saved(_bindings(raw))
    runtime = os.environ.get(KEY, '').strip()
    return {'saved': bool(saved), 'runtime_configured': len(runtime) >= 8,
            'runtime_override': bool(runtime) and runtime != saved,
            'reload_required': saved != runtime, 'verified': False,
            'revision': _revision(path, raw)}


def _write(host, path, raw, action, key):
    bindings = _bindings(raw)
    # Preserve every unrelated binding/comment, including multiline values.
    text = ''.join(b.original.string for b in bindings if b.key != KEY)
    if action == 'save':
        if text and not text.endswith('\n'):
            text += '\n'
        text += KEY + '=' + key + '\n'
    # Hermes' helper uses an adjacent 0600 temp file + fsync + atomic rename.
    # The shared .env.lock serializes with Hermes config writers.
    _path()
    if _read(path) != raw:
        raise HTTPException(409, 'Settings changed. Recheck before saving again.')
    host._write_env_lines(path, [text], preserve_mode=False)
    _check(path)
    actual = _read(path)
    if actual != text.encode('utf-8'):
        raise RuntimeError('Credential readback failed')
    host.invalidate_env_cache()
    return actual


def _authorize(request):
    # A native principal is set ONLY by Hermes' verified same-OS-user IPC
    # middleware. Never infer ownership from loopback, Host, cookies or headers.
    # Hosted authentication and shared dashboard users are intentionally refused.
    grant = getattr(request.state, 'native_http_principal', None)
    if (not isinstance(grant, dict) or grant.get('capabilities') != frozenset({'http:owner'})
            or not isinstance(grant.get('profile_id'), str)
            or Path(grant['profile_id']).absolute() != _home()):
        raise HTTPException(403, 'Key editing requires the local Hermes Desktop owner connection for this home.')
    if set(request.query_params) - {'profile'}:
        raise HTTPException(400, 'Only the host profile selector is accepted.')
    # ctx.rest may append its own profile selector. Honor only a selector the
    # host resolves to this already-authorized home; never use it as a file path.
    try:
        for selector in request.query_params.getlist('profile'):
            if selector.strip().lower() not in ('', 'current'):
                from hermes_cli.dashboard_auth.native_http import _own_profile
                _own_profile(selector, grant['profile_id'])
    except Exception:  # noqa: BLE001 -- credential boundary: sanitize all failures
        raise HTTPException(403, 'Key settings are limited to this backend home.') from None


def _response(value):
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


def make_router():
    router = APIRouter()

    @router.get('/credentials/meta')
    def status(request: Request):
        _authorize(request)
        try:
            _host()
            path = _path()
            return _response(_status(path, _read(path)))
        except Exception:  # noqa: BLE001 -- credential boundary: sanitize all failures
            raise HTTPException(503, _UNAVAILABLE) from None

    @router.post('/credentials/meta')
    async def update(request: Request):
        _authorize(request)
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            raise HTTPException(400, 'Use a JSON key-settings request.')
        raw_body = bytearray()
        async for chunk in request.stream():
            raw_body.extend(chunk)
            if len(raw_body) > 8192:
                raise HTTPException(413, 'Key-settings request too large.')
        try:
            body = json.loads(raw_body)
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Invalid key-settings request.') from None
        finally:
            raw_body.clear()
        # Do not use Pydantic request validation: its default 422 includes input.
        if not isinstance(body, dict) or set(body) - {'action', 'key', 'revision'}:
            raise HTTPException(400, 'Invalid key-settings request.')
        action, key, revision = body.get('action'), body.get('key'), body.get('revision')
        if (action not in ('save', 'remove') or not isinstance(revision, str) or len(revision) != 64
                or (action == 'save' and (not isinstance(key, str) or not _KEY_SHAPE.fullmatch(key)))
                or (action == 'remove' and 'key' in body)):
            raise HTTPException(400, 'Enter an 8–4096 character API key without spaces, quotes or line breaks; recheck settings before saving.')
        try:
            host = _host()
            host.require_env_writable(KEY, action)
            path = _path()
            # Blocking filesystem work runs off the ASGI event loop; the active
            # context propagates, and no key is passed to a subprocess/argv.
            import asyncio
            def apply():
                with host._env_write_lock(path):
                    _path()
                    raw = _read(path)
                    if not hmac.compare_digest(revision, _revision(path, raw)):
                        raise HTTPException(409, 'Settings changed. Recheck before saving again.')
                    updated = _write(host, path, raw, action, key)
                    return _status(path, updated)
            return _response(await asyncio.to_thread(apply))
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 -- credential boundary: sanitize all failures
            raise HTTPException(503, _UNAVAILABLE) from None
        finally:
            body.clear()
            key = None

    return router
