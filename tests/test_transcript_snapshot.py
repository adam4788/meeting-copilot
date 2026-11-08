import dataclasses
import json
import unittest
from transcript_snapshot import build_snapshot


class SnapshotTests(unittest.TestCase):
    def make(self, rows, **kw):
        return build_snapshot(rows, backend=kw.pop('backend', 'native'), clean=kw.pop('clean', lambda x: x), **kw)

    def test_immutable_under_correction_and_live_buffer_edits(self):
        row = dict(id='meetily:fictional:1', speaker='meeting', text='Draft', timestamp=10)
        first = self.make([row], backend='meetily')
        wire = first.provider_json
        row['text'] = 'Corrected'
        second = self.make([row], backend='meetily')
        self.assertEqual(first.provider_json, wire)
        self.assertEqual(first.segments[0].text, 'Draft')
        self.assertEqual(second.segments[0].text, 'Corrected')
        self.assertEqual(first.segments[0].source_id, second.segments[0].source_id)
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.segments[0].text = 'changed'

    def test_reference_isolation_for_identical_new_sessions(self):
        rows = [dict(text='Same words', timestamp=100)]
        a, b = self.make(rows), self.make(rows)
        self.assertIsNone(a.resolve(b.snapshot_id, 'T1'))
        self.assertIs(a.resolve(a.snapshot_id, 'T1'), a.segments[0])
        self.assertIsNone(a.resolve(a.snapshot_id, 'T99'))
        self.assertIsNone(a.segments[0].source_id)

    def test_timestamp_semantics_never_invent_wall_time_for_meetily(self):
        for backend, kind in [('native', 'capture_chunk_start_unix_seconds'), ('macos', 'capture_chunk_start_unix_seconds'), ('meetily', 'meetily_audio_start_time_uninterpreted')]:
            s = self.make([dict(text='Words', timestamp=12.5)], backend=backend).segments[0]
            self.assertEqual(s.timestamp, 12.5)
            self.assertEqual(s.timestamp_kind, kind)
        for stamp in [None, True, float('nan'), float('inf'), -1, 10**1000, '12.5']:
            s = self.make([dict(text='Words', timestamp=stamp)]).segments[0]
            self.assertIsNone(s.timestamp)
        self.assertIsNone(self.make([dict(text='Words', timestamp=12)], backend='unknown').segments[0].timestamp)

    def test_exact_wire_budget_and_no_partial_older_segments(self):
        snap = self.make([dict(text='older ' * 100), dict(text='newer ' * 100)], max_chars=512)
        self.assertLessEqual(len(snap.provider_json), 512)
        wire = json.loads(snap.provider_json)
        self.assertEqual(wire['segments'], [s.payload() for s in snap.segments])
        self.assertEqual(len(snap.segments), 1)
        self.assertTrue(snap.segments[0].shortened)
        self.assertEqual(snap.omitted_segments, 1)
        self.assertNotIn('older', snap.provider_json)

    def test_sanitizer_precedes_budget_and_snapshot_never_retains_raw(self):
        secret = 'synthetic-secret-value'
        snap = self.make([dict(text='Say '+secret, speaker=secret, id=secret)], clean=lambda x: x.replace(secret, '[REDACTED]'))
        self.assertNotIn(secret, repr(snap))
        self.assertIn('[REDACTED]', snap.provider_json)

    def test_complete_segments_exact_characters_and_recent_window(self):
        rows = [dict(text='Line '+str(i)+'\nquote " and Unicode 日本語', timestamp=i) for i in range(60)]
        snap = self.make(rows, max_chars=16000)
        self.assertEqual(len(snap.segments), 40)
        self.assertEqual(snap.omitted_segments, 20)
        self.assertEqual(snap.segments[0].text, rows[20]['text'])
        self.assertEqual(snap.segments[-1].text, rows[-1]['text'])
        self.assertTrue(all(not s.shortened for s in snap.segments))

    def test_invalid_budget_and_metadata_only_row_are_bounded(self):
        for limit in [0, True, 16001]:
            with self.assertRaises(ValueError):
                self.make([], max_chars=limit)
        snap = self.make([dict(text='x', id='y'*600)], max_chars=512)
        self.assertEqual(snap.segments, ())
        self.assertLessEqual(len(snap.provider_json), 512)

    def test_production_sanitizer_removes_credential_shapes_before_snapshot(self):
        from knowledge import _clean
        snap = self.make([dict(text='Password: "synthetic credential". Decision: review Friday.',
                               id='api_key=synthetic-value')], clean=_clean)
        self.assertNotIn('synthetic credential', repr(snap))
        self.assertNotIn('synthetic-value', repr(snap))
        self.assertIn('review Friday', snap.provider_json)