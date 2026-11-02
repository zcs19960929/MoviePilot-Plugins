"""Optional SDK cache isolation for upstream tests, including their child processes."""
import os
from pathlib import Path
import tempfile

_test_cache = os.environ.get("P115_TEST_SDK_CACHE")
if _test_cache:
    _expanduser = Path.expanduser

    def _isolated_expanduser(path):
        if str(path) == "~/.p115client.cache.d":
            target = Path(_test_cache).resolve()
            target.mkdir(parents=True, exist_ok=True)
            return target
        return _expanduser(path)

    Path.expanduser = _isolated_expanduser

_test_tmp = os.environ.get("P115_TEST_TMPDIR")
if _test_tmp:
    Path(_test_tmp).mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(Path(_test_tmp).resolve())

if os.environ.get("P115_TEST_HOST") == "1":
    import p115_test_host
    p115_test_host.install()
