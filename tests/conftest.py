"""Suite-level fixtures. The demo bank starts automatically for --env local runs."""
from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlparse

import pytest

from qaforge.config.settings import Settings


@pytest.fixture(scope="session", autouse=True)
def demo_bank(settings: Settings) -> Iterator[None]:
    if settings.env != "local":
        yield
        return
    try:
        from demo_app.server import start_in_thread  # optional, not committed to git
    except ImportError:
        yield
        return

    import socket

    target = urlparse(settings.app("retail").base_url)
    host, port = target.hostname or "127.0.0.1", target.port or 8765
    with socket.socket() as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((host, port)) == 0:  # already running (e.g. started for Studio)
            yield
            return
    server = start_in_thread(host, port)
    yield
    server.shutdown()
