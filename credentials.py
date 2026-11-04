"""Owner-only, disk-only MODEL_API_KEY editor for Hermes Desktop.

Uses Hermes' existing .env lock/atomic writer, not a second secret store.
No environment publication, provider probe, reveal, logging or background reload.
See docs/meta-api-key.md for the deliberately narrow trust/compatibility boundary.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

if __package__:
    from .credential_env import (KEY, UnsupportedEnvFile, HostTokenizerUnavailable,
                                 TOKENIZER_UNAVAILABLE, require_host_parser, bindings, saved as _saved)
else:
    from credential_env import (KEY, UnsupportedEnvFile, HostTokenizerUnavailable,
                                TOKENIZER_UNAVAILABLE, require_host_parser, bindings, saved as _saved)

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

_revision_secret = secrets.token_bytes(32)
_KEY_SHAPE = re.compile(r'[A-Za-z0-9._~+/=-]{8,4096}\Z')
_REVISION_SHAPE = re.compile(r'[0-9a-f]{64}\Z')
_UNAVAILABLE = 'Key settings unavailable. Use Hermes Settings → Plugins → Meeting Copilot, or check the active home permissions and host version.'
_UNSUPPORTED_FILE = 'Unsupported credential file syntax. No changes made. Multiline values or parser differences require manual review with the host administrator; this editor will not rewrite unrelated data.'


def _scope():
    try:
        from agent import secret_scope
    except ImportError:
        raise HostTokenizerUnavailable(TOKENIZER_UNAVAILABLE) from None
    return secret_scope


def _bindings(raw):
    # Use the installed host tokenizer on the same immutable bytes, not a
    # second file read or a copied approximation of the host's parser.
    return bindings(raw, host_parser=require_host_parser(_scope()))


def _host():
    # Import only on an owner-authorized request. Older/hosted/multiplexed
    # hosts fail closed, never fall back to an unscoped file or custom lock.
    require_host_parser(_scope())
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


def _check_acl(path, ancestor=False, preserve_metadata=False):
    """Refuse ACLs before the host can create a plaintext temporary file.

    chmod(0600) does not remove macOS ACLs. Do not repair them or silently
    discard existing metadata. Ancestors may have deny-only macOS ACLs (the
    standard home deny-delete entry); any allow entry is refused there too.
    Unsupported platforms or failed metadata inspection fail closed.
    """
    if sys.platform == 'darwin':
        import ctypes
        import errno
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        libc.acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_int]
        libc.acl_get_file.restype = ctypes.c_void_p
        libc.acl_valid.argtypes = [ctypes.c_void_p]
        libc.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        libc.acl_get_tag_type.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        libc.acl_free.argtypes = [ctypes.c_void_p]
        ctypes.set_errno(0)
        acl = libc.acl_get_file(os.fsencode(path), 0x100)  # ACL_TYPE_EXTENDED
        if not acl and ctypes.get_errno() != errno.ENOENT:
            raise RuntimeError('Cannot inspect credential ACL')
        if not acl:
            # Darwin returns ENOENT for an absent extended ACL as well as an
            # absent file. Confirm the already-checked object still exists.
            if stat.S_ISLNK(path.lstat().st_mode):
                raise RuntimeError('Unsafe credential path')
        try:
            if acl and libc.acl_valid(acl) != 0:
                raise RuntimeError('Invalid credential ACL')
            entry, tag = ctypes.c_void_p(), ctypes.c_int()
            which = 0  # ACL_FIRST_ENTRY, then ACL_NEXT_ENTRY
            while acl:
                ctypes.set_errno(0)
                result = libc.acl_get_entry(acl, which, ctypes.byref(entry))
                if result == -1 and ctypes.get_errno() == errno.EINVAL:
                    break  # Darwin's documented end-of-entries result
                if result != 0 or not ancestor:
                    raise RuntimeError('Credential ACL unsupported')
                if libc.acl_get_tag_type(entry, ctypes.byref(tag)) != 0 or tag.value != 2:
                    raise RuntimeError('Unsafe ancestor ACL')  # allow only ACL_EXTENDED_DENY
                which = -1
        finally:
            libc.acl_free(acl)
        if preserve_metadata:
            libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
            libc.listxattr.restype = ctypes.c_ssize_t
            size = libc.listxattr(os.fsencode(path), None, 0, 1)  # XATTR_NOFOLLOW
            if size < 0 or size > 65536:
                raise RuntimeError('Credential metadata unsupported')
            names = ctypes.create_string_buffer(size)
            if libc.listxattr(os.fsencode(path), names, size, 1) != size:
                raise RuntimeError('Credential metadata changed')
            if set(names.raw.split(b'\0')) - {b'', b'com.apple.provenance'}:
                raise RuntimeError('Credential metadata unsupported')
    elif sys.platform.startswith('linux'):
        names = os.listxattr(path, follow_symlinks=False)
        if (preserve_metadata and names) or any(name.startswith('system.posix_acl_') for name in names):
            raise RuntimeError('Credential ACL or metadata unsupported')
    else:
        raise RuntimeError('Credential permissions unsupported on this platform')
    if preserve_metadata and getattr(path.lstat(), 'st_flags', 0):
        raise RuntimeError('Credential file flags unsupported')


def _provenance(path):
    """Read the one macOS-generated xattr we allow; never read user xattrs."""
    if sys.platform != 'darwin':
        return None
    import ctypes
    import errno
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                             ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    libc.getxattr.restype = ctypes.c_ssize_t
    args = (os.fsencode(path), b'com.apple.provenance')
    size = libc.getxattr(*args, None, 0, 0, 1)
    if size == -1 and ctypes.get_errno() == errno.ENOATTR:
        return None
    if size < 0 or size > 65536:
        raise RuntimeError('Cannot inspect credential metadata')
    data = ctypes.create_string_buffer(size)
    if libc.getxattr(*args, data, size, 0, 1) != size:
        raise RuntimeError('Credential metadata changed')
    return data.raw


def _write_preserving_metadata(host, path, lines):
    """Stage the host's atomic write privately; publish only verified metadata.

    macOS automatically attaches provenance to new files. Permit that metadata
    only when the replacement has exactly the original value; otherwise refuse
    without replacing the original. Other extended metadata is refused earlier.
    """
    original = path.lstat() if path.exists() else None
    provenance = _provenance(path) if original else None
    with tempfile.TemporaryDirectory(prefix='.env-stage-', dir=path.parent) as directory:
        staging = Path(directory)
        _check(staging, directory=True)  # before the host opens any secret temp
        candidate = staging / '.env'
        host._write_env_lines(candidate, lines, preserve_mode=False)
        _check(candidate)
        if original:
            if _provenance(candidate) != provenance:
                raise RuntimeError('Credential metadata cannot be preserved')
            os.chown(candidate, -1, original.st_gid)
            os.chmod(candidate, stat.S_IMODE(original.st_mode))
        _path()  # recheck target ACLs/metadata before publication
        os.replace(candidate, path)


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
    _check_acl(path, preserve_metadata=not directory and confidential)


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
        _check_acl(parent, ancestor=parent != home)
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


def _revision(path, raw):
    # Keyed opaque revision, not a public credential hash or partial preview.
    return hmac.new(_revision_secret, str(path).encode() + b'\0' + raw, hashlib.sha256).hexdigest()


def _status(path, raw):
    saved = _saved(_bindings(raw))
    process = os.environ.get(KEY, '').strip()
    scope = _scope()
    # _host already requires the owner's scope. Read that same trusted snapshot
    # without importing a provider module (which could run a startup bridge).
    request_key = scope.get_secret(KEY, '') or ''
    return {'saved': bool(saved), 'request_configured': len(request_key.strip()) >= 8,
            'process_configured': len(process) >= 8,
            'process_differs_from_saved': bool(process) and process != saved,
            'activation': 'host-scoped' if scope.current_secret_scope() is not None else 'process',
            'verified': False,
            'revision': _revision(path, raw)}


def _write(host, path, raw, action, key):
    parsed = _bindings(raw)
    # dotenv attaches preceding blank lines to the next binding. Preserve those
    # too; only the target assignment itself is removed.
    text = ''.join(b.original.string if b.key != KEY else
                   re.findall(r'\A(?:[ \t]*(?:\r\n|\n|\r))*', b.original.string)[0]
                   for b in parsed)
    if action == 'save':
        if text and not text.endswith('\n'):
            text += '\n'
        text += KEY + '=' + key + '\n'
    _bindings(text.encode('utf-8'))  # validate the candidate before any mutation
    # Stage Hermes' 0600 temp + fsync writer inside a checked private directory.
    # The shared .env.lock serializes publication with Hermes config writers.
    _path()
    if _read(path) != raw:
        raise HTTPException(409, 'Settings changed. Recheck before saving again.')
    _write_preserving_metadata(host, path, [text])
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


def _response(value, status_code=200):
    return JSONResponse(value, status_code=status_code, headers={'Cache-Control': 'no-store'})


def make_router(change_boundary=None):
    router = APIRouter()

    @router.get('/credentials/meta')
    def status(request: Request):
        _authorize(request)
        try:
            _host()
            path = _path()
            return _response(_status(path, _read(path)))
        except HostTokenizerUnavailable:
            raise HTTPException(503, TOKENIZER_UNAVAILABLE) from None
        except UnsupportedEnvFile:
            raise HTTPException(503, _UNSUPPORTED_FILE) from None
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
        if (action not in ('save', 'remove') or not isinstance(revision, str) or not _REVISION_SHAPE.fullmatch(revision)
                or (action == 'save' and (not isinstance(key, str) or not _KEY_SHAPE.fullmatch(key)))
                or (action == 'remove' and 'key' in body)):
            raise HTTPException(400, 'Enter an 8–4096 character API key without spaces, quotes or line breaks; recheck settings before saving.')
        transition = {}
        def failure(detail):
            if transition.get('overlay_stop_confirmed') is False:
                detail += ' overlay_shutdown_unconfirmed: Retry hide overlay and verify the window is gone before sharing.'
            return _response({'detail': detail, **transition}, 503)
        try:
            host = _host()
            host.require_env_writable(KEY, action)
            path = _path()
            _bindings(_read(path))  # refuse before even creating .env.lock
            # Blocking filesystem work runs off the ASGI event loop; the active
            # context propagates, and no key is passed to a subprocess/argv.
            import asyncio
            def apply():
                nonlocal transition
                with host._env_write_lock(path):
                    _path()
                    raw = _read(path)
                    if not hmac.compare_digest(revision, _revision(path, raw)):
                        raise HTTPException(409, 'Settings changed. Recheck before saving again.')
                    from contextlib import nullcontext
                    boundary = change_boundary() if change_boundary else nullcontext({})
                    with boundary as transition:
                        updated = _write(host, path, raw, action, key)
                        status = _status(path, updated)
                    return {**status, **transition}
            return _response(await asyncio.to_thread(apply))
        except HTTPException:
            raise
        except HostTokenizerUnavailable:
            return failure(TOKENIZER_UNAVAILABLE)
        except UnsupportedEnvFile:
            return failure(_UNSUPPORTED_FILE)
        except Exception:  # noqa: BLE001 -- credential boundary: sanitize all failures
            return failure(_UNAVAILABLE)
        finally:
            body.clear()
            key = None

    return router
