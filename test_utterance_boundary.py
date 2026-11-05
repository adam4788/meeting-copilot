import unittest
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
