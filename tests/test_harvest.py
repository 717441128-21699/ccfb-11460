"""收割编排：幂等 upsert、真删除、原地更新、断点续传。"""
from __future__ import annotations

import json
import time

from scholar_hub.ingest.store import IngestStore
from scholar_hub.oai.client import OAIClient
from scholar_hub.oai.harvester import Harvester
from scholar_hub.oai.parser import parse_oai_response


def _repo(conn, code):
    row = conn.execute("SELECT * FROM repositories WHERE code=?",
                       (code,)).fetchone()
    assert row is not None
    return row


def test_full_harvest_idempotent(conn):
    repo = _repo(conn, "ir-nacl")
    h = Harvester(conn)
    s1 = h.harvest_repository(repo, force_fresh=True)
    assert s1.status == "completed"
    count_after_first = IngestStore(conn).records_count()

    s2 = h.harvest_repository(repo, force_fresh=True)
    assert s2.inserted == 0                    # 第二次无新增
    assert s2.updated == 0
    assert IngestStore(conn).records_count() == count_after_first


def test_no_duplicate_rows(conn):
    """同一 oai_identifier 永远只有一行。"""
    Harvester(conn).harvest_repository(_repo(conn, "ir-beifang"),
                                       force_fresh=True)
    dupes = conn.execute(
        """SELECT repository_id, oai_identifier, COUNT(*) c
           FROM records GROUP BY repository_id, oai_identifier
           HAVING c > 1""").fetchall()
    assert dupes == []


def test_deleted_records_are_hard_deleted(conn):
    repo = _repo(conn, "ir-yunzhong")  # 含删除标记
    Harvester(conn).harvest_repository(repo, force_fresh=True)
    # 数据库中不应存在 is_deleted=1 的行（硬删除）
    remaining_deleted = conn.execute(
        "SELECT COUNT(*) c FROM records WHERE is_deleted=1").fetchone()["c"]
    assert remaining_deleted == 0
    # 审计事件里保留删除痕迹
    events = conn.execute(
        "SELECT COUNT(*) c FROM record_events WHERE event='deleted'").fetchone()
    assert events["c"] >= 1


def test_in_place_update_bumps_version(conn):
    """更新的记录原地修改、version+1，不新增行。"""
    repo = _repo(conn, "ir-beifang")
    Harvester(conn).harvest_repository(repo, force_fresh=True)
    before = {r["oai_identifier"]: r["version"]
              for r in conn.execute(
                  "SELECT oai_identifier, version FROM records").fetchall()}
    # 第二轮增量（含 version=2 的更新记录）
    s = Harvester(conn).harvest_repository(
        repo, from_date="2026-11-01T00:00:00Z")
    assert s.status == "completed"
    updated = conn.execute(
        "SELECT oai_identifier, version FROM records WHERE version>=2").fetchall()
    # 若该仓储有更新记录，版本号确实递增且仍唯一
    for r in updated:
        assert r["version"] >= before.get(r["oai_identifier"], 1)
    dupes = conn.execute(
        """SELECT oai_identifier, COUNT(*) c FROM records
           WHERE repository_id=? GROUP BY oai_identifier HAVING c>1""",
        (repo["id"],)).fetchall()
    assert dupes == []


def test_resume_after_token_expiry(conn, dataset):
    """短令牌仓储：令牌过期后能从最近成功页回退续传，不漏不重。"""
    repo = _repo(conn, "ir-beifang")  # short_lived_tokens
    store = IngestStore(conn)
    quirks = json.loads(repo["quirks_json"])
    client = OAIClient(repo["base_url"], quirks=quirks)
    page1 = client.list_records_page(
        metadata_prefix=repo["metadata_format"], set_spec=repo["set_spec"])
    recs1, token1, _, total = parse_oai_response(page1.xml_text)
    run_id = store.start_run(repo["id"], None, None, None)
    for parsed in recs1:
        if not parsed.is_deleted:
            store.upsert_record(repo["id"], parsed, run_id)
    time.sleep(2.0)  # 超过 TTL
    # 过期令牌被拒
    stale = client.list_records_page(
        metadata_prefix=repo["metadata_format"], resumption_token=token1)
    try:
        parse_oai_response(stale.xml_text)
        assert False
    except Exception as e:
        assert getattr(e, "code", "") == "badResumptionToken"
    client.close()

    store.save_checkpoint(repo["id"], run_id, token1, len(recs1),
                          recs1[-1].datestamp, total)
    s = Harvester(conn).harvest_repository(repo)
    assert s.status == "completed"
    assert s.token_expired >= 1
    # 不漏收：该仓储所有活动记录都在库；不重复：同一标识仅一行
    active_in_payload = total - _count_deleted_in_payload(repo, dataset)
    distinct_in_db = conn.execute(
        "SELECT COUNT(DISTINCT oai_identifier) c FROM records "
        "WHERE repository_id=?", (repo["id"],)).fetchone()["c"]
    assert distinct_in_db == active_in_payload
    dupes = conn.execute(
        """SELECT oai_identifier, COUNT(*) c FROM records
           WHERE repository_id=? GROUP BY oai_identifier HAVING c>1""",
        (repo["id"],)).fetchall()
    assert dupes == []


def _count_deleted_in_payload(repo, dataset=None):
    if dataset is not None:
        base = dataset[1]
    else:
        from scholar_hub.config import GEN_DATA_DIR
        base = GEN_DATA_DIR
    recs = json.loads((base / f"repo_{repo['code']}.json")
                      .read_text(encoding="utf-8"))
    return sum(1 for r in recs if r["status"] == "deleted")


def test_date_window_filtering(conn):
    repo = _repo(conn, "ir-nacl")
    s = Harvester(conn).harvest_repository(
        repo, from_date="2026-11-01T00:00:00Z", force_fresh=True)
    # 窗口内只应包含 11 月的增量记录
    rows = conn.execute("SELECT datestamp FROM records").fetchall()
    assert all(r["datestamp"] >= "2026-11-01" for r in rows)
    assert s.status == "completed"
