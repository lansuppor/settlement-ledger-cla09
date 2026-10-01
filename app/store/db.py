import sqlite3
from pathlib import Path

from app.config import db_path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

def connect(timeout: float = 5.0) -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def migrate() -> None:
    conn = connect()
    try:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    finally:
        conn.close()
