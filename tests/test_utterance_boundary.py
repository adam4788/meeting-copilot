import unittest
from unittest.mock import patch
import numpy as np
from audio_worker import utterance_boundary

class BoundaryTests(unittest.TestCase):
    def test_speech_crosses_old_four_second_boundary(self):
        self.assertFalse(utterance_boundary([np.ones(64000,dtype=np.float32)*.1],64000))
        self.assertFalse(utterance_boundary([np.ones(128000,dtype=np.float32)*.1],128000))
    def test_silence_flushes_with_full_speech_context(self):
        speech=np.ones(80000,dtype=np.float32)*.1
        pause=np.zeros(6400,dtype=np.float32)
        self.assertTrue(utterance_boundary([speech,pause],86400))
        self.assertFalse(utterance_boundary([speech,pause[:1000]],81000))
    def test_latency_and_memory_bounded_for_noise(self):
        self.assertTrue(utterance_boundary([np.ones(192000,dtype=np.float32)],192000))
        self.assertFalse(utterance_boundary([np.zeros(32000,dtype=np.float32)],32000))

    def test_microphone_frames_inspect_only_trailing_window(self):
        # A long utterance split at the observed device's approximate cadence.
        frames=[np.full(171,.1,dtype=np.float32) for _ in range(1100)]
        original=np.concatenate
        copied=[]
        def concatenate(parts):
            copied.append(sum(len(p) for p in parts))
            return original(parts)
        with patch.object(np,'concatenate',side_effect=concatenate):
            self.assertFalse(utterance_boundary(frames,171*1100))
        self.assertEqual(copied,[6400])

    def test_arbitrary_frame_partitions_preserve_energy_decision(self):
        rng=np.random.default_rng(27)
        for level in (0.,.004,.008,.02):
            audio=np.concatenate([rng.normal(0,.2,80000),np.full(6400,level)]).astype(np.float32)
            expected=float(np.sqrt(np.mean(audio[-6400:].astype(np.float64)**2)))<.008
            for size in (1,171,512,8000,86400):
                frames=[audio[i:i+size] for i in range(0,len(audio),size)]
                self.assertEqual(utterance_boundary(frames,len(audio)),expected)
