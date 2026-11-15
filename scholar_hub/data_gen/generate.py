"""构造模拟数据集。

刻意制造的困难点：
- 张伟/王芳/李强/刘洋/陈静 等高频重名，各 6-12 个真实学者同名；
- 同一学者在不同仓储里有中文名、拼音、缩写英文名、带/不带邮箱机构等多种呈现；
- 同一论文在 1-3 个仓储重复出现，预印本与正式版 DOI 不同但互相引用；
- 第二轮收割包含更新、硬删除、新记录三类增量。
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from ..config import GEN_DATA_DIR, REPO_PORT_START, ensure_dirs
from .model import Mention, Person, Publication, RepoRecord, RepoSpec

# ---------------------------------------------------------------- 基础词表

COMMON_NAMES = [
    ("张伟", "张", "伟", "Zhang", "Wei"),
    ("王芳", "王", "芳", "Wang", "Fang"),
    ("李强", "李", "强", "Li", "Qiang"),
    ("刘洋", "刘", "洋", "Liu", "Yang"),
    ("陈静", "陈", "静", "Chen", "Jing"),
    ("王磊", "王", "磊", "Wang", "Lei"),
    ("张敏", "张", "敏", "Zhang", "Min"),
]
RARE_SURNAMES = ["赵", "孙", "周", "吴", "郑", "冯", "何", "许", "邓", "韩",
                 "曹", "谢", "邹", "苏", "潘", "葛", "范", "彭", "蒋", "蔡"]
RARE_GIVEN = ["晨曦", "若曦", "嘉懿", "子墨", "雨桐", "浩然", "奕辰", "一诺",
              "沐宸", "若汐", "明远", "知衡", "书瑶", "景行", "望舒", "清越",
              "砚秋", "临川", "既白", "听澜", "照青", "栖迟", "怀瑾", "见山"]

AFFILIATIONS = [
    "北方大学计算机学院", "北方大学电子工程学院", "东海大学数学学院",
    "东海大学生命科学学院", "南山大学物理学院", "南山大学材料学院",
    "西江大学化学学院", "西江大学经济管理学院", "云中大学医学部",
    "云中大学新闻传播学院", "国家先进计算实验室", "临海人工智能研究院",
]
AFF_EN = {
    "北方大学计算机学院": "School of Computer Science, Beifang University",
    "北方大学电子工程学院": "School of Electronic Engineering, Beifang University",
    "东海大学数学学院": "School of Mathematics, Donghai University",
    "东海大学生命科学学院": "School of Life Sciences, Donghai University",
    "南山大学物理学院": "School of Physics, Nanshan University",
    "南山大学材料学院": "School of Materials, Nanshan University",
    "西江大学化学学院": "School of Chemistry, Xijiang University",
    "西江大学经济管理学院": "School of Economics and Management, Xijiang University",
    "云中大学医学部": "Faculty of Medicine, Yunzhong University",
    "云中大学新闻传播学院": "School of Journalism, Yunzhong University",
    "国家先进计算实验室": "National Advanced Computing Laboratory",
    "临海人工智能研究院": "Linhai Institute of Artificial Intelligence",
}
AREAS = [
    "机器学习", "分布式系统", "量子计算", "生物信息", "光电子材料",
    "有机合成", "自然语言处理", "计算机视觉", "图神经网络", "密码学",
    "数据挖掘", "高性能计算", "知识图谱", "机器人", "计算金融",
]
TOPIC_TITLES = {
    "机器学习": ["面向小样本学习的{}方法研究", "基于对比学习的{}框架", "{}的泛化误差分析"],
    "分布式系统": ["高吞吐{}的一致性协议设计", "面向跨数据中心的{}调度算法", "{}容错机制与实现"],
    "量子计算": ["噪声量子硬件上的{}", "基于变分线路的{}", "{}的量子加速上界"],
    "生物信息": ["单细胞测序数据的{}分析", "蛋白质结构预测中的{}", "基因组{}流水线构建"],
    "光电子材料": ["钙钛矿薄膜的{}特性", "宽禁带半导体{}器件研究", "{}发光机理实验"],
    "有机合成": ["不对称催化下的{}合成路线", "绿色溶剂中的{}反应", "{}中间体表征与机理"],
    "自然语言处理": ["低资源语言的{}模型", "基于大语言模型的{}", "多模态{}对齐方法"],
    "计算机视觉": ["遮挡场景下的{}检测", "视频理解中的{}建模", "自监督{}表征学习"],
    "图神经网络": ["异构图上的{}消息传递", "动态网络的{}预测", "可解释{}架构"],
    "密码学": ["后量子{}签名方案", "格上{}的安全性证明", "轻量级{}协议"],
    "数据挖掘": ["用户行为序列的{}挖掘", "异质信息网络中的{}", "可扩展{}算法"],
    "高性能计算": ["众核架构上的{}优化", "混合精度{}通信压缩", "面向 E 级计算的{}"],
    "知识图谱": ["知识图谱补全的{}策略", "本体对齐中的{}融合", "时序图谱{}推理"],
    "机器人": ["移动机器人{}路径规划", "柔性夹爪的{}控制", "人机协作中的{}"],
    "计算金融": ["波动率曲面的{}建模", "高频订单流的{}预测", "信贷风险{}评估"],
}
TITLE_FILLER = ["自适应", "鲁棒", "轻量化", "多尺度", "端到端", "可解释", "联邦式", "增量式"]
PUBLISHERS = ["高等教育出版社", "科学出版社", "Springer", "Elsevier", "IEEE", "ACM"]
VENUES = {
    "journal-article": ["计算机学报", "物理学报", "Journal of AI Research", "Nature Communications"],
    "conference": ["ICML 会议论文集", "NeurIPS 会议论文集", "ACM SIGMOD 会议论文集"],
    "preprint": ["arXiv 预印本"],
    "report": ["技术报告"],
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 学者生成

def _english_variants(py_family: str, py_given: str, rng: random.Random) -> tuple[str, str | None]:
    base = f"{py_given} {py_family}"
    roll = rng.random()
    if roll < 0.25:
        alt = f"{py_given[0]}. {py_family}"
    elif roll < 0.4:
        alt = f"{py_given} {py_family[0]}."
    elif roll < 0.5:
        alt = f"{py_given.upper()} {py_family.upper()}"
    else:
        alt = None
    return base, alt


def build_persons(rng: random.Random, n_persons: int = 90) -> list[Person]:
    # 与 ingest.names 的拼音词典保持一致，保证中文名与拼音/英文名可桥接
    from ..ingest.names import _GIVEN_SYLLABLES, SURNAME_PINYIN

    persons: list[Person] = []
    pid = 1
    # 全局 (中文全名, 主机构) 唯一：保证“全名 + 机构”是可靠身份锚点
    global_name_aff: set[tuple[str, str]] = set()

    # 1) 重名组：每个常见名分配 6-12 个真实学者；同组内任何机构（含备用）
    #    都不与组内任何主机构重合，保证渲染出的“全名 + 机构”全局唯一
    for zh, fam_zh, giv_zh, py_f, py_g in COMMON_NAMES:
        group_size = rng.randint(6, 12)
        reserved_affs: set[str] = set()
        for _ in range(group_size):
            def _take_aff(pool_reserved: set[str]):
                choices = [a for a in AFFILIATIONS
                           if a not in pool_reserved
                           and (zh, a) not in global_name_aff]
                if choices:
                    return rng.choice(choices)
                # 机构数见底：优先保证全局 (全名,机构) 唯一
                global_free = [a for a in AFFILIATIONS
                               if (zh, a) not in global_name_aff]
                return rng.choice(global_free or AFFILIATIONS)
            aff = _take_aff(reserved_affs)
            reserved_affs.add(aff)
            global_name_aff.add((zh, aff))
            # 备用机构同时避开组内保留集合与全局 (全名,机构) 占用
            alt_pool = [a for a in AFFILIATIONS
                        if a not in reserved_affs
                        and (zh, a) not in global_name_aff]
            k_alt = min(rng.randint(0, 1), len(alt_pool))
            alt_affs = rng.sample(alt_pool, k=k_alt)
            reserved_affs.update(alt_affs)
            for a in alt_affs:
                global_name_aff.add((zh, a))
            areas = rng.sample(AREAS, k=rng.randint(2, 3))
            base, alt_en = _english_variants(py_f, py_g, rng)
            persons.append(Person(
                person_id=f"tp{pid:04d}",
                family_zh=fam_zh, given_zh=giv_zh,
                pinyin_family=py_f, pinyin_given=py_g,
                english_name=base, alt_english=alt_en,
                email=f"{py_g.lower()}.{py_f.lower()}{pid}@example.org",
                affiliation=aff, alt_affiliations=alt_affs, research_areas=areas,
            ))
            pid += 1

    # 2) 稀有名学者，基本不会撞名；拼音严格由汉字词典推出
    while len(persons) < n_persons:
        fam_zh = rng.choice(RARE_SURNAMES)
        giv_zh = rng.choice(RARE_GIVEN)
        zh = fam_zh + giv_zh
        py_f = SURNAME_PINYIN[fam_zh]
        py_g = "".join(_GIVEN_SYLLABLES[ch] for ch in giv_zh)
        free = [a for a in AFFILIATIONS if (zh, a) not in global_name_aff]
        aff = rng.choice(free or AFFILIATIONS)
        global_name_aff.add((zh, aff))
        base, alt_en = _english_variants(py_f, py_g, rng)
        persons.append(Person(
            person_id=f"tp{pid:04d}",
            family_zh=fam_zh, given_zh=giv_zh,
            pinyin_family=py_f, pinyin_given=py_g,
            english_name=base, alt_english=alt_en,
            email=f"{py_g.lower()}.{py_f.lower()}{pid}@example.org",
            affiliation=aff,
            alt_affiliations=rng.sample([a for a in AFFILIATIONS if a != aff],
                                        k=rng.randint(0, 2)),
            research_areas=rng.sample(AREAS, k=rng.randint(2, 3)),
        ))
        pid += 1

    # 3) 强制不变量：每个中文全名的所有真人主机构互不相同；备用机构不得
    #    撞上同名者主机构。机构池不够时宁可牺牲备用机构也要保证主机构唯一，
    #    使渲染层“全名 + 机构”成为可靠身份锚点。
    by_name: dict[str, list] = {}
    for p in persons:
        by_name.setdefault(p.family_zh + p.given_zh, []).append(p)
    for zh, group in by_name.items():
        used = {p.affiliation for p in group}
        for p in group:
            if list(used).count(p.affiliation) > 1 or len(used) != len(group):
                pass
        # 逐人重排主机构，直到组内唯一
        seen: set[str] = set()
        for p in group:
            if p.affiliation in seen:
                free = [a for a in AFFILIATIONS if a not in seen]
                if free:
                    p.affiliation = rng.choice(free)
            seen.add(p.affiliation)
        # 备用机构避开组内全部主机构
        primaries = {q.affiliation for q in group}
        for p in group:
            p.alt_affiliations = [a for a in p.alt_affiliations
                                  if a not in primaries]
    return persons


# ---------------------------------------------------------------- 论文生成

def _make_title(area: str, rng: random.Random) -> str:
    tmpl = rng.choice(TOPIC_TITLES[area])
    return tmpl.format(rng.choice(TITLE_FILLER))


def build_publications(persons: list[Person], rng: random.Random,
                       n_publications: int = 260) -> list[Publication]:
    pubs: list[Publication] = []
    by_area: dict[str, list[Person]] = {}
    for p in persons:
        for a in p.research_areas:
            by_area.setdefault(a, []).append(p)

    for i in range(1, n_publications + 1):
        first = rng.choice(persons)
        area = rng.choice(first.research_areas)
        team_pool = [q for q in by_area.get(area, persons) if q.person_id != first.person_id]
        team_size = min(rng.randint(1, 5), len(team_pool))
        teammates = rng.sample(team_pool, k=team_size)
        # 同机构合作者出现概率更高：制造稳定的合作者信号
        if rng.random() < 0.5 and team_pool:
            same_aff = [q for q in team_pool if q.affiliation == first.affiliation]
            if same_aff:
                extra = rng.choice(same_aff)
                if extra not in teammates:
                    teammates.append(extra)
        authors = [first] + teammates
        rng.shuffle(teammates)
        author_ids = [authors[0].person_id] + [a.person_id for a in authors[1:]]

        pub_type = rng.choices(
            ["journal-article", "conference", "preprint", "report"],
            weights=[45, 30, 20, 5], k=1)[0]
        venue = rng.choice(VENUES[pub_type])
        year = rng.choices([2021, 2022, 2023, 2024, 2025, 2026],
                           weights=[8, 14, 20, 24, 24, 10], k=1)[0]
        doi = f"10.{rng.randint(1000, 9999)}/pub{i:05d}"
        if pub_type == "preprint":
            doi = f"10.48550/arXiv.{rng.randint(2000, 2599)}.{rng.randint(10000, 99999)}"

        pubs.append(Publication(
            record_uid=f"pub{i:04d}",
            title=_make_title(area, rng),
            abstract=f"本研究围绕{area}问题，提出{rng.choice(TITLE_FILLER)}方法并进行了实验验证。",
            year=year,
            pub_type=pub_type,
            publisher=venue if pub_type != "journal-article" else rng.choice(PUBLISHERS),
            language=rng.choices(["zh", "en"], weights=[6, 4], k=1)[0],
            subjects=rng.sample(
                [area] + [a for a in AREAS if a != area], k=rng.randint(1, 3)
            )[: 1 + rng.randint(0, 2)],
            doi=doi,
            author_person_ids=author_ids,
        ))

    # 预印本挂接：挑一部分正式版论文，生成对应预印本（不同 DOI，互相引用）
    formal = [p for p in pubs if p.pub_type in ("journal-article", "conference")]
    chosen = rng.sample(formal, k=min(40, len(formal)))
    preprints: list[Publication] = []
    seq = len(pubs)
    for f in chosen:
        seq += 1
        pre_doi = f"10.48550/arXiv.{rng.randint(2000, 2599)}.{rng.randint(10000, 99999)}"
        preprints.append(Publication(
            record_uid=f"pub{seq:04d}",
            title=f.title, abstract=f.abstract, year=max(2020, f.year - 1),
            pub_type="preprint", publisher="arXiv 预印本",
            language=f.language, subjects=list(f.subjects),
            doi=pre_doi,
            author_person_ids=list(f.author_person_ids),
            preprint_of=f.record_uid, relation_doi=f.doi,
        ))
        # 正式版反向引用（部分平台只在正式版标 isVersionOf）
        if rng.random() < 0.7:
            f.relation_doi = pre_doi
    pubs.extend(preprints)
    return pubs


# ---------------------------------------------------------------- 名字渲染

def render_mention_name(person: Person, style: str, rng: random.Random) -> str:
    if style == "zh":
        return person.name_zh
    if style == "pinyin_space":
        return f"{person.pinyin_given} {person.pinyin_family}"
    if style == "pinyin_comma":
        return f"{person.pinyin_family}, {person.pinyin_given}"
    if style == "pinyin_initials":
        return f"{person.pinyin_given[0]}. {person.pinyin_family}"
    if style == "english_upper":
        return person.english_name.upper()
    if style == "english_alt" and person.alt_english:
        return person.alt_english
    return person.english_name


# ---------------------------------------------------------------- 仓储规格

REPO_QUIRK_POOL = {
    "short_lived_tokens": "续传令牌很快过期",
    "cursor_only_resume": "令牌过期后只认 _cursor 扩展参数",
    "token_503": "令牌过期返回 HTTP 503 而不是标准 badResumptionToken",
    "nonstandard_verbs": "动词写在 x-oai-verb 参数里",
    "marc_affiliation_in_subfield": "MARC 机构信息塞在 909 自定义字段",
    "missing_emails": "三分之一提及不带邮箱",
    "missing_affiliations": "部分提及不带机构",
    "uppercase_doi": "DOI 全大写且带 doi: 前缀",
    "relation_isversionof": "版本关系用 isVersionOf 表达",
    "extra_xml_namespace": "响应包一层自定义命名空间",
    "stale_token_no_error_code": "过期令牌返回 200 但错误体没有 errorCode",
    "zero_datestamp_on_delete": "删除记录不带 datestamp",
}


def build_repo_specs(rng: random.Random) -> list[RepoSpec]:
    plans = [
        ("ir-beifang", "北方大学机构知识库", "oai_dc", "col-faculty", 20,
         ["short_lived_tokens", "missing_emails"]),
        ("ir-donghai", "东海大学机构知识库", "marcxml", "col-thesis", 15,
         ["marc_affiliation_in_subfield", "cursor_only_resume", "missing_affiliations"]),
        ("ir-nanshan", "南山大学机构知识库", "oai_dc", "col-papers", 25,
         ["nonstandard_verbs", "uppercase_doi"]),
        ("ir-xijiang", "西江大学机构知识库", "marcxml", "col-journals", 30,
         ["token_503", "extra_xml_namespace"]),
        ("ir-yunzhong", "云中大学医学知识库", "oai_dc", "col-med", 10,
         ["stale_token_no_error_code", "zero_datestamp_on_delete", "missing_emails"]),
        ("ir-nacl", "国家先进计算实验室库", "oai_dc", "col-tech", 40,
         ["cursor_only_resume"]),
        ("jrnl-cjcs", "计算机学报期刊平台", "oai_dc", "col-articles", 25,
         ["uppercase_doi", "relation_isversionof"]),
        ("jrnl-aps", "物理学报期刊平台", "marcxml", "col-articles", 20,
         ["marc_affiliation_in_subfield", "short_lived_tokens"]),
        ("conf-icml", "国际会议镜像平台", "oai_dc", "col-conf", 50,
         ["extra_xml_namespace", "missing_affiliations"]),
        ("preprint-hub", "预印本仓储", "oai_dc", "col-preprints", 30,
         ["relation_isversionof", "nonstandard_verbs", "missing_emails"]),
    ]
    specs = []
    for i, (code, name, fmt, set_spec, page_size, quirks) in enumerate(plans):
        specs.append(RepoSpec(
            code=code, name=name,
            port=REPO_PORT_START + i,
            metadata_format=fmt, set_spec=set_spec,
            page_size=page_size, quirks=quirks,
        ))
    return specs


# ---------------------------------------------------------------- 仓储负载

def _repo_name_style(code: str, rng: random.Random) -> str:
    if code.startswith("ir-") and rng.random() < 0.6:
        return "zh"
    return rng.choice(["pinyin_space", "pinyin_comma", "pinyin_initials",
                       "english_upper", "english_alt"])


def build_repo_records(pubs: list[Publication], persons_by_id: dict[str, Person],
                       specs: list[RepoSpec], rng: random.Random
                       ) -> tuple[dict[str, list[RepoRecord]], list[dict]]:
    repo_records: dict[str, list[RepoRecord]] = {s.code: [] for s in specs}
    truth_mentions: list[dict] = []
    base_day = datetime(2026, 10, 1, 9, 0, 0)

    # 同中文全名 -> 所有真人的主机构集合；备用机构渲染若落在其中会造成
    # 异人同机构，必须回退到本人主机构
    primary_by_name: dict[str, set[str]] = {}
    for p in persons_by_id.values():
        primary_by_name.setdefault(
            p.family_zh + p.given_zh, set()).add(p.affiliation)

    def _safe_affiliation(person: Person) -> str:
        zh = person.family_zh + person.given_zh
        blocked = primary_by_name.get(zh, set()) - {person.affiliation}
        safe_alts = [a for a in person.alt_affiliations if a not in blocked]
        if safe_alts:
            return rng.choice(safe_alts)
        return person.affiliation

    def _log_mention(code: str, identifier: str, m: Mention) -> None:
        truth_mentions.append({
            "repo_code": code,
            "oai_identifier": identifier,
            "position": m.position,
            "person_id": m.person_id,
            "rendered_name": m.rendered_name,
        })

    for pub in pubs:
        # 每篇论文进 1-3 个仓储；预印本必进 preprint-hub
        candidates = [s.code for s in specs]
        if pub.pub_type == "preprint":
            targets = ["preprint-hub"]
            if rng.random() < 0.4:
                targets.append(rng.choice([c for c in candidates if c != "preprint-hub"]))
        else:
            k = rng.choices([1, 2, 3], weights=[40, 40, 20], k=1)[0]
            targets = rng.sample(candidates, k=min(k, len(candidates)))

        for code in targets:
            spec = next(s for s in specs if s.code == code)
            style = _repo_name_style(code, rng)
            mentions: list[Mention] = []
            for pos, pid in enumerate(pub.author_person_ids):
                person = persons_by_id[pid]
                rendered = render_mention_name(person, style, rng)
                email = person.email
                if "missing_emails" in spec.quirks and rng.random() < 0.34:
                    email = None
                aff = person.affiliation
                if "missing_affiliations" in spec.quirks and rng.random() < 0.3:
                    aff = None
                elif rng.random() < 0.2 and person.alt_affiliations:
                    aff = _safe_affiliation(person)
                if rng.random() < 0.15 and email:
                    # 同一人邮箱偶尔出现别名
                    email = email.replace("@example.org", "@mail.example.cn")
                m = Mention(person_id=pid, position=pos,
                            rendered_name=rendered,
                            rendered_email=email, rendered_affiliation=aff)
                mentions.append(m)

            doi_override = None
            if pub.doi and "uppercase_doi" in spec.quirks:
                doi_override = "DOI:" + pub.doi.upper()
            rec = RepoRecord(
                repo_code=code,
                oai_identifier=f"oai:{code}:{pub.record_uid}",
                record_uid=pub.record_uid,
                datestamp=_iso(base_day + timedelta(hours=rng.randint(0, 600))),
                set_spec=spec.set_spec,
                status="active",
                version=1,
                mentions=mentions,
                doi_override=doi_override,
            )
            for m in mentions:
                _log_mention(code, rec.oai_identifier, m)
            repo_records[code].append(rec)

    # 第二轮增量：更新 / 删除 / 新增
    delta_day = datetime(2026, 11, 10, 8, 0, 0)
    for code, recs in repo_records.items():
        spec = next(s for s in specs if s.code == code)
        # 更新 12%：换标题、调整一个作者的呈现（模拟元数据修订）
        for rec in rng.sample(recs, k=max(2, len(recs) // 8)):
            rec.version = 2
            rec.datestamp = _iso(delta_day + timedelta(hours=rng.randint(0, 40)))
            rec.title_override = rng.choice(["【修订版】", "（更新数据）", ""]) + \
                pubs[[p.record_uid for p in pubs].index(rec.record_uid)].title
            if rec.mentions and rng.random() < 0.5:
                m = rec.mentions[0]
                person = persons_by_id[m.person_id]
                m.rendered_name = render_mention_name(
                    person, rng.choice(["zh", "pinyin_space", "english_upper"]), rng)
        # 删除 5%
        for rec in rng.sample(recs, k=max(1, len(recs) // 20)):
            rec.status = "deleted"
            rec.version = 2
            rec.datestamp = _iso(delta_day + timedelta(hours=rng.randint(0, 40)))
        # 每个仓储新增 2 条全新记录
        for j in range(2):
            first = rng.choice(list(persons_by_id.values()))
            area = rng.choice(first.research_areas)
            uid = f"new-{code}-{j}"
            new_pub_uid = f"newpub-{code}-{j}"
            style = _repo_name_style(code, rng)
            mentions = []
            team = [first] + rng.sample(
                [p for p in persons_by_id.values() if p.person_id != first.person_id],
                k=rng.randint(1, 3))
            for pos, person in enumerate(team):
                mentions.append(Mention(
                    person_id=person.person_id, position=pos,
                    rendered_name=render_mention_name(person, style, rng),
                    rendered_email=None if "missing_emails" in spec.quirks else person.email,
                    rendered_affiliation=None if "missing_affiliations" in spec.quirks
                    else person.affiliation,
                ))
                truth_mentions.append({
                    "repo_code": code,
                    "oai_identifier": f"oai:{code}:{new_pub_uid}",
                    "position": mentions[-1].position,
                    "person_id": mentions[-1].person_id,
                    "rendered_name": mentions[-1].rendered_name,
                })
            repo_records[code].append(RepoRecord(
                repo_code=code,
                oai_identifier=f"oai:{code}:{new_pub_uid}",
                record_uid=new_pub_uid,
                datestamp=_iso(delta_day + timedelta(hours=41 + j)),
                set_spec=spec.set_spec,
                status="active", version=1, mentions=mentions,
            ))
            # 在真值出版物里也登记，便于评估
            pubs.append(Publication(
                record_uid=new_pub_uid,
                title=_make_title(area, rng),
                abstract="新增收割记录。", year=2026,
                pub_type="journal-article", publisher="新增期刊",
                language="zh", subjects=[area],
                doi=f"10.new/{code}-{j}",
                author_person_ids=[m.person_id for m in mentions],
            ))

    for recs in repo_records.values():
        recs.sort(key=lambda r: r.datestamp)
    return repo_records, truth_mentions


# ---------------------------------------------------------------- 总装

def build_dataset(seed: int = 20261115) -> dict:
    rng = random.Random(seed)
    persons = build_persons(rng)
    persons_by_id = {p.person_id: p for p in persons}
    pubs = build_publications(persons, rng)
    specs = build_repo_specs(rng)
    repo_records, truth_mentions = build_repo_records(pubs, persons_by_id, specs, rng)
    return {
        "persons": persons,
        "publications": pubs,
        "specs": specs,
        "repo_records": repo_records,
        "truth_mentions": truth_mentions,
        "affiliation_en": AFF_EN,
    }


def _dc(obj) -> dict:
    """dataclass -> 普通 dict（含嵌套）。"""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _dc(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, dict):
        return {k: _dc(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_dc(v) for v in obj]
    return obj


def write_dataset(dataset: dict, out_dir: Path | None = None) -> Path:
    ensure_dirs()
    out = out_dir or GEN_DATA_DIR
    out.mkdir(parents=True, exist_ok=True)
    with (out / "truth_persons.json").open("w", encoding="utf-8") as f:
        json.dump([_dc(p) for p in dataset["persons"]], f, ensure_ascii=False, indent=1)
    with (out / "truth_records.json").open("w", encoding="utf-8") as f:
        json.dump([_dc(p) for p in dataset["publications"]], f, ensure_ascii=False, indent=1)
    with (out / "truth_mentions.json").open("w", encoding="utf-8") as f:
        json.dump([_dc(m) for m in dataset["truth_mentions"]], f, ensure_ascii=False, indent=1)
    with (out / "affiliation_en.json").open("w", encoding="utf-8") as f:
        json.dump(dataset["affiliation_en"], f, ensure_ascii=False, indent=1)
    servers = []
    for spec in dataset["specs"]:
        servers.append({
            "code": spec.code, "name": spec.name,
            "base_url": f"http://127.0.0.1:{spec.port}/oai",
            "port": spec.port,
            "metadata_format": spec.metadata_format,
            "set_spec": spec.set_spec,
            "page_size": spec.page_size,
            "quirks": spec.quirks,
        })
        with (out / f"repo_{spec.code}.json").open("w", encoding="utf-8") as f:
            json.dump([_dc(r) for r in dataset["repo_records"][spec.code]],
                      f, ensure_ascii=False, indent=1)
    with (out / "servers.json").open("w", encoding="utf-8") as f:
        json.dump(servers, f, ensure_ascii=False, indent=1)
    return out
