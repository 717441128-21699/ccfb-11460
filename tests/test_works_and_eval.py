"""作品挂接（DOI/预印本）、BibTeX 导出与评估真值测试。"""
from __future__ import annotations

import json

from scholar_hub.evaluation import evaluate
from scholar_hub.ingest.bibtex import export_person_bibtex, export_work_bibtex
from scholar_hub.ingest.store import IngestStore
from scholar_hub.ingest.works import WorkLinker
from scholar_hub.oai.harvester import Harvester


def _harvest_all(conn):
    h = Harvester(conn)
    for repo in IngestStore(conn).list_repositories():
        h.harvest_repository(repo, force_fresh=True)


def test_work_linking_groups_versions(conn):
    _harvest_all(conn)
    stats = WorkLinker(conn).rebuild()
    assert stats["works"] >= 1
    # 至少存在一个含多条记录的 work（同 DOI 或预印本挂接）
    multi = conn.execute(
        """SELECT work_id, COUNT(*) c FROM record_work
           GROUP BY work_id HAVING c > 1 LIMIT 1""").fetchall()
    assert multi, "应至少有一个多版本 work"


def test_preprint_attached_to_formal(conn):
    """预印本与正式版通过 relation DOI 挂到同一 work。"""
    _harvest_all(conn)
    WorkLinker(conn).rebuild()
    preprint_records = conn.execute(
        "SELECT id FROM records WHERE type='preprint'").fetchall()
    assert preprint_records
    linked = conn.execute(
        """SELECT COUNT(*) c FROM record_work rw
           JOIN records r ON r.id=rw.record_id
           WHERE r.type='preprint'""").fetchone()["c"]
    assert linked == len(preprint_records)
    # 找到一个同时含 preprint 和正式版的 work
    mixed = conn.execute(
        """SELECT work_id FROM record_work rw JOIN records r ON r.id=rw.record_id
           GROUP BY work_id
           HAVING SUM(CASE WHEN r.type='preprint' THEN 1 ELSE 0 END) > 0
              AND SUM(CASE WHEN r.type!='preprint' THEN 1 ELSE 0 END) > 0
           LIMIT 1""").fetchall()
    assert mixed, "存在预印本-正式版同 work 的挂接"


def test_bibtex_export_valid(conn):
    _harvest_all(conn)
    WorkLinker(conn).rebuild()
    from scholar_hub.ingest.disambiguation import Disambiguator
    Disambiguator(conn).run()
    work_id = conn.execute("SELECT id FROM works LIMIT 1").fetchone()["id"]
    bib = export_work_bibtex(conn, work_id)
    assert bib.startswith("@")
    assert "title" in bib and "author" in bib and "year" in bib
    # 特殊字符被转义
    person_id = conn.execute(
        "SELECT id FROM persons LIMIT 1").fetchone()["id"]
    person_bib = export_person_bibtex(conn, person_id)
    assert person_bib.count("@") >= 1


def test_evaluation_against_truth(conn, dataset):
    _harvest_all(conn)
    WorkLinker(conn).rebuild()
    from scholar_hub.ingest.disambiguation import Disambiguator
    Disambiguator(conn).run()
    m = evaluate(conn, truth_dir=dataset[1])
    assert m["mentions_evaluated"] > 0
    assert m["truth_mentions_missing"] == 0
    assert 0 <= m["pair"]["precision"] <= 1
    assert 0 <= m["pair"]["recall"] <= 1
    # 数据设计目标
    assert m["pair"]["precision"] >= 0.90
    assert m["pair"]["recall"] >= 0.90


def test_truth_identities_preserved(dataset):
    """生成数据必须保留真实身份，且重名组确实包含多个真人。"""
    ds, _ = dataset
    persons = ds["persons"]
    from collections import Counter
    name_counts = Counter(p.family_zh + p.given_zh for p in persons)
    assert max(name_counts.values()) >= 6       # 有张伟级重名
    truth_mentions = ds["truth_mentions"]
    assert all("person_id" in m for m in truth_mentions)
