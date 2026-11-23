import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import fake_console  # noqa: E402

EXAMPLE = str(pathlib.Path(__file__).parent.parent / "examples" / "demo-console.toml")


@pytest.fixture(scope="session")
def console_server():
    server, state = fake_console.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()


@pytest.fixture
def console(console_server):
    url, state = console_server
    state.reset()
    return url, state
