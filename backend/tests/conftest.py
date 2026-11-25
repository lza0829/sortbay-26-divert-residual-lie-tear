from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

# Give imports a writable session database; each test repoints app.db.DB_PATH
# at its own tmp_path database.
_SESSION_DB = Path(tempfile.mkdtemp(prefix="sortbay-pytest-")) / "session.db"
os.environ.setdefault("SORTBAY_DB", str(_SESSION_DB))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "sortbay-test.db"
    monkeypatch.setattr("app.db.DB_PATH", path)
    init_db()
    seed_if_empty()
    return path


@pytest.fixture()
def client(db_path):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def server_url(db_path):
    """Run a real uvicorn socket for true multi-thread collision tests."""
    import socket
    import threading

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            import httpx

            with httpx.Client(trust_env=False, timeout=0.2) as http_client:
                response = http_client.get(f"http://127.0.0.1:{port}/api/健康")
            if response.status_code == 200:
                break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("test API server did not start")

    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
