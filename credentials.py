"""Credential setup guidance only. Secrets are managed exclusively by Hermes."""
from fastapi import APIRouter

def make_router(on_change=None):
    router=APIRouter()
    @router.get('/credentials/meta')
    def setup():
        return {'configured': False, 'editable': False, 'message': 'Use Hermes Settings → Plugins → Meeting Copilot for API keys. Never paste keys into chat or model fields.'}
    return router
