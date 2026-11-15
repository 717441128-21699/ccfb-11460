"""作者消歧与归并。

信号（同 block、姓名兼容的提及对）：
  邮箱相同        强信号（归一化域别名后比较）
  机构相同        利用中英机构别名表 + RapidFuzz 兜底
  合作者重叠      按合作者 block_key 跨仓储匹配
  研究方向重叠    记录主题词
  全名一致        加分项

聚类采用带 cannot_link 约束的平均链接凝聚聚类；手工锁与 must_link 在聚类
结果上强制生效。persons 行稳定保留，重跑只重新分配 mention -> person，
因此手工结果不会被后续收割冲掉。
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from rapidfuzz import fuzz

from ..config import GEN_DATA_DIR, PAIR_ACCEPT
from .names import NameForm, names_compatible, normalize_name
from .store import utcnow_iso

EMAIL_DOMAIN_ALIASES = {"mail.example.cn": "example.org"}


# ---------------------------------------------------------------- 工具

def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    value = email.strip().lower()
    if "@" not in value:
        return None
    local, domain = value.rsplit("@", 1)
    domain = EMAIL_DOMAIN_ALIASES.get(domain, domain)
    return f"{local}@{domain}"


def _norm_text(text: str | None) -> str:
    return re.sub(r"[\s,，。.;；·\-_/（）()]+", "", (text or "").lower())


class AffiliationIndex:
    """机构别名组：同一机构的中文名/英文名归到同一组 id。"""

    def __init__(self, alias_map: dict[str, str] | None = None):
        self._group_of: dict[str, int] = {}
        next_id = 0
        for zh, en in (alias_map or {}).items():
            key_zh, key_en = _norm_text(zh), _norm_text(en)
            existing = self._group_of.get(key_zh)
            if existing is None:
                existing = next_id
                next_id += 1
            self._group_of[key_zh] = existing
            self._group_of[key_en] = existing

    def same(self, a: str | None, b: str | None) -> bool:
        """严格同机构：归一化后相等，或属于同一中英别名组。

        刻意不做模糊兜底——“北方大学计算机学院”与“北方大学电子工程学院”
        共享大量 token，模糊匹配会把不同学院误判为同机构。
        """
        if not a or not b:
            return False
        ka, kb = _norm_text(a), _norm_text(b)
        if ka == kb:
            return True
        ga, gb = self._group_of.get(ka), self._group_of.get(kb)
        return ga is not None and ga == gb


# ---------------------------------------------------------------- 数据装配

class MentionContext:
    def __init__(self, row: sqlite3.Row, subjects: list[str],
                 coauthor_blocks: set[str], name_form: NameForm | None):
        self.id = int(row["id"])
        self.mention_key = row["mention_key"]
        self.record_id = int(row["record_id"])
        self.raw_name = row["raw_name"]
        self.email = normalize_email(row["email"])
        self.affiliation = row["affiliation"]
        self.person_id = row["person_id"]
        self.subjects = set(subjects)
        self.coauthor_blocks = coauthor_blocks
        self.form = name_form


def load_mentions(conn: sqlite3.Connection) -> list[MentionContext]:
    rows = conn.execute(
        "SELECT * FROM record_creators ORDER BY id").fetchall()
    subj_map: dict[int, list[str]] = defaultdict(list)
    for s in conn.execute("SELECT record_id, subject FROM record_subjects"):
        subj_map[int(s["record_id"])].append(s["subject"])
    coauthors: dict[int, set[str]] = defaultdict(set)
    for rid, in conn.execute("SELECT DISTINCT record_id FROM record_creators"):
        peers = conn.execute(
            "SELECT raw_name FROM record_creators WHERE record_id=?",
            (rid,)).fetchall()
        blocks = set()
        for p in peers:
            form = normalize_name(p["raw_name"])
            if form:
                blocks.add(form.block_key)
        coauthors[int(rid)] = blocks

    out: list[MentionContext] = []
    for row in rows:
        form = normalize_name(row["raw_name"])
        rid = int(row["record_id"])
        own_block = form.block_key if form else None
        peer_blocks = {b for b in coauthors[rid] if b != own_block}
        out.append(MentionContext(row, subj_map.get(rid, []),
                                  peer_blocks, form))
    return out


# ---------------------------------------------------------------- 并查集

class _UF:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


# ---------------------------------------------------------------- 归并器

class Disambiguator:
    def __init__(self, conn: sqlite3.Connection,
                 accept_threshold: float = PAIR_ACCEPT,
                 common_block_size: int = 50,
                 aff_alias_path: Path | None = None):
        self.conn = conn
        self.threshold = accept_threshold
        self.common_block_size = common_block_size
        # run() 时填充：当前这批数据里的常见重名 block，需要更强证据
        self._common_blocks: set[str] = set()
        path = aff_alias_path or (GEN_DATA_DIR / "affiliation_en.json")
        aliases = {}
        if Path(path).exists():
            with Path(path).open("r", encoding="utf-8") as f:
                aliases = json.load(f)
        self.aff_index = AffiliationIndex(aliases)

    # ------------------------------------------------------ 成对评分
    def score_pair(self, a: MentionContext, b: MentionContext,
                   block_key: str | None = None) -> float:
        """按名字常见度分层的证据评分。

        实测分布（对真值）：邮箱一致、全名+同机构这两类强证据的异人误判为 0，
        而“合作者重合”在同领域重名者之间大量出现，单独使用不可信。因此：
        - 常见重名 block 只接受身份级证据（邮箱 / 全名+同机构 / 全名+机构+信号）；
        - 稀有名 block（block 内只有一个真人）才允许合作者信号兜底召回；
        - 缩写名对必须有机构锚点，且在重名 block 要求更严。
        """
        if not names_compatible(a.form, b.form):
            return 0.0
        if a.email and b.email and a.email == b.email:
            return 1.0

        aff = self.aff_index.same(a.affiliation, b.affiliation)
        co = len(a.coauthor_blocks & b.coauthor_blocks)
        subj = len(a.subjects & b.subjects)
        both_full = bool(
            a.form and b.form and a.form.given and b.form.given
            and a.form.given == b.form.given)
        is_common = block_key in self._common_blocks

        if is_common:
            # 常见 block 里缩写名（Y. Sun）会在多个不同真人之间产生桥接，
            # 并查集的传递闭包会把整串人并成一个，因此缩写名对只认邮箱。
            if not both_full:
                return 0.0
            # 全名对：机构是可靠的身份锚点（全局 全名+机构 唯一）
            if aff:
                return 0.92
            return 0.0

        # 稀有名 block：block 内现实中只有一个真人，可放心用弱信号召回
        if both_full:
            if aff:
                return 0.88
            if co >= 1:
                return 0.72
            if subj >= 1:
                return 0.55
            return 0.0
        # 缩写名对（即使在稀有 block）：单机构不足为凭，必须机构 + 第二信号，
        # 避免 Min Z. 这类缩写在同机构/同领域异人之间桥接
        if aff and co >= 1:
            return 0.70
        if aff and subj >= 1:
            return 0.62
        if co >= 2:
            return 0.58
        return 0.0

    # ------------------------------------------------------ 主流程
    def run(self) -> dict:
        mentions = load_mentions(self.conn)
        if not mentions:
            return {"mentions": 0, "persons": 0, "clusters": 0}

        locks = {r["mention_key"]: int(r["person_id"])
                 for r in self.conn.execute("SELECT * FROM mention_locks")}
        must_link, cannot_link = self._load_constraints()

        # block 分组：同 family + 首字母才可能同 person
        blocks: dict[str, list[int]] = defaultdict(list)
        for idx, m in enumerate(mentions):
            key = m.form.block_key if m.form else f"raw:{_norm_text(m.raw_name)}"
            blocks[key].append(idx)
        self._common_blocks = {
            key for key, members in blocks.items()
            if len(members) >= self.common_block_size}

        uf = _UF(len(mentions))
        merge_log: list[tuple[str, str, float]] = []
        for block_key, block_members in blocks.items():
            self._cluster_block(block_key, block_members, mentions, uf,
                                cannot_link, merge_log)

        # 第二阶段：把因缺机构/邮箱而孤立的提及指派给同 block 内最匹配的簇。
        # 用簇级聚合证据（簇内任一成员强匹配即吸入），迭代到收敛，提高召回。
        self._assign_orphans(blocks, mentions, uf, cannot_link, merge_log)

        # must_link（手工合并）：强制联通对应 person 下的全部提及
        person_to_indices: dict[int, list[int]] = defaultdict(list)
        for idx, m in enumerate(mentions):
            if m.person_id is not None:
                person_to_indices[int(m.person_id)].append(idx)
        for pa, pb in must_link:
            ia, ib = person_to_indices.get(pa), person_to_indices.get(pb)
            if ia and ib:
                for x in ia[1:]:
                    uf.union(ia[0], x)
                for x in ib:
                    uf.union(ia[0], x)

        # 提及锁：同一 cluster 里若锁向不同 person，按锁拆分 cluster
        cluster_members: dict[int, list[int]] = defaultdict(list)
        for idx in range(len(mentions)):
            cluster_members[uf.find(idx)].append(idx)
        forced_person_of_cluster = self._apply_locks(
            cluster_members, mentions, locks)

        # cluster -> 稳定 person_id
        assignment = self._assign_persons(
            cluster_members, mentions, forced_person_of_cluster, locks)
        self._write_back(mentions, cluster_members, assignment)
        return {
            "mentions": len(mentions),
            "persons": self.conn.execute(
                "SELECT COUNT(*) AS n FROM persons").fetchone()["n"],
            "clusters": len(cluster_members),
            "merges": len(merge_log),
        }

    # ------------------------------------------------------ 孤立提及指派
    def _assign_orphans(self, blocks: dict[str, list[int]],
                        mentions: list[MentionContext], uf: _UF,
                        cannot_link: set[tuple[int, int]],
                        merge_log: list) -> None:
        """把自成单元素簇的提及，吸入同 block 内证据最强的多元素簇。

        安全策略：
        - 只把单元素簇并入多元素簇，不合并两个多元素簇（避免错误扩散）；
        - 取孤立提及与候选簇成员的最高匹配分，且要求达到指派阈值；
        - 邮箱一致直接吸入；其余按 score_pair 的分层规则；
        - cannot_link 拦截；迭代至多 4 轮直至没有新指派。
        """
        assign_threshold = max(0.55, self.threshold)
        for _ in range(4):
            moved = False
            for block_key, members in blocks.items():
                # 当前簇结构
                clusters: dict[int, list[int]] = defaultdict(list)
                for idx in members:
                    clusters[uf.find(idx)].append(idx)
                singleton_roots = [r for r, grp in clusters.items()
                                   if len(grp) == 1]
                multi_roots = [r for r, grp in clusters.items()
                               if len(grp) > 1]
                if not singleton_roots or not multi_roots:
                    continue
                for sr in singleton_roots:
                    orphan_idx = clusters[sr][0]
                    orphan = mentions[orphan_idx]
                    best: tuple[float, int] | None = None
                    for mr in multi_roots:
                        if self._cluster_blocked(orphan, clusters[mr],
                                                  mentions, cannot_link, uf):
                            continue
                        top = max(
                            (self._assign_score(orphan, mentions[k], block_key)
                             for k in clusters[mr]),
                            default=0.0)
                        if top >= assign_threshold and (
                                best is None or top > best[0]):
                            best = (top, mr)
                    if best is not None:
                        uf.union(sr, best[1])
                        merge_log.append(
                            (orphan.mention_key, f"assign@{best[1]}",
                             round(best[0], 3)))
                        moved = True
            if not moved:
                break

    def _cluster_blocked(self, orphan: MentionContext, target_indices: list[int],
                         mentions: list[MentionContext],
                         cannot_link: set[tuple[int, int]], uf: _UF) -> bool:
        if orphan.person_id is None:
            return False
        target_persons = {mentions[k].person_id for k in target_indices
                          if mentions[k].person_id is not None}
        return any(
            tuple(sorted((orphan.person_id, pb))) in cannot_link
            for pb in target_persons)

    def _assign_score(self, orphan: MentionContext, anchor: MentionContext,
                      block_key: str) -> float:
        """孤立提及 -> 簇锚点成员的保守指派分。

        与 score_pair 的区别：常见 block 里缩写名孤立提及允许通过邮箱，
        或“全名锚点 + 同机构 + 合作者”归位，但纯机构仍不足以合并，
        防止缩写名在不同真人之间桥接。
        """
        if not names_compatible(orphan.form, anchor.form):
            return 0.0
        if orphan.email and anchor.email and orphan.email == anchor.email:
            return 1.0
        base = self.score_pair(orphan, anchor, block_key)
        if base > 0:
            return base
        # 缩写名孤儿 + 全名锚点：机构之外还需合作者或方向佐证
        orphan_full = bool(orphan.form and orphan.form.given)
        anchor_full = bool(anchor.form and anchor.form.given)
        if not orphan_full and anchor_full:
            aff = self.aff_index.same(orphan.affiliation, anchor.affiliation)
            co = len(orphan.coauthor_blocks & anchor.coauthor_blocks)
            subj = len(orphan.subjects & anchor.subjects)
            if aff and co >= 1:
                return 0.70
            if aff and subj >= 1:
                return 0.64
            if co >= 2 and subj >= 1:
                return 0.60
        return 0.0

    # ------------------------------------------------------ 块内聚类
    def _cluster_block(self, block_key: str, members: list[int],
                       mentions: list[MentionContext], uf: _UF,
                       cannot_link: set[tuple[int, int]],
                       merge_log: list[tuple[str, str, float]]) -> None:
        """单遍并查集聚类：边只算一次，按分数降序处理，O(n²)。

        两个簇合并的条件是“当前边分数达标”，并查集天然做传递闭包；
        cannot_link 用现有 person 归属拦截，避免跨越手工拆分。
        """
        n = len(members)
        if n < 2:
            return
        edges: list[tuple[float, int, int]] = []
        for i in range(n):
            for j in range(i + 1, n):
                s = self.score_pair(mentions[members[i]], mentions[members[j]],
                                    block_key)
                if s >= self.threshold:
                    edges.append((s, i, j))
        edges.sort(reverse=True)

        for score, i, j in edges:
            ri, rj = uf.find(members[i]), uf.find(members[j])
            if ri == rj:
                continue
            # cannot_link 检查：两个簇里现存的手工 person 是否冲突
            persons_i = {mentions[k].person_id for k in members
                         if uf.find(k) == ri and mentions[k].person_id is not None}
            persons_j = {mentions[k].person_id for k in members
                         if uf.find(k) == rj and mentions[k].person_id is not None}
            if any(tuple(sorted((pa, pb))) in cannot_link
                   for pa in persons_i for pb in persons_j):
                continue
            uf.union(ri, rj)
            merge_log.append(
                (mentions[members[i]].mention_key,
                 mentions[members[j]].mention_key, round(score, 3)))

    # ------------------------------------------------------ 约束/锁
    def _load_constraints(self) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
        must, cannot = set(), set()
        for row in self.conn.execute("SELECT * FROM pair_constraints"):
            pair = tuple(sorted((int(row["person_a"]), int(row["person_b"]))))
            (must if row["kind"] == "must_link" else cannot).add(pair)
        return must, cannot

    def _apply_locks(self, cluster_members: dict[int, list[int]],
                     mentions: list[MentionContext],
                     locks: dict[str, int]) -> dict[int, int]:
        """检查每个 cluster 的锁；锁向不同 person 时拆 cluster。"""
        forced: dict[int, int] = {}
        for root, members in list(cluster_members.items()):
            locked = {locks[mentions[i].mention_key] for i in members
                      if mentions[i].mention_key in locks}
            if len(locked) <= 1:
                if locked:
                    forced[root] = next(iter(locked))
                continue
            # 按锁目标拆成多个子 cluster
            groups: dict[int, list[int]] = defaultdict(list)
            unlocked: list[int] = []
            for i in members:
                key = mentions[i].mention_key
                if key in locks:
                    groups[locks[key]].append(i)
                else:
                    unlocked.append(i)
            # 未锁定提及跟随第一个锁组（评分最高的组在 _assign_persons 再校正）
            first_person = sorted(groups)[0]
            groups[first_person].extend(unlocked)
            del cluster_members[root]
            for pid, grp in groups.items():
                new_root = min(grp)
                cluster_members[new_root] = grp
                forced[new_root] = pid
        return forced

    def _assign_persons(self, cluster_members: dict[int, list[int]],
                        mentions: list[MentionContext],
                        forced: dict[int, int],
                        locks: dict[str, int]) -> dict[int, int]:
        assignment: dict[int, int] = {}
        for root, members in cluster_members.items():
            if root in forced:
                assignment[root] = forced[root]
                continue
            prior = Counter(mentions[i].person_id for i in members
                            if mentions[i].person_id is not None)
            if prior:
                assignment[root] = prior.most_common(1)[0][0]
            else:
                assignment[root] = self._create_person(mentions, members)
        return assignment

    def _create_person(self, mentions: list[MentionContext],
                       members: list[int]) -> int:
        names = Counter(mentions[i].raw_name for i in members)
        emails = Counter(mentions[i].email for i in members
                         if mentions[i].email)
        affs = Counter(mentions[i].affiliation for i in members
                       if mentions[i].affiliation)
        cur = self.conn.execute(
            """INSERT INTO persons(display_name, canonical_email,
                 preferred_affiliation, kind, created_at, updated_at)
               VALUES (?,?,?, 'auto', ?, ?)""",
            (names.most_common(1)[0][0],
             emails.most_common(1)[0][0] if emails else None,
             affs.most_common(1)[0][0] if affs else None,
             utcnow_iso(), utcnow_iso()))
        self.conn.commit()
        return int(cur.lastrowid)

    def _write_back(self, mentions: list[MentionContext],
                    cluster_members: dict[int, list[int]],
                    assignment: dict[int, int]) -> None:
        touched_persons: set[int] = set()
        for root, members in cluster_members.items():
            pid = assignment[root]
            touched_persons.add(pid)
            for idx in members:
                self.conn.execute(
                    "UPDATE record_creators SET person_id=? WHERE id=?",
                    (pid, mentions[idx].id))
        for pid in touched_persons:
            self._refresh_person(pid)
        self.conn.commit()

    # ------------------------------------------------------ 手工操作
    def assign_mention(self, mention_key: str, person_id: int,
                       note: str | None = None) -> None:
        row = self.conn.execute(
            "SELECT id FROM persons WHERE id=?", (person_id,)).fetchone()
        if row is None:
            raise ValueError(f"person {person_id} 不存在")
        self.conn.execute(
            """INSERT INTO mention_locks(mention_key, person_id, locked_at, note)
               VALUES (?,?,?,?)
               ON CONFLICT(mention_key) DO UPDATE SET
                 person_id=excluded.person_id, locked_at=excluded.locked_at,
                 note=excluded.note""",
            (mention_key, person_id, utcnow_iso(), note))
        self.conn.execute(
            "UPDATE record_creators SET person_id=? WHERE mention_key=?",
            (person_id, mention_key))
        self._refresh_person(person_id)
        self.conn.commit()

    def merge_persons(self, keep_id: int, drop_id: int,
                      note: str | None = None) -> None:
        if keep_id == drop_id:
            return
        self.conn.execute(
            "UPDATE record_creators SET person_id=? WHERE person_id=?",
            (keep_id, drop_id))
        # 先把涉及 drop_id 的约束迁移到 keep_id（指向仍然存在的 person）
        old_constraints = self.conn.execute(
            "SELECT * FROM pair_constraints WHERE person_a=? OR person_b=?",
            (drop_id, drop_id)).fetchall()
        self.conn.execute(
            "DELETE FROM pair_constraints WHERE person_a=? OR person_b=?",
            (drop_id, drop_id))
        for row in old_constraints:
            other = row["person_b"] if int(row["person_a"]) == drop_id \
                else row["person_a"]
            if int(other) in (keep_id, drop_id):
                continue
            pair = sorted((keep_id, int(other)))
            self.conn.execute(
                """INSERT OR IGNORE INTO pair_constraints
                   (person_a, person_b, kind, created_at, note)
                   VALUES (?,?,?,?,?)""",
                (pair[0], pair[1], row["kind"], utcnow_iso(),
                 row["note"] or note))
        # 提及锁迁移到保留的 person
        self.conn.execute(
            "UPDATE mention_locks SET person_id=? WHERE person_id=?",
            (keep_id, drop_id))
        self.conn.execute("UPDATE persons SET kind='manual', updated_at=? WHERE id=?",
                          (utcnow_iso(), keep_id))
        self.conn.execute("DELETE FROM persons WHERE id=?", (drop_id,))
        self._refresh_person(keep_id)
        self.conn.commit()

    def split_person(self, person_id: int, mention_keys: list[str],
                     note: str | None = None) -> int:
        if not mention_keys:
            raise ValueError("拆分至少要选择一个提及")
        placeholders = ",".join("?" for _ in mention_keys)
        rows = self.conn.execute(
            f"SELECT * FROM record_creators WHERE mention_key "
            f"IN ({placeholders})", mention_keys).fetchall()
        if not rows:
            raise ValueError("未找到指定提及")
        names = Counter(r["raw_name"] for r in rows)
        emails = Counter(normalize_email(r["email"]) for r in rows if r["email"])
        affs = Counter(r["affiliation"] for r in rows if r["affiliation"])
        cur = self.conn.execute(
            """INSERT INTO persons(display_name, canonical_email,
                 preferred_affiliation, kind, created_at, updated_at)
               VALUES (?,?,?, 'manual', ?, ?)""",
            (names.most_common(1)[0][0],
             emails.most_common(1)[0][0] if emails else None,
             affs.most_common(1)[0][0] if affs else None,
             utcnow_iso(), utcnow_iso()))
        new_id = int(cur.lastrowid)
        self.conn.execute(
            f"UPDATE record_creators SET person_id=? "
            f"WHERE mention_key IN ({placeholders})",
            [new_id, *mention_keys])
        for key in mention_keys:
            self.conn.execute(
                """INSERT INTO mention_locks(mention_key, person_id, locked_at, note)
                   VALUES (?,?,?,?)
                   ON CONFLICT(mention_key) DO UPDATE SET
                     person_id=excluded.person_id,
                     locked_at=excluded.locked_at""",
                (key, new_id, utcnow_iso(), note or "手工拆分"))
        self.conn.execute(
            """INSERT OR IGNORE INTO pair_constraints
               (person_a, person_b, kind, created_at, note)
               VALUES (?,?,'cannot_link',?,?)""",
            (min(person_id, new_id), max(person_id, new_id),
             utcnow_iso(), note or "手工拆分"))
        self.conn.execute("UPDATE persons SET kind='manual', updated_at=? WHERE id=?",
                          (utcnow_iso(), person_id))
        self._refresh_person(person_id)
        self._refresh_person(new_id)
        self.conn.commit()
        return new_id

    def _refresh_person(self, person_id: int) -> None:
        rows = self.conn.execute(
            "SELECT raw_name, email, affiliation FROM record_creators "
            "WHERE person_id=?", (person_id,)).fetchall()
        if not rows:
            return
        names = Counter(r["raw_name"] for r in rows)
        emails = Counter(normalize_email(r["email"]) for r in rows
                         if r["email"])
        affs = Counter(r["affiliation"] for r in rows if r["affiliation"])
        self.conn.execute(
            """UPDATE persons SET display_name=?, canonical_email=?,
                 preferred_affiliation=?, updated_at=? WHERE id=?""",
            (names.most_common(1)[0][0],
             emails.most_common(1)[0][0] if emails else None,
             affs.most_common(1)[0][0] if affs else None,
             utcnow_iso(), person_id))
