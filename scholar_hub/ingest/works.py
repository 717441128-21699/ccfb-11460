"""作品（work）挂接：同 DOI 合并、预印本与正式版挂到同一 work 下。"""
from __future__ import annotations

import sqlite3

from ..ingest.store import utcnow_iso


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # 稳定排序，保证多次运行结果一致
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


class WorkLinker:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def rebuild(self) -> dict:
        conn = self.conn
        records = conn.execute(
            """SELECT r.id, r.doi, r.title, r.date, r.type
               FROM records r WHERE r.is_deleted=0 ORDER BY r.id""").fetchall()
        doi_rows = conn.execute(
            "SELECT record_id, doi, kind FROM record_dois").fetchall()

        rels_by_record: dict[int, list[tuple[str, str]]] = {}
        dois_by_record: dict[int, set[str]] = {}
        for row in doi_rows:
            rels_by_record.setdefault(int(row["record_id"]), []).append(
                (row["doi"], row["kind"]))
            dois_by_record.setdefault(int(row["record_id"]), set()).add(row["doi"])

        uf = _UnionFind()
        # 边 1：共享同一 DOI 的记录直接同组
        doi_members: dict[str, list[int]] = {}
        for rec in records:
            for doi in dois_by_record.get(int(rec["id"]), set()):
                doi_members.setdefault(doi, []).append(int(rec["id"]))
        for doi, members in doi_members.items():
            uf.find(f"doi:{doi}")
            for rid in members:
                uf.union(f"doi:{doi}", f"rec:{rid}")

        # 边 2：relation DOI 指向另一记录持有的 DOI -> 同组（预印本挂接）
        known_doi_nodes = {f"doi:{d}" for d in doi_members}
        for rid, rels in rels_by_record.items():
            for doi, kind in rels:
                node = f"doi:{doi}"
                if node in known_doi_nodes:
                    uf.union(f"rec:{rid}", node)

        # 实体化 works（全量重建，保证可重复）
        conn.execute("DELETE FROM record_work")
        conn.execute("DELETE FROM works")
        groups: dict[str, list[sqlite3.Row]] = {}
        for rec in records:
            root = uf.find(f"rec:{rec['id']}")
            groups.setdefault(root, []).append(rec)

        work_count = 0
        multi_version = 0
        for root, members in sorted(groups.items(), key=lambda kv: kv[0]):
            work_count += 1
            if len(members) > 1:
                multi_version += 1
            # 规范 DOI：优先正式版（非预印本），否则取最小 DOI
            formal_dois = [r["doi"] for r in members
                           if r["doi"] and r["type"] != "preprint"]
            all_dois = sorted({d for r in members
                               for d in dois_by_record.get(int(r["id"]), set())})
            canonical = sorted(formal_dois)[0] if formal_dois else (
                all_dois[0] if all_dois else None)
            title = next((r["title"] for r in members if r["title"]), None)
            year = next((str(r["date"]) for r in members if r["date"]), None)
            cur = conn.execute(
                """INSERT INTO works(canonical_doi, title, year, created_at)
                   VALUES (?,?,?,?)""",
                (canonical, title, year, utcnow_iso()))
            work_id = int(cur.lastrowid)
            for r in sorted(members, key=lambda x: int(x["id"])):
                role = "preprint" if r["type"] == "preprint" else "version"
                conn.execute(
                    "INSERT INTO record_work(record_id, work_id, role) "
                    "VALUES (?,?,?)", (int(r["id"]), work_id, role))
        conn.commit()
        return {"works": work_count, "multi_version_works": multi_version,
                "records": len(records)}
