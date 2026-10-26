from pathlib import Path

import pytest

from qaforge.nocode.spec import all_tags

ROOT = Path(__file__).resolve().parents[2]


def pytest_configure(config: pytest.Config) -> None:
    # tags chosen in Studio become pytest markers (pytest -m smoke works for no-code tests too)
    for tag in sorted(all_tags(ROOT)):
        config.addinivalue_line("markers", f"{tag}: no-code tag")
