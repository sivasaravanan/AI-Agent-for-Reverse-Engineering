import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def make_zip(tmp_path):
    """make_zip({'a/b.py': 'text'}, name='x.zip') -> Path"""
    def _make(files, name="t.zip", compression=zipfile.ZIP_DEFLATED):
        p = tmp_path / name
        with zipfile.ZipFile(p, "w", compression) as zf:
            for n, c in files.items():
                zf.writestr(n, c)
        return p
    return _make
