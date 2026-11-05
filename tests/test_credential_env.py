"""Differential startup parsing against the installed host; synthetic data only."""
import importlib.util
import unittest

from credential_env import UnsupportedEnvFile, bindings, startup_value

# Every fixture ends with a good value: final-map equality alone misses attacks.
HOSTILE_TARGETS = [
    b'OTHER="first\nMODEL_API_KEY=fake-hidden\nlast"\n',
    b"OTHER='first\nexport MODEL_API_KEY=fake-hidden\nlast'\n",
    b"'MODEL_API_KEY'=fake-hidden\n",
    b'export\tMODEL_API_KEY=fake-hidden\n',
    b'MODEL_API_KEY="first\nlast"\n',
    b'MODEL_API_KEY="fake-escaped\\nvalue"\n',
    b'MODEL_API_KEY\n',
    b"'MODEL_API_KEY'=\"first\nlast\"\n",
    # Same-valued hidden assignment still counts; values alone cannot prove origin.
    b'OTHER="first\nMODEL_API_KEY=fake-final-key\nlast"\n',
]


@unittest.skipUnless(importlib.util.find_spec('agent'), 'requires installed Hermes tokenizer')
class StartupParserTests(unittest.TestCase):
    def setUp(self):
        from agent.secret_scope import _parse_env_text
        self.host = _parse_env_text

    def test_global_target_occurrences_not_only_final_map(self):
        for prefix in HOSTILE_TARGETS:
            raw = prefix + b'MODEL_API_KEY=fake-final-key\n'
            with self.subTest(case=HOSTILE_TARGETS.index(prefix)):
                self.assertEqual(self.host(raw.decode())['MODEL_API_KEY'], 'fake-final-key')
                with self.assertRaises(UnsupportedEnvFile):
                    startup_value(raw, self.host)
                with self.assertRaises(UnsupportedEnvFile):
                    bindings(raw, self.host)

    def test_benign_other_syntax_duplicates_and_literal_expansion(self):
        for other in (b'OTHER="first\nsecond"\n', b'OTHER="escaped\\nnewline"\n',
                      b"'OTHER'=value\n", b'NORMAL=value\n', b'# comment\n\n'):
            for ending in (b'\n', b'\r\n', b'\r'):
                raw = other + ending + b' export MODEL_API_KEY="fake-first"' + ending
                raw += b'  MODEL_API_KEY=${LITERAL}' + ending
                self.assertEqual(startup_value(raw, self.host), '${LITERAL}')

    def test_full_host_result_is_also_checked(self):
        real = self.host
        def contextual(text):
            result = real(text)
            if text.count('\n') > 1:
                result['MODEL_API_KEY'] = 'fake-different'
            return result
        with self.assertRaises(UnsupportedEnvFile):
            startup_value(b'OTHER=value\nMODEL_API_KEY=fake-final-key\n', contextual)
