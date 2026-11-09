"""Profile-owned, version-bound macOS artifacts for catalog installs."""
import hashlib
import os
from pathlib import Path
import re
import sys


_INPUTS = ('plugin.yaml', 'audio_worker.py', 'overlay_bridge.py',
           'native/macos_capture.swift', 'native/macos_overlay.swift',
           'requirements-macos-audio.txt', 'requirements-macos-audio.lock')
_SHA = re.compile(r'[0-9a-fA-F]{40}')


def effective_home():
    """Resolve on each request; never call private Hermes internals."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        override = os.environ.get('HERMES_HOME', '').strip()
        if override:
            return Path(os.path.expandvars(override)).expanduser().resolve()
        suffix = os.environ.get('HERMES_DATA_DIR_SUFFIX', '')
        if sys.platform == 'win32':
            local = os.environ.get('LOCALAPPDATA', '').strip()
            base = Path(local) if local else Path.home() / 'AppData/Local'
            return (base / ('hermes' + suffix)).resolve()
        return (Path.home() / ('.hermes' + suffix)).resolve()
    home = get_hermes_home()
    if not isinstance(home, (str, os.PathLike)) or not os.fspath(home):
        raise ValueError('Hermes home is unavailable')
    return Path(home).expanduser().resolve()


def is_catalog(root):
    # Legacy install.py puts the actual runtime in dashboard/core; its
    # plugin.yaml lives two levels above. Catalog modules live beside it.
    return (Path(root) / 'plugin.yaml').is_file()


def _git_revision(root):
    gitdir = root / '.git'
    if not gitdir.exists():
        # SOURCE_REVISION is the development-source commit, not necessarily
        # the catalog release pin. Without the installed pin, silently reusing
        # an artifact could cross a repin even when source bytes are identical.
        raise ValueError('Catalog checkout has no Git revision; reinstall through the catalog')
    if gitdir.is_file():
        marker = gitdir.read_text(encoding='utf-8').strip()
        if not marker.startswith('gitdir: '):
            raise ValueError('Invalid Git metadata')
        gitdir = (root / marker[8:]).resolve()
    head = (gitdir / 'HEAD').read_text(encoding='ascii').strip()
    if head.startswith('ref: '):
        ref = head[5:]
        if not re.fullmatch(r'refs/[A-Za-z0-9._/-]+', ref) or '..' in ref.split('/'):
            raise ValueError('Invalid Git ref')
        common = gitdir
        if (gitdir / 'commondir').is_file():
            common = (gitdir / (gitdir / 'commondir').read_text().strip()).resolve()
        ref_file = common / ref
        if ref_file.is_file():
            head = ref_file.read_text(encoding='ascii').strip()
        else:
            packed = (common / 'packed-refs').read_text(encoding='ascii').splitlines()
            head = next((line.split(' ', 1)[0] for line in packed if line.endswith(' ' + ref)), '')
    if not _SHA.fullmatch(head):
        raise ValueError('Cannot resolve installed Git revision')
    return head.lower().encode('ascii')


def artifact_dir(root, home=None):
    """Derive the same location for setup and runtime, without writing."""
    root = Path(root).resolve()
    if not is_catalog(root):
        return root  # preserve the legacy installer layout
    digest = hashlib.sha256(b'meeting-copilot-native-v1\0')
    digest.update(_git_revision(root) + b'\0')
    revision = root / 'SOURCE_REVISION'
    if revision.is_file():
        digest.update(revision.read_bytes())
    for name in _INPUTS:
        data = (root / name).read_bytes()
        digest.update(name.encode('utf-8') + b'\0' + len(data).to_bytes(8, 'big') + data)
    home = effective_home() if home is None else Path(home).expanduser().resolve()
    return home / 'plugin-data/meeting-copilot-live/macos' / digest.hexdigest()
