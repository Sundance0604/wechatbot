"""Load the upstream database reader without importing its desktop automation UI."""
from functools import lru_cache
import importlib.util
from importlib.metadata import distribution
from pathlib import Path
import sys


@lru_cache(maxsize=1)
def database_reader():
    path=Path(__file__).with_name('vendor')/'db.py'
    if not path.is_file():
        package=distribution('wechatauto-replica')
        path=Path(package.locate_file('wechatauto/db.py'))
    spec=importlib.util.spec_from_file_location('violet._database_backend',path)
    if spec is None or spec.loader is None:raise RuntimeError('微信数据库读取组件缺失，请重新安装程序。')
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    try:spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name,None)
        raise
    return module
