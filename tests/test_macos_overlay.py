"""Native overlay tests; AppKit diagnostics use only a synthetic hidden panel."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).parent.parent / 'native' / 'macos_overlay.swift'


@unittest.skipUnless(sys.platform == 'darwin', 'Swift AppKit target requires macOS')
class MacOverlayStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        swiftc = shutil.which('swiftc')
        if not swiftc:
            raise unittest.SkipTest('Apple Swift compiler is not installed')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.binary = str(Path(cls.tmp.name) / 'overlay')
        try:
            subprocess.run([swiftc, '-parse-as-library', '-O', str(SOURCE), '-o', cls.binary],
                           check=True, capture_output=True, timeout=120)
        except BaseException:
            cls.tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_compiled_overlay_state_and_protocol(self):
        result = subprocess.run([self.binary, '--self-test'], check=True, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.stdout.strip(), 'overlay state tests passed')
        version = subprocess.run([self.binary, '--protocol-version'], check=True, capture_output=True,
                                 text=True, timeout=5)
        self.assertEqual(version.stdout.strip(), 'meeting-overlay/2')
        import overlay_bridge
        self.assertTrue(overlay_bridge.OverlayBridge(helper=self.binary)._check_helper_protocol())

    @unittest.skipUnless(os.environ.get('MEETING_OVERLAY_APPKIT_TEST') == '1',
                         'opt in to the synthetic AppKit panel diagnostic')
    def test_presentation_diagnostic_exercises_real_panel(self):
        result = subprocess.run([self.binary, '--presentation-self-test'],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, f'stdout: {result.stdout}\nstderr: {result.stderr}')
        report = json.loads(result.stdout)
        self.assertEqual(report['type'], 'presentation_self_test')
        self.assertEqual(report['scope'], 'synthetic_appkit_panel')
        self.assertTrue(report['ok'])
        self.assertEqual(report['preset_count'], 6)
        for check in ('main_thread', 'default_click_through', 'recording_default_off',
                      'recording_modes', 'presets', 'restores_size_after_small_display',
                      'mounted_grip', 'grip_hit_test',
                      'non_grip_not_dragging', 'drag_moves_panel', 'manual_clamped',
                      'cross_screen_geometry', 'snapshot_keeps_placement',
                      'snapshot_keeps_recording_mode', 'interaction_poll_stable',
                      'temporary_key_capability', 'lost_mouse_up_releases_grip',
                      'interaction_timeout', 'drag_timeout'):
            with self.subTest(check=check):
                self.assertIs(report['checks'][check], True)


class OverlayProtocolTests(unittest.TestCase):
    """Source-level presentation and wire contracts; run on all platforms."""

    def test_overlay_background_is_translucent_without_dimming_text(self):
        source = SOURCE.read_text()
        self.assertIn('.background(Color.black.opacity(0.72), in: RoundedRectangle(cornerRadius: 14))', source)
        self.assertNotIn('NSVisualEffectView(', source, 'a material underlay obscures the desktop')
        self.assertNotIn('window.alphaValue', source, 'window opacity also fades text and controls')
        self.assertIn('window.backgroundColor = .clear', source)
        self.assertIn('.preferredColorScheme(.dark)', source)

    def test_snapshot_protocol_carries_streaming_identity_and_share_warning(self):
        source = SOURCE.read_text()
        for token in ('"completed_only"', 'share_warning', 'shareWarning', 'Streaming answer',
                      'ignoresMouseEvents = true', 'sharingType = .none'):
            self.assertIn(token, source)
        self.assertRegex(source, r'sharingType\s*=\s*\.none')

    def test_controls_explain_temporary_interaction_and_best_effort_exclusion(self):
        source = SOURCE.read_text()
        for label in ('Drag to move', '⌃⌥⌘O', 'Top left', 'Top center', 'Top right',
                      'Bottom left', 'Bottom center', 'Bottom right', 'Show in recordings',
                      'Exclusion is best effort', 'Hide it in the pane'):
            with self.subTest(label=label):
                self.assertIn(label, source)
        self.assertIn('.readOnly', source)
        self.assertIn('window.isMovableByWindowBackground = false', source)
        self.assertNotIn('Hidden from capture', source)
        self.assertNotIn('Private mode', source)

    def test_privacy_controls_are_pinned_above_scrollable_content(self):
        source = SOURCE.read_text().split('struct OverlayView: View {', 1)[1].split('// Route only', 1)[0]
        pinned, content = source.split('ScrollView {', 1)
        self.assertIn('Toggle("Show in recordings"', pinned)
        self.assertIn('Exclusion is best effort', pinned)
        self.assertNotIn('Toggle("Show in recordings"', content)
        self.assertLess(content.index('ForEach(model.state.questions)'),
                        content.index('ForEach(model.state.transcript)'),
                        'detected questions must not be pushed below the transcript')

    def test_overlay_never_initiates_answer_from_a_row(self):
        source = SOURCE.read_text()
        self.assertNotIn('Button(action: { model.click(', source)
        self.assertNotIn('"type": "click"', source)


if __name__ == '__main__':
    unittest.main()
