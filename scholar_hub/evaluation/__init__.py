"""归并质量评估：与保留的真值身份比对，输出精确率/召回率。

两类指标：
1. 成对（pairwise）：同一真值 person 的两个提及是否被分到同一预测 person，
   以及被分到一起的两个提及是否真值相同 —— 直接对应“错并/漏并”。
2. B^3：逐提及 precision/recall 的平均，对大类不敏感，更贴近实际观感。
另附提及级准确率与重名组专项结果。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from ..config import GEN_DATA_DIR
from ..ingest.store import IngestStore


def _truth_key(repo_id_by_code: dict[str, int], item: dict) -> str:
    return IngestStore.mention_key(
        repo_id_by_code[item["repo_code"]],
        item["oai_identifier"], item["position"])


def evaluate(conn: sqlite3.Connection,
             truth_dir: Path | None = None) -> dict:
    base = truth_dir or GEN_DATA_DIR
    with (base / "truth_mentions.json").open("r", encoding="utf-8") as f:
        truth_mentions = json.load(f)
    with (base / "truth_persons.json").open("r", encoding="utf-8") as f:
        truth_persons = json.load(f)

    repo_id_by_code = {
        r["code"]: int(r["id"]) for r in conn.execute(
            "SELECT id, code FROM repositories")}

    # 已硬删除记录的真值提及不应计入评估分母（系统按删除标记真删是正确行为）
    deleted_pairs: set[tuple[str, str]] = set()
    for path in sorted(base.glob("repo_*.json")):
        for rec in json.loads(path.read_text(encoding="utf-8")):
            if rec.get("status") == "deleted":
                deleted_pairs.add(
                    (rec["repo_code"], rec["oai_identifier"]))

    truth_of: dict[str, str] = {}
    skipped_deleted = 0
    for item in truth_mentions:
        if item["repo_code"] not in repo_id_by_code:
            continue
        if (item["repo_code"], item["oai_identifier"]) in deleted_pairs:
            skipped_deleted += 1
            continue
        truth_of[_truth_key(repo_id_by_code, item)] = item["person_id"]

    pred_rows = conn.execute(
        "SELECT mention_key, person_id FROM record_creators").fetchall()
    pred_of = {r["mention_key"]: int(r["person_id"]) for r in pred_rows}

    common = sorted(set(truth_of) & set(pred_of))
    missing = sorted(set(truth_of) - set(pred_of))
    extra = sorted(set(pred_of) - set(truth_of))

    # ------------------------------------------------ 成对指标
    truth_members: dict[str, list[str]] = defaultdict(list)
    pred_members: dict[int, list[str]] = defaultdict(list)
    for key in common:
        truth_members[truth_of[key]].append(key)
        pred_members[pred_of[key]].append(key)

    def pair_count(groups: dict) -> int:
        return sum(len(v) * (len(v) - 1) // 2 for v in groups.values())

    truth_pairs = _pair_set(truth_members)
    pred_pairs = _pair_set(pred_members)
    tp = len(truth_pairs & pred_pairs)
    pair_precision = tp / len(pred_pairs) if pred_pairs else 1.0
    pair_recall = tp / len(truth_pairs) if truth_pairs else 1.0
    pair_f1 = _f1(pair_precision, pair_recall)

    # ------------------------------------------------ B^3 指标
    b3_p = b3_r = 0.0
    for key in common:
        t_cluster = truth_members[truth_of[key]]
        p_cluster = pred_members[pred_of[key]]
        p_set, t_set = set(p_cluster), set(t_cluster)
        b3_p += len(p_set & t_set) / len(p_set)
        b3_r += len(p_set & t_set) / len(t_set)
    b3_precision = b3_p / len(common)
    b3_recall = b3_r / len(common)
    b3_f1 = _f1(b3_precision, b3_recall)

    # ------------------------------------------------ 提及准确率
    correct_mentions = sum(
        1 for k in common
        if _cluster_dominant_truth(pred_members[pred_of[k]], truth_of)
        == truth_of[k])

    # ------------------------------------------------ 重名组专项
    name_of_person = {p["person_id"]: p["family_zh"] + p["given_zh"]
                      for p in truth_persons}
    homonym_groups = defaultdict(list)
    for p in truth_persons:
        homonym_groups[p["family_zh"] + p["given_zh"]].append(p["person_id"])
    homonym_ids = {pid for name, ids in homonym_groups.items()
                   if len(ids) > 1 for pid in ids}
    homo_keys = [k for k in common if truth_of[k] in homonym_ids]
    homo_tp = sum(1 for a, b in _iter_pairs(homo_keys)
                  if (pred_of[a] == pred_of[b])
                  == (truth_of[a] == truth_of[b]))

    return {
        "mentions_evaluated": len(common),
        "truth_mentions_missing": len(missing),
        "truth_mentions_deleted_skipped": skipped_deleted,
        "pred_mentions_extra": len(extra),
        "pred_persons": conn.execute(
            "SELECT COUNT(*) AS n FROM persons").fetchone()["n"],
        "truth_persons": len(truth_persons),
        "pair": {
            "tp_pairs": tp,
            "truth_pairs": len(truth_pairs),
            "pred_pairs": len(pred_pairs),
            "precision": round(pair_precision, 4),
            "recall": round(pair_recall, 4),
            "f1": round(pair_f1, 4),
        },
        "b3": {
            "precision": round(b3_precision, 4),
            "recall": round(b3_recall, 4),
            "f1": round(b3_f1, 4),
        },
        "mention_accuracy": round(correct_mentions / len(common), 4)
        if common else 0.0,
        "homonym_pair_accuracy": round(
            homo_tp / max(1,
                          len(homo_keys) * (len(homo_keys) - 1) // 2), 4),
        "target_pair_p_ge_09": pair_precision >= 0.90,
        "target_pair_r_ge_09": pair_recall >= 0.90,
    }


def _pair_set(groups: dict) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for members in groups.values():
        for a, b in _iter_pairs(sorted(members)):
            pairs.add((a, b))
    return pairs


def _iter_pairs(items: list[str]):
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            yield items[i], items[j]


def _cluster_dominant_truth(keys: list[str], truth_of: dict[str, str]) -> str:
    votes: dict[str, int] = defaultdict(int)
    for k in keys:
        votes[truth_of[k]] += 1
    return max(votes.items(), key=lambda kv: kv[1])[0]


def _f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if p + r else 0.0
