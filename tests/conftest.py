import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def make_jar(tmp_path: Path):
    """Build a zip/jar at tmp_path/relname with {entry_name: content} members."""

    def _make(relname: str, entries: dict[str, bytes | str], base: Path | None = None) -> Path:
        path = (base or tmp_path) / relname
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as zf:
            for name, content in entries.items():
                zf.writestr(name, content)
        return path

    return _make
