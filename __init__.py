"""Register the supplied host facade; importing never starts capture or a model call."""
import importlib.util
from importlib import import_module
from pathlib import Path


def register(ctx):
    root = Path(__file__).resolve().parent
    if (root / 'dashboard' / 'core' / 'runtime_loader.py').is_file():
        root = root / 'dashboard' / 'core'
    spec = importlib.util.spec_from_file_location('_meeting_loader', root / 'runtime_loader.py')
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    import_module(loader.runtime_name(root) + '.host_default').register(ctx)
