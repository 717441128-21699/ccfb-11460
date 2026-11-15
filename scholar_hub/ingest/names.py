"""姓名归一化：中文名、拼音、英文名、缩写名统一到可比较结构。"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- 拼音词典
# 覆盖生成数据中出现的姓氏与名字用字（真实系统里会挂接完整拼音库）。

SURNAME_PINYIN: dict[str, str] = {
    "张": "Zhang", "王": "Wang", "李": "Li", "刘": "Liu", "陈": "Chen",
    "赵": "Zhao", "孙": "Sun", "周": "Zhou", "吴": "Wu", "郑": "Zheng",
    "冯": "Feng", "何": "He", "许": "Xu", "邓": "Deng", "韩": "Han",
    "曹": "Cao", "谢": "Xie", "邹": "Zou", "苏": "Su", "潘": "Pan",
    "葛": "Ge", "范": "Fan", "彭": "Peng", "蒋": "Jiang", "蔡": "Cai",
}

_GIVEN_SYLLABLES: dict[str, str] = {
    "伟": "Wei", "芳": "Fang", "强": "Qiang", "洋": "Yang", "静": "Jing",
    "磊": "Lei", "敏": "Min",
    "晨": "Chen", "曦": "Xi", "若": "Ruo", "嘉": "Jia", "懿": "Yi",
    "子": "Zi", "墨": "Mo", "雨": "Yu", "桐": "Tong", "浩": "Hao",
    "然": "Ran", "奕": "Yi", "辰": "Chen", "一": "Yi", "诺": "Nuo",
    "沐": "Mu", "汐": "Xi", "明": "Ming", "远": "Yuan", "知": "Zhi",
    "衡": "Heng", "书": "Shu", "瑶": "Yao", "景": "Jing", "行": "Xing",
    "望": "Wang", "舒": "Shu", "清": "Qing", "越": "Yue", "砚": "Yan",
    "秋": "Qiu", "临": "Lin", "川": "Chuan", "既": "Ji", "白": "Bai",
    "听": "Ting", "澜": "Lan", "照": "Zhao", "青": "Qing", "栖": "Qi",
    "迟": "Chi", "怀": "Huai", "瑾": "Jin", "见": "Jian", "山": "Shan",
    "宸": "Chen",
}

_CJK_RE = re.compile(r"[一-鿿]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def is_cjk(name: str) -> bool:
    return bool(_CJK_RE.search(name or ""))


def cjk_to_pinyin(name: str) -> tuple[str, str] | None:
    """中文名 -> (family_pinyin, given_pinyin)，无法识别返回 None。"""
    chars = [c for c in name if _CJK_RE.match(c)]
    if not chars:
        return None
    family = SURNAME_PINYIN.get(chars[0])
    if not family:
        return None
    given_parts = [_GIVEN_SYLLABLES.get(c) for c in chars[1:]]
    if not given_parts or any(p is None for p in given_parts):
        return None
    return family.lower(), "".join(given_parts).lower()


@dataclass(frozen=True)
class NameForm:
    family: str               # 小写姓氏（CJK 已转拼音）
    given: str                # 小写全名（无缩写）；未知为 ""
    initials: tuple[str, ...]  # 名的首字母序列
    cjk: str                  # 原始中文整名（无中文名时为 ""）

    @property
    def block_key(self) -> str:
        return f"{self.family}|{self.initials[0] if self.initials else '_'}"


def _norm_latin_token(tok: str) -> str:
    return re.sub(r"[^a-z]", "", tok.lower())


def parse_latin_name(raw: str) -> NameForm | None:
    if not raw or is_cjk(raw):
        return None
    text = raw.replace(",", " , ")
    tokens = [t for t in text.split() if t and t != ","]
    if not tokens:
        return None
    # 已知拼音姓氏表：用于判断 "Li Qiang" 这类名前姓后写法里哪个是姓
    known_surnames = {v.lower() for v in SURNAME_PINYIN.values()} | {
        "lin", "xu", "shen", "lu", "tang", "gu", "luo"}
    # "Family, Given ..."
    if "," in raw:
        parts = [p.strip() for p in raw.split(",", 1)]
        family = _norm_latin_token(parts[0])
        given_tokens = parts[1].split() if len(parts) > 1 else []
    else:
        initial_idx = [i for i, t in enumerate(tokens)
                       if t.endswith(".") or (len(t.strip(".")) == 1
                                              and t.strip(".").isupper())]
        if initial_idx:
            non_init = [t for i, t in enumerate(tokens) if i not in initial_idx]
            family = _norm_latin_token(non_init[-1]) if non_init else ""
            given_tokens = [tokens[i] for i in initial_idx]
        else:
            # 无逗号、无缩写：默认 "Given Family"（最后一个 token 是姓）；
            # 但若第一个 token 命中已知姓氏，则是 "Family Given" 写法
            first_norm = _norm_latin_token(tokens[0])
            if len(tokens) >= 2 and first_norm in known_surnames:
                family = first_norm
                given_tokens = tokens[1:]
            else:
                family = _norm_latin_token(tokens[-1])
                given_tokens = tokens[:-1]

    initials: list[str] = []
    given_parts: list[str] = []
    for t in given_tokens:
        clean = _norm_latin_token(t)
        if not clean:
            continue
        if t.endswith(".") or len(clean) == 1:
            initials.append(clean[0])
        else:
            given_parts.append(clean)
            initials.append(clean[0])
    if not initials and given_parts:
        initials = [g[0] for g in given_parts]
    if not family:
        return None
    return NameForm(family=family, given="".join(given_parts),
                    initials=tuple(initials), cjk="")


def normalize_name(raw: str) -> NameForm | None:
    if not raw:
        return None
    raw = raw.strip()
    if is_cjk(raw):
        py = cjk_to_pinyin(raw)
        if py is None:
            # 词典外的中文：退化为整名 block，无法跨语言比较
            compact = "".join(_CJK_RE.findall(raw))
            return NameForm(family=f"cjk:{compact}", given="",
                            initials=(), cjk=compact)
        family, given = py
        return NameForm(family=family, given=given,
                        initials=tuple(given[:1]), cjk=raw)
    return parse_latin_name(raw)


def names_compatible(a: NameForm | None, b: NameForm | None) -> bool:
    """名字本身是否可能指同一人（姓氏 + 名缩写前缀兼容）。"""
    if a is None or b is None:
        return False
    if a.family != b.family:
        return False
    # 中文整名互相比较
    if a.cjk and b.cjk:
        return a.cjk == b.cjk
    ai, bi = a.initials, b.initials
    if not ai or not bi:
        # 至少一方只有姓：需要其他证据，名字层面不拦截
        return True
    n = min(len(ai), len(bi))
    if ai[:n] != bi[:n]:
        return False
    # 全名都存在时必须一致（防止 Wei Zhang vs Wen Zhang）
    if a.given and b.given and a.given != b.given:
        return False
    return True
