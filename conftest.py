from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Give pytest imports a writable session database before app.config is imported.
_SESSION_DB = Path(tempfile.mkdtemp(prefix="sortbay-root-pytest-")) / "session.db"
os.environ.setdefault("SORTBAY_DB", str(_SESSION_DB))
