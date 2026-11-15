# 学者成果库（Scholar Hub）

本地全流程模拟系统：从 10 个本地 OAI-PMH 模拟仓储收割论文元数据，做 DC/MARCXML
统一映射、增量收割与断点续传、删除/更新处理、作者消歧与手工归并、预印本/正式版
挂接、学者主页与 BibTeX 导出，并用保留的真值身份计算归并精确率/召回率。

**运行期不访问任何外网**：所有 OAI-PMH 端点都是绑定在 127.0.0.1 的本地 HTTP 服务。

## 快速开始

```powershell
py -m pip install -r requirements.txt
py run_demo.py            # 生成数据 -> 启动 10 仓储 -> 两轮收割 -> 归并 -> 评估
py run_servers.py         # 单独常驻启动 10 个模拟仓储（另开终端跑 Web 时用）
py -m scholar_hub.web     # 启动 Web 界面（默认 http://127.0.0.1:8000）
py -m pytest              # 全量测试（28 项）
```

## 验收结果（默认种子）

| 指标 | 结果 | 目标 |
| --- | --- | --- |
| 成对精确率 Precision | **1.0000** | ≥ 0.90 |
| 成对召回率 Recall | **0.9597** | ≥ 0.90 |
| 成对 F1 | 0.9794 | — |
| B³ P / R / F1 | 1.0000 / 0.9587 / 0.9789 | 辅助口径 |
| 提及级准确率 | 1.0000 | — |
| 重名组成对准确率 | 0.9993 | — |
| 真值学者 / 预测学者 | 90 / 114 | 过分裂为主，无误并 |

调参记录见 `data/generated/threshold_sweep.json`，当前阈值 `PAIR_ACCEPT=0.65`。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `scholar_hub/data_gen/` | 带真值的模拟学者、论文、仓储数据生成 |
| `scholar_hub/repositories/` | 本地 OAI-PMH 仓储（含各种魔改 quirk） |
| `scholar_hub/oai/` | httpx 客户端、XML 解析、DC/MARCXML 映射、收割编排 |
| `scholar_hub/ingest/` | 幂等入库、删除、作品挂接、作者归并、BibTeX |
| `scholar_hub/evaluation/` | 精确率/召回率（成对指标 + 提及准确率） |
| `scholar_hub/web/` | Flask 五块界面 |
| `tests/` | pytest 全流程测试 |

## 关键设计

- **断点续传**：每页提交后写 checkpoint（令牌、游标、最后 datestamp）。令牌未过期直接
  续；过期则用最后成功 datestamp 重发 `ListRecords`（支持 `_cursor` 扩展的仓储直接
  从下一页游标接上），upsert 幂等保证不产生重复。
- **删除与更新**：`status=deleted` 的头标触发硬删除（审计事件保留在 `record_events`）；
  同一 `oai_identifier` 永远原地更新，版本号 +1。
- **手工归并保护**：手工合并/拆分落为 `mention_locks` 与 `pair_constraints`，重跑归并时
  锁优先，自动算法不得跨越手工约束。
- **作品挂接**：同 DOI 归一后合并；预印本通过 `dc:relation`/MARC 关联字段做并查集挂接。
