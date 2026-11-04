"""Catalog-loadable dashboard API: mount server.router from this repo tree."""
import hashlib
import importlib.util
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
name = '_meeting_copilot_repo_' + hashlib.sha256(str(root).encode()).hexdigest()[:20]
if name not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        name, root / '__init__.py', submodule_search_locations=[str(root)])
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
from importlib import import_module
router = import_module(name + '.server').router
