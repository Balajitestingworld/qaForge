import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/framework/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.unit)
