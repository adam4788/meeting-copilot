"""Source-only prototype: immutable, bounded provider transcript provenance.

Not wired into runtime yet. A caller must send provider_json unchanged and retain
this snapshot with that answer; never resolve refs against the live buffer.
"""
from dataclasses import dataclass
from collections import deque
import hashlib
import json
import math
import uuid


@dataclass(frozen=True)
class Segment:
    ref: str
    speaker: str
    text: str
    source_id: str | None
    timestamp: float | None
    timestamp_kind: str
    shortened: bool

    def payload(self):
        return dict(ref=self.ref, speaker=self.speaker, text=self.text,
                    source_id=self.source_id, timestamp=self.timestamp,
                    timestamp_kind=self.timestamp_kind, shortened=self.shortened)


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    segments: tuple[Segment, ...]
    provider_json: str
    sha256: str
    omitted_segments: int

    def resolve(self, snapshot_id, ref):
        """Scope references to this answer; never resolve another snapshot's T1."""
        if snapshot_id != self.snapshot_id:
            return None
        return next((s for s in self.segments if s.ref == ref), None)


def build_snapshot(segments, *, backend, clean, max_chars=6000):
    """Sanitize first, then budget the exact serialized provider representation.

    Caller must provide the production sanitizer and trusted backend selection.
    Native timestamps are chunk starts, not exact utterance timings. Meetily's
    audio_start_time is kept uninterpreted; it is not converted into UTC or a
    playback offset. Backend IDs are provenance, not globally stable citations.
    """
    if type(max_chars) is not int or not 512 <= max_chars <= 16000:
        raise ValueError('max_chars must be between 512 and 16000')
    recent = deque(maxlen=40)
    total = 0
    for segment in segments:
        recent.append(segment)
        total += 1
    source = list(recent)
    # Fresh identity also distinguishes identical text in separate meetings.
    identity = uuid.uuid4().hex
    selected = []
    def encode(rows):
        return json.dumps({'snapshot_id': identity,
                           'scope': 'recent available transcript; not a complete meeting',
                           'segments': [s.payload() for s in rows]},
                          ensure_ascii=False, separators=(',', ':'))
    for raw in reversed(source):
        if not isinstance(raw, dict) or not isinstance(raw.get('text'), str):
            continue
        text = str(clean(raw['text'][:4000])).strip()
        if not text:
            continue
        speaker = str(clean(str(raw.get('speaker', 'unknown'))[:30]))[:100]
        source_id = raw.get('id')
        source_id = str(clean(str(source_id)[:600]))[:600] if source_id is not None else None
        stamp = raw.get('timestamp')
        valid = (type(stamp) in (int, float) and 0 <= stamp <= 253402300799
                 and math.isfinite(stamp))
        kind = ('capture_chunk_start_unix_seconds' if backend in ('native', 'macos')
                else 'meetily_audio_start_time_uninterpreted' if backend == 'meetily' else 'unknown')
        stamp = float(stamp) if valid and kind != 'unknown' else None
        if stamp is None:
            kind = 'unavailable'
        # References are allocated newest-first and stay attached to this copy.
        ref = 'T' + str(len(selected) + 1)
        def row(value, shortened=False):
            return Segment(ref, speaker, value, source_id, stamp, kind, shortened)
        candidate = row(text, len(raw['text']) > 4000)
        if len(encode([candidate] + selected)) > max_chars:
            if selected:
                break  # preserve contiguous recent context, do not cherry-pick
            # A single newest long segment can be shortened, explicitly labeled.
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len(encode([row(text[:mid], True)])) <= max_chars:
                    lo = mid
                else:
                    hi = mid - 1
            if not lo:
                break
            candidate = row(text[:lo], True)
        selected.insert(0, candidate)
    rows = tuple(selected)
    payload = encode(rows)
    assert len(payload) <= max_chars
    return Snapshot(identity, rows, payload, hashlib.sha256(payload.encode()).hexdigest(),
                    max(0, total - len(rows)))
