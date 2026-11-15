"""SQLite 连接与 schema 管理。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DB_PATH, ensure_dirs

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    metadata_format TEXT NOT NULL,          -- oai_dc | marcxml
    set_spec TEXT,
    page_size INTEGER,
    quirks_json TEXT NOT NULL DEFAULT '[]',
    is_active INTEGER NOT NULL DEFAULT 1,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvest_runs (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    from_date TEXT,
    until_date TEXT,
    status TEXT NOT NULL,                   -- running|completed|failed|stopped
    records_seen INTEGER NOT NULL DEFAULT 0,
    records_upserted INTEGER NOT NULL DEFAULT 0,
    records_deleted INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    resumed_from INTEGER,
    token_expired_events INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS harvest_checkpoints (
    repository_id INTEGER PRIMARY KEY REFERENCES repositories(id),
    run_id INTEGER NOT NULL,
    resumption_token TEXT,
    cursor INTEGER NOT NULL DEFAULT 0,
    last_datestamp TEXT,
    total_count INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    oai_identifier TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    abstract TEXT,
    date TEXT,
    type TEXT,
    publisher TEXT,
    language TEXT,
    doi TEXT,
    datestamp TEXT NOT NULL,
    source_payload_hash TEXT,
    first_harvested_at TEXT NOT NULL,
    last_harvested_at TEXT NOT NULL,
    UNIQUE(repository_id, oai_identifier)
);
CREATE INDEX IF NOT EXISTS idx_records_doi ON records(doi);
CREATE INDEX IF NOT EXISTS idx_records_date ON records(date);

CREATE TABLE IF NOT EXISTS record_dois (
    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    doi TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'primary',
    PRIMARY KEY (record_id, doi)
);

CREATE TABLE IF NOT EXISTS record_subjects (
    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    PRIMARY KEY (record_id, subject)
);

CREATE TABLE IF NOT EXISTS record_events (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER,
    oai_identifier TEXT,
    event TEXT NOT NULL,                    -- inserted|updated|deleted
    at TEXT NOT NULL,
    run_id INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_rid ON record_events(repository_id, oai_identifier);

CREATE TABLE IF NOT EXISTS works (
    id INTEGER PRIMARY KEY,
    canonical_doi TEXT,
    title TEXT,
    year TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS record_work (
    record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
    work_id INTEGER NOT NULL REFERENCES works(id),
    role TEXT NOT NULL DEFAULT 'version'
);
CREATE INDEX IF NOT EXISTS idx_rw_work ON record_work(work_id);

CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    canonical_email TEXT,
    preferred_affiliation TEXT,
    kind TEXT NOT NULL DEFAULT 'auto',     -- auto|manual
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 作者提及（mention）：mention_key 跨收割稳定，是手工锁的锚点
CREATE TABLE IF NOT EXISTS record_creators (
    id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    mention_key TEXT NOT NULL,
    raw_name TEXT NOT NULL,
    email TEXT,
    affiliation TEXT,
    person_id INTEGER REFERENCES persons(id),
    UNIQUE(record_id, position)
);
CREATE INDEX IF NOT EXISTS idx_creators_person ON record_creators(person_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_creators_mkey ON record_creators(mention_key);

-- 手工锁定：某个提及必须归属某 person，自动归并不得改动
CREATE TABLE IF NOT EXISTS mention_locks (
    mention_key TEXT PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES persons(id),
    locked_at TEXT NOT NULL,
    note TEXT
);

-- 手工成对约束：合并(must_link)/拆分(cannot_link)
CREATE TABLE IF NOT EXISTS pair_constraints (
    id INTEGER PRIMARY KEY,
    person_a INTEGER NOT NULL REFERENCES persons(id),
    person_b INTEGER NOT NULL REFERENCES persons(id),
    kind TEXT NOT NULL,                    -- must_link|cannot_link
    created_at TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_constraints_ab ON pair_constraints(person_a, person_b);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    ensure_dirs()
    path = Path(db_path) if db_path is not None else DB_PATH
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def reset_db(db_path: Path | str | None = None) -> None:
    """删除旧库（含 WAL 侧车文件）后重建空库。"""
    path = Path(db_path) if db_path is not None else DB_PATH
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    conn = connect(path)
    try:
        init_db(conn)
    finally:
        conn.close()
