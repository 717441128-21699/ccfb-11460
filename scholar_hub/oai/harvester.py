"""收割编排：日期区间、分页、断点续传、令牌过期回退。

状态机
------
1. 启动时读取 ``harvest_checkpoints`` 与最近未完成 run：
   - 有令牌 -> 直接带 token 续传；
   - 令牌过期被服务端拒绝 -> 丢弃令牌，用最近成功页的 ``last_datestamp``
     重新发起 ListRecords（支持 ``_cursor`` 的仓储直接从下一页游标接上），
     由 upsert 幂等性兜底，重放页不会产生重复。
2. 每处理完一页立即落 checkpoint（令牌、游标、最后 datestamp）。
3. 连续令牌失败超过预算则保留现场退出，下次调用自动接着跑。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field

from ..ingest.store import IngestStore
from .client import OAIError, OAIClient
from .parser import parse_oai_response

log = logging.getLogger("scholar_hub.harvester")


@dataclass
class HarvestSummary:
    repository_id: int
    run_id: int
    pages: int = 0
    seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    token_expired: int = 0
    resumed: bool = False
    status: str = "completed"
    error: str | None = None
    actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class Harvester:
    def __init__(self, conn: sqlite3.Connection, max_token_failures: int = 3):
        self.conn = conn
        self.store = IngestStore(conn)
        self.max_token_failures = max_token_failures

    def harvest_repository(self, repo_row: sqlite3.Row,
                           from_date: str | None = None,
                           until_date: str | None = None,
                           force_fresh: bool = False) -> HarvestSummary:
        repo_id = int(repo_row["id"])
        quirks = json.loads(repo_row["quirks_json"] or "[]")
        client = OAIClient(repo_row["base_url"], quirks=quirks)
        checkpoint = None if force_fresh else self.store.get_checkpoint(repo_id)
        prior_run = None if force_fresh else self.store.last_unfinished_run(repo_id)
        resumed = checkpoint is not None
        run_id = self.store.start_run(repo_id, from_date, until_date,
                                      prior_run["id"] if prior_run else None)
        summary = HarvestSummary(repo_id, run_id, resumed=resumed)

        token = checkpoint["resumption_token"] if checkpoint else None
        cursor = int(checkpoint["cursor"]) if checkpoint else 0
        last_datestamp = checkpoint["last_datestamp"] if checkpoint else None
        # 无令牌续传：从最近成功 datestamp 的“下一条”开始（同秒记录不丢，
        # 已收割的同秒记录由 upsert 幂等跳过）
        effective_from = from_date
        if resumed and token is None and last_datestamp:
            effective_from = from_date
            if "cursor_only_resume" not in quirks:
                cursor = 0
        try:
            with client:
                self._loop(client, repo_row, repo_id, run_id, summary,
                           token=token, cursor=cursor,
                           from_date=effective_from, until_date=until_date,
                           last_datestamp=last_datestamp, quirks=quirks)
        except Exception as exc:
            message = str(exc) or repr(exc)
            summary.status = "failed"
            summary.error = message
            self.store.finish_run(run_id, "failed", message)
            log.warning("harvest failed repo=%s: %s", repo_row["code"], message)
            return summary

        self.store.finish_run(run_id, "completed")
        self.store.clear_checkpoint(repo_id)
        summary.status = "completed"
        return summary

    # ------------------------------------------------------------ 主循环
    def _loop(self, client: OAIClient, repo_row: sqlite3.Row, repo_id: int,
              run_id: int, summary: HarvestSummary, *, token: str | None,
              cursor: int, from_date: str | None, until_date: str | None,
              last_datestamp: str | None, quirks: list[str]) -> None:
        token_failures = 0
        pages_without_progress = 0
        prev_token = None
        # 重放边界：严格晚于该 datestamp 的记录才算新（同秒记录仍处理，
        # 由 upsert 幂等跳过），避免同秒分页导致漏收
        replay_after = last_datestamp if token is None else None

        while True:
            try:
                page = client.list_records_page(
                    metadata_prefix=repo_row["metadata_format"],
                    from_date=None if token else from_date,
                    until_date=None if token else until_date,
                    set_spec=None if token else repo_row["set_spec"],
                    resumption_token=token,
                    cursor_hint=cursor if "cursor_only_resume" in quirks else None,
                )
                records, next_token, page_cursor, total = parse_oai_response(
                    page.xml_text)
            except OAIError as err:
                if err.code == "noRecordsMatch":
                    log.info("repo=%s no records match window", repo_row["code"])
                    return
                if err.code in ("badResumptionToken", "unspecifiedError") \
                        and token is not None:
                    token_failures += 1
                    summary.token_expired += 1
                    if token_failures > self.max_token_failures:
                        raise
                    log.info("repo=%s 令牌过期/失效，从 from=%s 重放窗口",
                             repo_row["code"], from_date)
                    token = None
                    replay_after = last_datestamp
                    if "cursor_only_resume" not in quirks:
                        cursor = 0
                    continue
                raise

            if not records:
                return
            summary.pages += 1
            page_max_datestamp = last_datestamp
            for parsed in records:
                if replay_after and parsed.datestamp \
                        and parsed.datestamp <= replay_after:
                    # 重放窗口里早已处理过的页；同秒边界保留，交给 upsert 幂等
                    continue
                summary.seen += 1
                if parsed.is_deleted:
                    if self.store.hard_delete_record(
                            repo_id, parsed.oai_identifier, run_id):
                        summary.deleted += 1
                        summary.actions.append(
                            f"deleted {parsed.oai_identifier}")
                    continue
                action, _rid = self.store.upsert_record(
                    repo_id, parsed, run_id)
                summary.actions.append(f"{action} {parsed.oai_identifier}")
                if action == "inserted":
                    summary.inserted += 1
                elif action == "updated":
                    summary.updated += 1
                else:
                    summary.unchanged += 1
                if parsed.datestamp and (page_max_datestamp is None
                                         or parsed.datestamp > page_max_datestamp):
                    page_max_datestamp = parsed.datestamp

            last_datestamp = page_max_datestamp
            cursor = page_cursor + len(records)
            self._sync_run_counters(run_id, summary)
            # 本页成功后立刻落 checkpoint
            self.store.save_checkpoint(
                repo_id, run_id, next_token, cursor, last_datestamp, total)

            if not next_token:
                return
            if next_token == prev_token:
                pages_without_progress += 1
                if pages_without_progress >= 2:
                    raise OAIError("loopGuard",
                                   "resumption token not advancing")
            else:
                pages_without_progress = 0
            prev_token = token
            token = next_token

    def _sync_run_counters(self, run_id: int, summary: HarvestSummary) -> None:
        self.conn.execute(
            """UPDATE harvest_runs SET
                 records_seen=?, records_upserted=?, records_deleted=?,
                 pages=?, token_expired_events=?
               WHERE id=?""",
            (summary.seen, summary.inserted + summary.updated,
             summary.deleted, summary.pages, summary.token_expired, run_id))
        self.conn.commit()
