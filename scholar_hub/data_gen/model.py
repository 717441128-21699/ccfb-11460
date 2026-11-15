"""生成器的数据模型（dataclass，可直接 JSON 序列化）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Person:
    person_id: str                       # tp0001 ...
    family_zh: str                       # 张
    given_zh: str                        # 伟
    pinyin_family: str                   # Zhang
    pinyin_given: str                    # Wei
    english_name: str                    # Wei Zhang（部分学者另有外文名）
    alt_english: Optional[str]           # Wei W. Zhang / W. Zhang 之类变体
    email: str
    affiliation: str
    alt_affiliations: list[str]
    research_areas: list[str]

    @property
    def name_zh(self) -> str:
        return f"{self.family_zh}{self.given_zh}"


@dataclass
class Mention:
    """一条记录里的一个作者提及，以及它在某个仓储里的呈现方式。"""
    person_id: str
    position: int
    rendered_name: str                   # 仓储里实际出现的名字串
    rendered_email: Optional[str]
    rendered_affiliation: Optional[str]


@dataclass
class Publication:
    record_uid: str                      # pub0001
    title: str
    abstract: str
    year: int
    pub_type: str                        # journal-article | conference | ...
    publisher: str
    language: str
    subjects: list[str]
    doi: Optional[str]
    author_person_ids: list[str]         # 有序
    preprint_of: Optional[str] = None    # 正式版 record_uid
    relation_doi: Optional[str] = None   # 指向另一版本的 DOI


@dataclass
class RepoRecord:
    repo_code: str
    oai_identifier: str                  # oai:repo:xxx
    record_uid: str                      # 关联的真实 Publication
    datestamp: str
    set_spec: str
    status: str                          # active | deleted
    version: int
    mentions: list[Mention] = field(default_factory=list)
    # 仓储层面的渲染细节
    title_override: Optional[str] = None
    doi_override: Optional[str] = None


@dataclass
class RepoSpec:
    code: str
    name: str
    port: int
    metadata_format: str                 # oai_dc | marcxml
    set_spec: str
    page_size: int
    quirks: list[str]
