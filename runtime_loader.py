"""Share this plugin's own runtime between the two host entrypoints."""
import hashlib
import importlib.util
import sys
import types
from pathlib import Path


def runtime_name(root):
    root = Path(root).resolve()
    digest = hashlib.sha256(str(root).encode())
    for path in sorted(root.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    name = '_meeting_copilot_repo_' + digest.hexdigest()[:20]
    package = types.ModuleType(name)
    package.__path__ = [str(root)]
    package.__file__ = str(root / '__init__.py')
    package.__package__ = name
    package.__spec__ = importlib.util.spec_from_file_location(
        name, root / '__init__.py', submodule_search_locations=[str(root)])
    sys.modules.setdefault(name, package)
    return name
