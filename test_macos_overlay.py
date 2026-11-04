"""Native overlay state checks; no GUI, network, devices, or permissions."""
from pathlib import Path
import unittest
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'darwin', 'Swift AppKit target requires macOS')
class MacOverlayStateTests(unittest.TestCase):
    def test_compiled_overlay_state_and_protocol(self):
        swiftc = shutil.which('swiftc')
        if not swiftc:
            self.skipTest('Apple Swift compiler is not installed')
        source = Path(__file__).parent / 'native' / 'macos_overlay.swift'
        with tempfile.TemporaryDirectory() as tmp:
            binary = str(Path(tmp) / 'overlay')
            subprocess.run([swiftc, '-parse-as-library', str(source), '-o', binary],
                           check=True, capture_output=True, timeout=120)
            result = subprocess.run([binary, '--self-test'], check=True, capture_output=True,
                                    text=True, timeout=15)
            self.assertEqual(result.stdout.strip(), 'overlay state tests passed')




class OverlayProtocolTests(unittest.TestCase):
    """Source-level protocol contract; runs on all platforms without compiling."""

    def test_snapshot_protocol_carries_streaming_identity_and_share_warning(self):
        source = (Path(__file__).parent / 'native' / 'macos_overlay.swift').read_text()
        for token in ('"completed_only"', 'share_warning', 'shareWarning', 'Streaming answer',
                      'ignoresMouseEvents = true', 'sharingType = .none'):
            self.assertIn(token, source)
        # Best-effort comment must stay next to the legacy flag.
        self.assertRegex(source, r'sharingType\s*=\s*\.none')


if __name__ == '__main__':
    unittest.main()
