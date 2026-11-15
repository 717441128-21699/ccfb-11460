"""一键演示：生成数据 -> 启动 10 个本地仓储 -> 两轮收割 -> 挂接/归并 -> 评估。

全程只访问 127.0.0.1。用法：  py run_demo.py
"""
from __future__ import annotations

import json
import logging
import sys
import time

from scholar_hub.config import GEN_DATA_DIR
from scholar_hub.data_gen import build_dataset, write_dataset
from scholar_hub.db import connect, reset_db
from scholar_hub.evaluation import evaluate
from scholar_hub.ingest.disambiguation import Disambiguator
from scholar_hub.ingest.works import WorkLinker
from scholar_hub.pipeline import (build_works, disambiguate, harvest_all,
                                  register_repositories)
from scholar_hub.repositories.server import ServerManager


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("run_demo")

    log.info("1/6 生成带真值的模拟数据集 ...")
    dataset = build_dataset()
    write_dataset(dataset)
    log.info("  学者=%d 论文=%d 仓储=%d",
             len(dataset["persons"]), len(dataset["publications"]),
             len(dataset["specs"]))

    log.info("2/6 启动 %d 个本地 OAI-PMH 仓储（127.0.0.1）...", len(dataset["specs"]))
    manager = ServerManager()
    servers = manager.start()
    time.sleep(0.4)
    try:
        log.info("3/6 初始化数据库并登记仓储 ...")
        reset_db()
        conn = connect()
        register_repositories(conn)

        log.info("4/6 第一轮全量收割 ...")
        first = harvest_all(conn)
        log.info("  合计 inserted=%d updated=%d deleted=%d token_expired=%d",
                 sum(r["inserted"] for r in first),
                 sum(r["updated"] for r in first),
                 sum(r["deleted"] for r in first),
                 sum(r["token_expired"] for r in first))

        log.info("5/6 第二轮增量收割（2026-11-01 起，含更新/删除/新增）...")
        second = harvest_all(conn, from_date="2026-11-01T00:00:00Z")
        log.info("  合计 inserted=%d updated=%d deleted=%d",
                 sum(r["inserted"] for r in second),
                 sum(r["updated"] for r in second),
                 sum(r["deleted"] for r in second))

        log.info("6/6 作品挂接 + 作者归并 + 真值评估 ...")
        works = build_works(conn)
        dis = disambiguate(conn)
        metrics = evaluate(conn)
        conn.close()
    finally:
        manager.stop()

    report = {
        "servers": [{"code": s["code"], "port": s["port"],
                     "quirks": s["quirks"]} for s in servers],
        "harvest_round_1": first,
        "harvest_round_2": second,
        "works": works,
        "disambiguation": dis,
        "metrics": metrics,
    }
    out = GEN_DATA_DIR / "demo_report.json"
    with out.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    m = metrics
    print("\n================ 评估结果 ================")
    print(f"提及总数(可评估) : {m['mentions_evaluated']}")
    print(f"真值学者 / 预测学者: {m['truth_persons']} / {m['pred_persons']}")
    print(f"成对精确率 P     : {m['pair']['precision']:.4f}  "
          f"(目标 >= 0.90: {'达标' if m['target_pair_p_ge_09'] else '未达标'})")
    print(f"成对召回率 R     : {m['pair']['recall']:.4f}  "
          f"(目标 >= 0.90: {'达标' if m['target_pair_r_ge_09'] else '未达标'})")
    print(f"成对 F1          : {m['pair']['f1']:.4f}")
    print(f"B3  P/R/F1       : {m['b3']['precision']:.4f} / "
          f"{m['b3']['recall']:.4f} / {m['b3']['f1']:.4f}")
    print(f"提及级准确率     : {m['mention_accuracy']:.4f}")
    print(f"重名组成对准确率 : {m['homonym_pair_accuracy']:.4f}")
    print(f"作品(work)数     : {works['works']}，其中多版本挂接: "
          f"{works['multi_version_works']}")
    print(f"完整报告已写入   : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
