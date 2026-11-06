"""Synthetic localhost vendor wire tests; no real credentials or vendor calls."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import byok
from answer_core import AnswerCore, NoHistory, DisabledSearch
from pathlib import Path
import tempfile

class WireTests(unittest.TestCase):
    def setUp(self):
        self.requests=[]
        self.status=200
        self.payload={'choices':[{'finish_reason':'stop','message':{'content':'Synthetic answer [1]'}}]}
        owner=self
        class Vendor(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(owner.status)
                self.send_header('Content-Type','application/json')
                if owner.status==302:self.send_header('Location',owner.endpoint+'/redirected')
                self.end_headers()
                self.wfile.write(json.dumps(owner.payload).encode())
            def log_message(self,*args): pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Vendor)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.endpoint=f'http://127.0.0.1:{self.server.server_port}/chat/completions'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def route(self, provider='openai'):
        # Only the test substitutes a localhost endpoint; no API accepts origins.
        with patch.dict(byok.PROVIDERS,{provider:('Synthetic vendor','TEST_KEY',self.endpoint)}), patch.object(byok,'secret',return_value='synthetic-key'):
            return byok.completion(provider,'synthetic/model-1')
    def test_wire_exact_model_header_and_single_request(self):
        for provider in ('openai','openrouter'):
            with self.subTest(provider=provider):
                self.assertEqual(self.route(provider)([{'role':'user','content':'synthetic question'}]),'Synthetic answer [1]')
                path,headers,body=self.requests[-1]
                self.assertEqual(path,'/chat/completions')
                self.assertEqual(headers['Authorization'],'Bearer synthetic-key')
                self.assertEqual(body['model'],'synthetic/model-1')
                self.assertNotIn('api_key',body)
                self.assertNotIn('tools',body)
        self.assertEqual(len(self.requests),2)
    def test_http_error_empty_and_truncated_never_retry(self):
        for status,payload in [(401,{'error':'synthetic-key'}),(200,{'choices':[{'finish_reason':'length','message':{'content':'partial'}}]}),(200,{'choices':[{'finish_reason':'stop','message':{'content':''}}]})]:
            self.status,self.payload=status,payload
            before=len(self.requests)
            with self.assertRaises(RuntimeError) as raised:self.route()([])
            self.assertNotIn('synthetic-key',str(raised.exception))
            self.assertEqual(len(self.requests),before+1)
    def test_redirects_never_forward_authorization_or_retry(self):
        self.status=302
        with self.assertRaisesRegex(RuntimeError,'HTTP 302'):self.route()([])
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.requests[0][0],'/chat/completions')
    def test_grounded_answer_core_reaches_wire(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp)/'Synthetic';project.mkdir();(project/'notes.md').write_text('Synthetic launch budget is 42 units.')
            core=AnswerCore(project_root=tmp, completion=self.route(), search=DisabledSearch(), history=NoHistory())
            result=core.answer('What is the synthetic launch budget?',project='Synthetic')
            self.assertIn('Synthetic answer',result['answer'])
            self.assertTrue(result['sources'])
            wire=json.dumps(self.requests[0][2])
            self.assertIn('42 units',wire)
    def test_host_missing_scope_never_borrows_environment(self):
        import sys,types
        scope=types.SimpleNamespace(current_secret_scope=lambda:None,get_secret=lambda *a: self.fail('must not read'))
        with patch.dict(sys.modules,{'agent':types.SimpleNamespace(secret_scope=scope)}),patch.dict('os.environ',{'OPENAI_API_KEY':'synthetic-key'}):
            self.assertEqual(byok.secret('OPENAI_API_KEY'),'')
            self.assertEqual(byok.secret('OPENAI_API_KEY',standalone=True),'')
    def test_invalid_routes_rejected_before_network(self):
        for provider,model in [('auto','model'),('openai','https://bad'),('openai','sk-secret'),('openrouter','auto'),('openai',None)]:
            with self.assertRaises(ValueError):byok.completion(provider,model)
        self.assertFalse(self.requests)

if __name__=='__main__':unittest.main()
