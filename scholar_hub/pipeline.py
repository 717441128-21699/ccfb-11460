"""端到端流水线：仓储注册、收割、作品挂接、归并、评估的可复用入口。"""
from __future__ import annotations

import json
import logging
import sqlite3

from .config import GEN_DATA_DIR
from .db import connect, init_db, reset_db
from .evaluation import evaluate
from .ingest.disambiguation import Disambiguator
from .ingest.store import IngestStore
from .ingest.works import WorkLinker
from .oai.harvester import Harvester

log = logging.getLogger("scholar_hub.pipeline")


def register_repositories(conn: sqlite3.Connection,
                          servers_path=GEN_DATA_DIR / "servers.json") -> list[int]:
    store = IngestStore(conn)
    with open(servers_path, "r", encoding="utf-8") as f:
        servers = json.load(f)
    ids = []
    for item in servers:
        ids.append(store.upsert_repository({
            "code": item["code"],
            "name": item["name"],
            "base_url": item["base_url"],
            "metadata_format": item["metadata_format"],
            "set_spec": item["set_spec"],
            "page_size": item["page_size"],
            "quirks_json": json.dumps(item["quirks"], ensure_ascii=False),
        }))
    return ids


def harvest_all(conn: sqlite3.Connection, from_date: str | None = None,
                until_date: str | None = None,
                force_fresh: bool = False) -> list[dict]:
    harvester = Harvester(conn)
    results = []
    for repo in IngestStore(conn).list_repositories():
        summary = harvester.harvest_repository(
            repo, from_date=from_date, until_date=until_date,
            force_fresh=force_fresh)
        results.append(summary.as_dict())
        log.info("repo=%s status=%s seen=%d ins=%d upd=%d del=%d token_exp=%d",
                 repo["code"], summary.status, summary.seen, summary.inserted,
                 summary.updated, summary.deleted, summary.token_expired)
    return results


def build_works(conn: sqlite3.Connection) -> dict:
    return WorkLinker(conn).rebuild()


def disambiguate(conn: sqlite3.Connection, threshold: float | None = None) -> dict:
    d = Disambiguator(conn) if threshold is None else Disambiguator(
        conn, accept_threshold=threshold)
    return d.run()


def run_full_pipeline(db_path=None, *, fresh: bool = True,
                      harvest_windows: list[tuple[str | None, str | None]]
                      | None = None,
                      threshold: float | None = None) -> dict:
    if fresh:
        reset_db(db_path)
    conn = connect(db_path)
    init_db(conn)
    try:
        register_repositories(conn)
        windows = harvest_windows or [(None, None)]
        all_runs = []
        for win in windows:
            all_runs.extend(harvest_all(
                conn, from_date=win[0], until_date=win[1]))
        works = build_works(conn)
        dis = disambiguate(conn, threshold=threshold)
        metrics = evaluate(conn)
        return {"harvest_runs": all_runs, "works": works,
                "disambiguation": dis, "metrics": metrics}
    finally:
        conn.close()
