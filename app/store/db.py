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

def _run_script(conn: sqlite3.Connection, script: str) -> None:
    """Apply a migration statement by statement.

    DDL is otherwise idempotent (CREATE TABLE/INDEX IF NOT EXISTS); the only
    non-idempotent form is ``ALTER TABLE ... ADD COLUMN`` (SQLite has no
    ``IF NOT EXISTS`` for columns), whose "duplicate column name" error on a
    re-run means the column is already present and is safely ignored, keeping
    migrations reentrant.
    """
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            try:
                conn.execute(statement.strip())
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error):
                    raise
            statement = ""

def migrate() -> None:
    conn = connect()
    try:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            _run_script(conn, path.read_text(encoding="utf-8"))
    finally:
        conn.close()
