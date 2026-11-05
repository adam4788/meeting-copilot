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
        source = Path(__file__).parent.parent / 'native' / 'macos_overlay.swift'
        with tempfile.TemporaryDirectory() as tmp:
            binary = str(Path(tmp) / 'overlay')
            subprocess.run([swiftc, '-parse-as-library', str(source), '-o', binary],
                           check=True, capture_output=True, timeout=120)
            result = subprocess.run([binary, '--self-test'], check=True, capture_output=True,
                                    text=True, timeout=15)
            self.assertEqual(result.stdout.strip(), 'overlay state tests passed')
            version = subprocess.run([binary, '--protocol-version'], check=True, capture_output=True,
                                     text=True, timeout=5)
            self.assertEqual(version.stdout.strip(), 'meeting-overlay/2')
            import overlay_bridge
            self.assertTrue(overlay_bridge.OverlayBridge(helper=binary)._check_helper_protocol())




class OverlayProtocolTests(unittest.TestCase):
    """Source-level protocol contract; runs on all platforms without compiling."""

    def test_overlay_background_is_translucent_without_dimming_text(self):
        source = (Path(__file__).parent.parent / 'native' / 'macos_overlay.swift').read_text()
        self.assertIn('.background(Color.black.opacity(0.72), in: RoundedRectangle(cornerRadius: 14))', source)
        self.assertNotIn('NSVisualEffectView(', source, 'a material underlay obscures the desktop')
        self.assertNotIn('window.alphaValue', source, 'window opacity also fades text and controls')
        self.assertIn('window.backgroundColor = .clear', source)
        self.assertIn('.preferredColorScheme(.dark)', source)

    def test_snapshot_protocol_carries_streaming_identity_and_share_warning(self):
        source = (Path(__file__).parent.parent / 'native' / 'macos_overlay.swift').read_text()
        for token in ('"completed_only"', 'share_warning', 'shareWarning', 'Streaming answer',
                      'ignoresMouseEvents = true', 'sharingType = .none'):
            self.assertIn(token, source)
        # Best-effort comment must stay next to the legacy flag.
        self.assertRegex(source, r'sharingType\s*=\s*\.none')


if __name__ == '__main__':
    unittest.main()
