"""Load only the upstream database module, without its GUI/sending package init."""
from functools import lru_cache
import importlib.util
from importlib.metadata import distribution
from pathlib import Path
import sys


@lru_cache(maxsize=1)
def get_upstream():
    bundled = Path(__file__).parent / "vendor/db.py"
    if bundled.is_file():
        path = bundled
    else:
        package = distribution("wechatauto-replica")
        if package.version != "1.2.1":
            raise RuntimeError("This release requires wechatauto-replica 1.2.1")
        path = package.locate_file("wechatauto/db.py")
    spec = importlib.util.spec_from_file_location("cleaner._reader_backend", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
