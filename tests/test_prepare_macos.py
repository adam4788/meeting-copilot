"""Preparation is installation/build only, never a device start."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import prepare_macos


class PreparationTests(unittest.TestCase):
    def test_preparation_compiles_usage_metadata_and_never_starts_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = Path(tmp) / 'plugins/meeting-copilot-live/dashboard/core'
            (core / 'native').mkdir(parents=True)
            for name in ('audio_worker.py', 'overlay_bridge.py', 'native/macos_capture.swift',
                         'native/macos_overlay.swift', 'requirements-macos-audio.txt',
                         'requirements-macos-audio.lock'):
                (core / name).write_text('synthetic')
            calls, call_envs = [], []
            def run(argv, **kwargs):
                self.assertTrue(kwargs['check'])
                calls.append(argv)
                call_envs.append(kwargs.get('env'))
            with patch.dict(os.environ, {'UV_EXTRA_INDEX_URL': 'https://example.invalid',
                                        'PIP_INDEX_URL': 'https://example.invalid', 'PYTHONPATH': '/bad'}), \
                 patch('prepare_macos.platform.machine', return_value='arm64'), \
                 patch('prepare_macos.sys.platform', 'darwin'), patch('prepare_macos.shutil.which', side_effect=lambda x: '/tools/' + x):
                result = prepare_macos.prepare(tmp, run=run)
            self.assertFalse(result['capture_started'])
            self.assertTrue(result['overlay'].endswith('macos_overlay'))
            compilers = [argv for argv in calls if argv[0].endswith('swiftc')]
            self.assertEqual(len(compilers), 2)
            capture_compile = next(argv for argv in compilers if any('macos_capture.swift' in a for a in argv))
            self.assertIn('-parse-as-library', capture_compile)
            self.assertIn('__info_plist', capture_compile)
            overlay_compile = next(argv for argv in compilers if any('macos_overlay.swift' in a for a in argv))
            self.assertIn('-parse-as-library', overlay_compile)
            self.assertTrue((core / 'native/CaptureInfo.plist').is_file())
            venv = next(argv for argv in calls if 'venv' in argv)
            self.assertIn('--no-python-downloads', venv, 'setup must not fetch an unpinned interpreter')
            self.assertIn('--no-config', venv)
            install = next(argv for argv in calls if 'pip' in argv)
            for option in ('--no-config', '--require-hashes', '--only-binary', ':all:',
                           '--default-index', 'https://pypi.org/simple'):
                self.assertIn(option, install)
            self.assertEqual(install[install.index('-r') + 1], str(core / 'requirements-macos-audio.lock'))
            for argv, env in zip(calls, call_envs):
                if argv[0] == '/tools/uv':
                    self.assertNotIn('UV_EXTRA_INDEX_URL', env)
                    self.assertNotIn('PIP_INDEX_URL', env)
                    self.assertNotIn('PYTHONPATH', env)
            self.assertFalse(any('--start' in argv for argv in calls))
            self.assertFalse(any('audio_worker.py' in argv for argv in calls))

    def test_missing_install_fails_before_commands(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp, patch('prepare_macos.platform.machine', return_value='arm64'), patch('prepare_macos.sys.platform', 'darwin'), patch('prepare_macos.shutil.which', return_value='/tools/tool'):
            with self.assertRaises(FileNotFoundError):
                prepare_macos.prepare(tmp, run=lambda *args, **kwargs: calls.append(args))
        self.assertEqual(calls, [])

        with patch('prepare_macos.sys.platform', 'darwin'), patch('prepare_macos.platform.machine', return_value='x86_64'):
            with self.assertRaisesRegex(RuntimeError, 'Apple Silicon'):
                prepare_macos.prepare('/unused', run=lambda *a, **kw: self.fail('must not run'))

    def test_cli_does_not_create_bytecode_in_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = Path(prepare_macos.__file__).resolve().parent
            for name in ('prepare_macos.py', 'native_runtime.py'):
                shutil.copyfile(source / name, root / name)
            env = {k: v for k, v in os.environ.items()
                   if k not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONDONTWRITEBYTECODE')}
            subprocess.run([sys.executable, str(root / 'prepare_macos.py'), '--help'],
                           check=True, env=env, capture_output=True)
            self.assertEqual({p.name for p in root.iterdir()}, {'prepare_macos.py', 'native_runtime.py'})

    def test_catalog_setup_leaves_checkout_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / 'plugins/meeting-copilot-live'
            (plugin / 'native').mkdir(parents=True)
            for name in ('plugin.yaml', 'audio_worker.py', 'overlay_bridge.py',
                         'native/macos_capture.swift', 'native/macos_overlay.swift',
                         'requirements-macos-audio.txt', 'requirements-macos-audio.lock'):
                (plugin / name).write_text('synthetic')
            (plugin / '.git').mkdir()
            (plugin / '.git/HEAD').write_text('a' * 40 + '\n')
            # A stale legacy core carried into a catalog checkout must not win.
            stale = plugin / 'dashboard/core/audio_worker.py'
            stale.parent.mkdir(parents=True)
            stale.write_text('stale legacy worker')
            def snapshot():
                return {str(p.relative_to(plugin)):
                        ('link', os.readlink(p)) if p.is_symlink() else
                        ('file', p.read_bytes()) if p.is_file() else ('directory', None)
                        for p in plugin.rglob('*')}
            before = snapshot()
            def run(argv, **kwargs):
                if 'venv' in argv:
                    python = Path(argv[-1]) / 'bin/python'
                    python.parent.mkdir(parents=True, exist_ok=True)
                    python.symlink_to('/synthetic-python')
                if '-o' in argv:
                    Path(argv[argv.index('-o') + 1]).touch()
            with patch('prepare_macos.platform.machine', return_value='arm64'), patch('prepare_macos.sys.platform', 'darwin'), patch('prepare_macos.shutil.which', return_value='/tools/tool'):
                result = prepare_macos.prepare(tmp, run=run)
            after = snapshot()
            self.assertEqual(before, after, 'setup modified the catalog checkout')
            self.assertFalse(any(p.is_symlink() for p in plugin.rglob('*')))
            for name in ('runtime', 'helper', 'overlay'):
                self.assertNotIn(plugin, Path(result[name]).parents)
            self.assertFalse(result['capture_started'])
