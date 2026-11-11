"""Mount the same plugin-owned runtime that receives register(ctx)."""
import importlib.util
from importlib import import_module
from pathlib import Path

root = Path(__file__).resolve().parent
root = root / 'core' if (root / 'core' / 'runtime_loader.py').is_file() else root.parent
spec = importlib.util.spec_from_file_location('_meeting_loader', root / 'runtime_loader.py')
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
router = import_module(loader.runtime_name(root) + '.server').router
