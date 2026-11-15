"""Flask 五块界面与手工归并端点的冒烟测试。"""
from __future__ import annotations

import json

from scholar_hub.ingest.disambiguation import Disambiguator
from scholar_hub.ingest.store import IngestStore
from scholar_hub.ingest.works import WorkLinker
from scholar_hub.oai.harvester import Harvester
from scholar_hub.web.app import create_app


def _seed(conn):
    h = Harvester(conn)
    for repo in IngestStore(conn).list_repositories():
        h.harvest_repository(repo, force_fresh=True)
    WorkLinker(conn).rebuild()
    Disambiguator(conn).run()


def test_all_pages_render(conn, dataset):
    _seed(conn)
    app = create_app(db_path=_db_path(conn))
    app.config["TESTING"] = True
    client = app.test_client()
    for path in ["/", "/repositories", "/harvest", "/records", "/authors",
                 "/evaluation"]:
        resp = client.get(path)
        assert resp.status_code == 200, (path, resp.status_code)


def test_record_and_scholar_detail(conn, dataset):
    _seed(conn)
    app = create_app(db_path=_db_path(conn))
    client = app.test_client()
    rid = conn.execute("SELECT id FROM records LIMIT 1").fetchone()["id"]
    assert client.get(f"/records/{rid}").status_code == 200
    pid = conn.execute("SELECT id FROM persons LIMIT 1").fetchone()["id"]
    assert client.get(f"/scholars/{pid}").status_code == 200
    # BibTeX 导出
    resp = client.get(f"/scholars/{pid}/bibtex")
    assert resp.status_code == 200 and resp.data.startswith(b"@")


def test_manual_split_via_web(conn, dataset):
    _seed(conn)
    app = create_app(db_path=_db_path(conn))
    client = app.test_client()
    person = conn.execute(
        """SELECT person_id, COUNT(*) c FROM record_creators
           WHERE person_id IS NOT NULL GROUP BY person_id
           HAVING c >= 2 ORDER BY c DESC LIMIT 1""").fetchone()
    pid = person["person_id"]
    key = conn.execute(
        "SELECT mention_key FROM record_creators WHERE person_id=? LIMIT 1",
        (pid,)).fetchone()["mention_key"]
    resp = client.post(f"/authors/{pid}/split",
                       data={"mention_keys": [key]},
                       follow_redirects=True)
    assert resp.status_code == 200
    # 锁已建立，重跑归并不被冲掉
    new_pid = conn.execute(
        "SELECT person_id FROM mention_locks WHERE mention_key=?",
        (key,)).fetchone()["person_id"]
    Disambiguator(conn).run()
    assert conn.execute(
        "SELECT person_id FROM record_creators WHERE mention_key=?",
        (key,)).fetchone()["person_id"] == new_pid


def test_harvest_run_endpoint(conn, dataset):
    app = create_app(db_path=_db_path(conn))
    client = app.test_client()
    repo = conn.execute("SELECT id FROM repositories LIMIT 1").fetchone()["id"]
    resp = client.post("/harvest/run",
                       data={"repository_id": repo},
                       follow_redirects=True)
    assert resp.status_code == 200


def _db_path(conn):
    # 通过连接的文件名反推路径供 app 工厂使用
    cur = conn.execute("PRAGMA database_list").fetchone()
    return cur[2]
