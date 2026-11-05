"""No asynchronous editor publications; admitted workers keep scoped snapshots.

Replaces races between plugin file writes and overlay publication, since the
editor route no longer exists. Exercise delayed context use without secret files.
"""
import contextvars
import threading
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import credentials

class DelayedCredentialPublicationTests(unittest.TestCase):
    def test_rejected_edit_cannot_change_delayed_admitted_context(self):
        marker=contextvars.ContextVar('synthetic_key')
        token=marker.set('synthetic-original')
        captured=contextvars.copy_context()
        gate,release=threading.Event(),threading.Event()
        values=[]
        def delayed():
            gate.set()
            if release.wait(2):values.append(marker.get())
        worker=threading.Thread(target=lambda:captured.run(delayed))
        app=FastAPI();app.include_router(credentials.make_router(lambda:self.fail('mutation boundary forbidden')))
        try:
            worker.start();self.assertTrue(gate.wait(1))
            with TestClient(app) as client:
                self.assertEqual(client.post('/credentials/meta',json={'action':'save','key':'synthetic-new'}).status_code,405)
            marker.set('synthetic-future')
            release.set();worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(values,['synthetic-original'])
            self.assertEqual(marker.get(),'synthetic-future')
        finally:
            release.set();worker.join(2);marker.reset(token)
    def test_rejected_remove_never_retires_overlay_or_changes_process(self):
        callback=unittest.mock.Mock()
        app=FastAPI();app.include_router(credentials.make_router(callback))
        with TestClient(app) as client:
            self.assertEqual(client.post('/credentials/meta',json={'action':'remove'}).status_code,405)
        callback.assert_not_called()
