"""入库存储：幂等 upsert、硬删除、审计事件。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from ..oai.parser import ParsedRecord


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class IngestStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------ 仓储
    def upsert_repository(self, item: dict) -> int:
        conn = self.conn
        now = utcnow_iso()
        conn.execute(
            """INSERT INTO repositories
               (code, name, base_url, metadata_format, set_spec, page_size,
                quirks_json, is_active, registered_at)
               VALUES (:code,:name,:base_url,:metadata_format,:set_spec,:page_size,
                       :quirks_json,1,:now)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, base_url=excluded.base_url,
                 metadata_format=excluded.metadata_format,
                 set_spec=excluded.set_spec, page_size=excluded.page_size,
                 quirks_json=excluded.quirks_json, is_active=1""",
            {**item, "now": now})
        conn.commit()
        row = conn.execute("SELECT id FROM repositories WHERE code=?",
                           (item["code"],)).fetchone()
        return int(row["id"])

    def get_repository(self, repo_id: int) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()

    def list_repositories(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM repositories ORDER BY code").fetchall()

    # ------------------------------------------------------------ 收割运行
    def start_run(self, repo_id: int, from_date: str | None,
                  until_date: str | None, resumed_from: int | None) -> int:
        cur = self.conn.execute(
            """INSERT INTO harvest_runs
               (repository_id, started_at, from_date, until_date, status,
                resumed_from)
               VALUES (?,?,?,?,'running',?)""",
            (repo_id, utcnow_iso(), from_date, until_date, resumed_from))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE harvest_runs SET finished_at=?, status=?, error=? WHERE id=?",
            (utcnow_iso(), status, error, run_id))
        self.conn.commit()

    def save_checkpoint(self, repo_id: int, run_id: int, token: str | None,
                        cursor: int, last_datestamp: str | None,
                        total: int | None) -> None:
        self.conn.execute(
            """INSERT INTO harvest_checkpoints
               (repository_id, run_id, resumption_token, cursor, last_datestamp,
                total_count, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(repository_id) DO UPDATE SET
                 run_id=excluded.run_id,
                 resumption_token=excluded.resumption_token,
                 cursor=excluded.cursor,
                 last_datestamp=excluded.last_datestamp,
                 total_count=excluded.total_count,
                 updated_at=excluded.updated_at""",
            (repo_id, run_id, token, cursor, last_datestamp, total,
             utcnow_iso()))
        self.conn.commit()

    def get_checkpoint(self, repo_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM harvest_checkpoints WHERE repository_id=?",
            (repo_id,)).fetchone()

    def clear_checkpoint(self, repo_id: int) -> None:
        self.conn.execute(
            "DELETE FROM harvest_checkpoints WHERE repository_id=?", (repo_id,))
        self.conn.commit()

    def last_unfinished_run(self, repo_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM harvest_runs
               WHERE repository_id=? AND status IN ('running','failed')
               ORDER BY id DESC LIMIT 1""", (repo_id,)).fetchone()

    def bump_run_counters(self, run_id: int, *, seen: int = 0,
                          upserted: int = 0, deleted: int = 0,
                          pages: int = 0, token_expired: int = 0) -> None:
        self.conn.execute(
            """UPDATE harvest_runs SET
                 records_seen=records_seen+?,
                 records_upserted=records_upserted+?,
                 records_deleted=records_deleted+?,
                 pages=pages+?,
                 token_expired_events=token_expired_events+?
               WHERE id=?""",
            (seen, upserted, deleted, pages, token_expired, run_id))
        self.conn.commit()

    # ------------------------------------------------------------ 记录
    def _event(self, repo_id: int, identifier: str, event: str,
               run_id: int, detail: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO record_events
               (repository_id, oai_identifier, event, at, run_id, detail)
               VALUES (?,?,?,?,?,?)""",
            (repo_id, identifier, event, utcnow_iso(), run_id, detail))

    def hard_delete_record(self, repo_id: int, identifier: str,
                           run_id: int) -> bool:
        row = self.conn.execute(
            "SELECT id, is_deleted FROM records WHERE repository_id=? "
            "AND oai_identifier=?", (repo_id, identifier)).fetchone()
        if row is None:
            self._event(repo_id, identifier, "deleted", run_id,
                        "tombstone for unknown record (ignored)")
            return False
        # 级联删除 creators/subjects/dois/record_work；审计事件保留
        self.conn.execute("DELETE FROM records WHERE id=?", (row["id"],))
        self._event(repo_id, identifier, "deleted", run_id)
        self.conn.commit()
        return True

    def upsert_record(self, repo_id: int, parsed: ParsedRecord,
                      run_id: int) -> tuple[str, int]:
        """返回 (动作 inserted|updated|unchanged, record_id)。"""
        conn = self.conn
        now = utcnow_iso()
        existing = conn.execute(
            "SELECT * FROM records WHERE repository_id=? AND oai_identifier=?",
            (repo_id, parsed.oai_identifier)).fetchone()

        if existing is not None and existing["source_payload_hash"] == parsed.payload_hash:
            # 内容未变：只刷新收割时间，不动版本，不产生新行
            conn.execute(
                "UPDATE records SET last_harvested_at=? WHERE id=?",
                (now, existing["id"]))
            conn.commit()
            return "unchanged", int(existing["id"])

        if existing is None:
            cur = conn.execute(
                """INSERT INTO records
                   (repository_id, oai_identifier, version, is_deleted, title,
                    abstract, date, type, publisher, language, doi, datestamp,
                    source_payload_hash, first_harvested_at, last_harvested_at)
                   VALUES (?,?,?,0,?,?,?,?,?,?,?,?,?,?,?)""",
                (repo_id, parsed.oai_identifier, 1, parsed.title,
                 parsed.abstract, parsed.date, parsed.pub_type,
                 parsed.publisher, parsed.language, parsed.doi,
                 parsed.datestamp, parsed.payload_hash, now, now))
            record_id = int(cur.lastrowid)
            action = "inserted"
        else:
            record_id = int(existing["id"])
            conn.execute(
                """UPDATE records SET
                     version=version+1, is_deleted=0, title=?, abstract=?,
                     date=?, type=?, publisher=?, language=?, doi=?,
                     datestamp=?, source_payload_hash=?, last_harvested_at=?
                   WHERE id=?""",
                (parsed.title, parsed.abstract, parsed.date, parsed.pub_type,
                 parsed.publisher, parsed.language, parsed.doi,
                 parsed.datestamp, parsed.payload_hash, now, record_id))
            conn.execute("DELETE FROM record_creators WHERE record_id=?",
                         (record_id,))
            conn.execute("DELETE FROM record_subjects WHERE record_id=?",
                         (record_id,))
            conn.execute("DELETE FROM record_dois WHERE record_id=?",
                         (record_id,))
            action = "updated"

        for subj in dict.fromkeys(parsed.subjects):
            conn.execute(
                "INSERT OR IGNORE INTO record_subjects(record_id, subject) "
                "VALUES (?,?)", (record_id, subj))
        if parsed.doi:
            conn.execute(
                "INSERT OR IGNORE INTO record_dois(record_id, doi, kind) "
                "VALUES (?,?,'primary')", (record_id, parsed.doi))
        for rel_doi in dict.fromkeys(parsed.relation_dois):
            conn.execute(
                "INSERT OR IGNORE INTO record_dois(record_id, doi, kind) "
                "VALUES (?,?,'relation')", (record_id, rel_doi))
        for c in parsed.creators:
            mention_key = self.mention_key(repo_id, parsed.oai_identifier,
                                           c.position)
            conn.execute(
                """INSERT INTO record_creators
                   (record_id, position, mention_key, raw_name, email,
                    affiliation)
                   VALUES (?,?,?,?,?,?)""",
                (record_id, c.position, mention_key, c.raw_name, c.email,
                 c.affiliation))
        self._event(repo_id, parsed.oai_identifier, action, run_id)
        conn.commit()
        return action, record_id

    @staticmethod
    def mention_key(repo_id: int, oai_identifier: str, position: int) -> str:
        return f"r{repo_id}:{oai_identifier}#c{position}"

    def records_count(self, include_deleted: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM records"
        if not include_deleted:
            sql += " WHERE is_deleted=0"
        return int(self.conn.execute(sql).fetchone()["n"])
