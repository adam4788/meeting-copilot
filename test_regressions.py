import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from knowledge import KnowledgeEngine


class RetrievalRegressionTests(TestCase):
    def test_latest_transcript_question_drives_lookup_when_assist_prompt_is_generic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'demo'
            project.mkdir()
            (project / 'README.md').write_text('The Quasar rollout requires two independent reviewer approvals.', encoding='utf-8')
            engine = KnowledgeEngine(project_root=root, hermes_home=root / 'profile')
            with patch.object(engine, '_complete', return_value='Two reviewers must approve the Quasar rollout [1].'), patch.object(engine, 'search', wraps=engine.search) as lookup:
                result = engine.answer('Suggest a concise answer to the most recent question in this transcript.',
                                       transcript='meeting: What approval does the Quasar rollout need?', project='demo')
            self.assertIn('Quasar', lookup.call_args.args[0])
            self.assertTrue(result['sources'])
            self.assertIn('Quasar rollout', result['sources'][0]['text'])
