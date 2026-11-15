"""作者消歧、手工归并锁、重跑保护测试。"""
from __future__ import annotations

import json

from scholar_hub.evaluation import evaluate
from scholar_hub.ingest.disambiguation import Disambiguator
from scholar_hub.ingest.names import names_compatible, normalize_name
from scholar_hub.ingest.store import IngestStore
from scholar_hub.ingest.works import WorkLinker
from scholar_hub.oai.harvester import Harvester


def _harvest_all(conn):
    h = Harvester(conn)
    for repo in IngestStore(conn).list_repositories():
        h.harvest_repository(repo, force_fresh=True)
    WorkLinker(conn).rebuild()


def test_name_forms_unify():
    """中文名、拼音、缩写、英文大写归一到同一 block 且姓名兼容。"""
    forms = [normalize_name(x) for x in
             ["张伟", "Wei Zhang", "Zhang, Wei", "W. Zhang", "WEI ZHANG"]]
    blocks = {f.block_key for f in forms}
    assert blocks == {"zhang|w"}
    for f in forms[1:]:
        assert names_compatible(forms[0], f)


def test_surname_aware_given_family_order():
    """Li Qiang 这种名前姓后写法应识别 Li 为姓。"""
    f = normalize_name("Li Qiang")
    assert f.family == "li" and f.given == "qiang"
    assert f.block_key == "li|q"


def test_disambiguation_meets_targets(conn):
    _harvest_all(conn)
    Disambiguator(conn).run()
    m = evaluate(conn)
    assert m["pair"]["precision"] >= 0.90
    assert m["pair"]["recall"] >= 0.90
    assert m["mention_accuracy"] >= 0.90


def test_manual_merge_then_rerun_holds(conn):
    _harvest_all(conn)
    d = Disambiguator(conn)
    d.run()
    # 找两个不同的 person 做手工合并
    persons = conn.execute(
        "SELECT id FROM persons ORDER BY id LIMIT 2").fetchall()
    keep, drop = persons[0]["id"], persons[1]["id"]
    d.merge_persons(keep, drop, note="测试手工合并")
    assert conn.execute("SELECT COUNT(*) c FROM persons WHERE id=?",
                        (drop,)).fetchone()["c"] == 0
    mention_count = conn.execute(
        "SELECT COUNT(*) c FROM record_creators WHERE person_id=?",
        (keep,)).fetchone()["c"]
    # 重跑自动归并，手工合并的提及仍在 keep 下（锁保护）
    Disambiguator(conn).run()
    after = conn.execute(
        "SELECT COUNT(*) c FROM record_creators WHERE person_id=?",
        (keep,)).fetchone()["c"]
    assert after >= mention_count
    # drop 人不再出现；keep 被标记为手工档案
    assert conn.execute("SELECT kind FROM persons WHERE id=?",
                        (keep,)).fetchone()["kind"] == "manual"


def test_manual_split_creates_cannot_link(conn):
    _harvest_all(conn)
    d = Disambiguator(conn)
    d.run()
    person = conn.execute(
        """SELECT person_id, COUNT(*) c FROM record_creators
           WHERE person_id IS NOT NULL GROUP BY person_id
           HAVING c >= 2 ORDER BY c DESC LIMIT 1""").fetchone()
    pid = person["person_id"]
    keys = [r["mention_key"] for r in conn.execute(
        "SELECT mention_key FROM record_creators WHERE person_id=? LIMIT 1",
        (pid,))]
    new_id = d.split_person(pid, keys, note="测试手工拆分")
    assert new_id != pid
    # 锁与 cannot_link 落库
    assert conn.execute(
        "SELECT COUNT(*) c FROM mention_locks WHERE person_id=?",
        (new_id,)).fetchone()["c"] == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM pair_constraints WHERE kind='cannot_link'") \
        .fetchone()["c"] >= 1
    # 重跑后被拆提及仍归属新 person
    Disambiguator(conn).run()
    assert conn.execute(
        "SELECT person_id FROM record_creators WHERE mention_key=?",
        (keys[0],)).fetchone()["person_id"] == new_id


def test_assign_mention_lock(conn):
    _harvest_all(conn)
    d = Disambiguator(conn)
    d.run()
    target = conn.execute("SELECT id FROM persons ORDER BY id LIMIT 1") \
        .fetchone()["id"]
    key = conn.execute(
        "SELECT mention_key FROM record_creators LIMIT 1").fetchone()[0]
    d.assign_mention(key, target, note="单条加锁")
    assert conn.execute(
        "SELECT person_id FROM record_creators WHERE mention_key=?",
        (key,)).fetchone()["person_id"] == target
    Disambiguator(conn).run()
    assert conn.execute(
        "SELECT person_id FROM record_creators WHERE mention_key=?",
        (key,)).fetchone()["person_id"] == target
