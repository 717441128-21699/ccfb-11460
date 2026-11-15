"""模拟数据生成：学者、论文、仓储负载，保留真实身份用于评估。

生成产物（data/generated/）：
  truth_persons.json   —— 真实学者档案（真值）
  truth_records.json   —— 真实论文 + 作者顺序（真值）
  truth_mentions.json  —— 每个仓储里每条记录每个作者提及 -> 真实 person_id
  servers.json         —— 10 个仓储的注册信息与 quirk 清单
  repo_<code>.json     —— 每个仓储的 OAI 记录负载（含删除标记/更新版本）
"""
from __future__ import annotations

from .model import (
    Mention,
    Person,
    Publication,
    RepoRecord,
    RepoSpec,
)
from .generate import build_dataset, write_dataset

__all__ = [
    "Mention",
    "Person",
    "Publication",
    "RepoRecord",
    "RepoSpec",
    "build_dataset",
    "write_dataset",
]
