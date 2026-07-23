from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data" / "hub")))
DB_PATH = DATA_DIR / "tasks.db"

if DATABASE_URL:
    import psycopg
    from psycopg import errors as pg_errors

    IntegrityError = pg_errors.IntegrityError
else:
    psycopg = None
    IntegrityError = sqlite3.IntegrityError


class Row(dict):
    """Mapping row that also supports SQLite-style numeric indexing."""

    def __init__(self, columns: Sequence[str], values: Sequence[Any]):
        super().__init__(zip(columns, values))
        self._values = tuple(values)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class PgCursor:
    def __init__(self, cursor: Any, *, lastrowid: int | None = None):
        self._cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = getattr(cursor, "rowcount", -1)

    def _columns(self) -> list[str]:
        desc = self._cursor.description or []
        return [getattr(item, "name", item[0]) for item in desc]

    def fetchone(self) -> Row | None:
        raw = self._cursor.fetchone()
        if raw is None:
            return None
        if isinstance(raw, dict):
            cols = list(raw.keys())
            return Row(cols, [raw[c] for c in cols])
        return Row(self._columns(), raw)

    def fetchall(self) -> list[Row]:
        raws = self._cursor.fetchall()
        cols = self._columns()
        out: list[Row] = []
        for raw in raws:
            if isinstance(raw, dict):
                keys = list(raw.keys())
                out.append(Row(keys, [raw[k] for k in keys]))
            else:
                out.append(Row(cols, raw))
        return out

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())


_QMARK_RE = re.compile(r"\?")


def _translate_sql(sql: str) -> str:
    sql = sql.strip()
    sql = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "BIGSERIAL PRIMARY KEY",
        sql,
        flags=re.I,
    )
    sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.I)
    if re.match(r"INSERT\s+INTO\s+settings\s*\(", sql, flags=re.I) and "ON CONFLICT" not in sql.upper():
        sql += " ON CONFLICT DO NOTHING"
    sql = _QMARK_RE.sub("%s", sql)
    return sql


class PgConnection:
    def __init__(self) -> None:
        assert psycopg is not None
        self._conn = psycopg.connect(DATABASE_URL, connect_timeout=20)

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> PgCursor:
        translated = _translate_sql(sql)
        wants_id = bool(re.match(r"INSERT\s+INTO\s+tasks\s*\(", translated, flags=re.I)) and "RETURNING" not in translated.upper()
        if wants_id:
            translated += " RETURNING id"
        cur = self._conn.cursor()
        cur.execute(translated, tuple(params or ()))
        lastrowid = None
        if wants_id:
            row = cur.fetchone()
            if row:
                lastrowid = int(row[0])
        return PgCursor(cur, lastrowid=lastrowid)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> PgCursor:
        cur = self._conn.cursor()
        cur.executemany(_translate_sql(sql), list(seq_of_params))
        return PgCursor(cur)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PgConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()


def connect_db() -> Any:
    if DATABASE_URL:
        return PgConnection()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def add_column(conn: Any, table: str, definition: str) -> None:
    name = definition.split()[0]
    if DATABASE_URL:
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=? AND column_name=?",
            (table, name),
        ).fetchone()
        if not row:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
        return
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
