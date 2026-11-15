"""基于 stdlib ThreadingHTTPServer 的本地 OAI-PMH 仓储。

只绑定 127.0.0.1；通过 quirk 列表模拟各类魔改行为。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import GEN_DATA_DIR
from ..data_gen.model import RepoSpec
from . import xml_builder

TOKEN_TTL_SECONDS = 1.5          # short_lived_tokens：收割端稍慢一步就会过期
TOKEN_PREFIX = "rt"


def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


class _RepoState:
    """单个仓储的内存状态：负载、令牌表。"""

    def __init__(self, spec: RepoSpec, data_dir: Path):
        self.spec = spec
        self.records = _load_json(data_dir / f"repo_{spec.code}.json")
        publications = _load_json(data_dir / "truth_records.json")
        self.publications = {p["record_uid"]: p for p in publications}
        self.aff_en = _load_json(data_dir / "affiliation_en.json")
        self.tokens: dict[str, dict] = {}
        self.lock = threading.Lock()
        self._token_seq = 0

    def issue_token(self, cursor: int, from_date: str | None,
                    until_date: str | None, set_spec: str | None) -> str:
        with self.lock:
            self._token_seq += 1
            token = f"{TOKEN_PREFIX}-{self.spec.code}-{self._token_seq}"
            ttl = TOKEN_TTL_SECONDS if "short_lived_tokens" in self.spec.quirks else 900
            self.tokens[token] = {
                "cursor": cursor,
                "from": from_date,
                "until": until_date,
                "set": set_spec,
                "expires_at": time.monotonic() + ttl,
            }
            return token

    def take_token(self, token: str) -> dict | None:
        with self.lock:
            return self.tokens.get(token)


def _filter_by_date(records: list[dict], from_date: str | None,
                    until_date: str | None) -> list[dict]:
    out = []
    for r in records:
        ds = r.get("datestamp") or ""
        if from_date and ds < from_date:
            continue
        if until_date and ds > until_date:
            continue
        out.append(r)
    return out


class OAIHandler(BaseHTTPRequestHandler):
    state: _RepoSpec = None  # 由工厂注入类属性（每个端口一个 handler 类）

    def log_message(self, *args):  # 静音
        pass

    # ------------------------------------------------------------ 工具
    def _params(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        flat = {k: v[0] for k, v in q.items()}
        quirks = self.state.spec.quirks
        if "nonstandard_verbs" in quirks:
            if "x-oai-verb" in flat and "verb" not in flat:
                flat["verb"] = flat.pop("x-oai-verb")
        return flat

    def _send(self, body: bytes, status: int = 200,
              content_type: str = "text/xml; charset=utf-8",
              retry_after: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if retry_after:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.wfile.write(body)

    def _send_oai_error(self, code: str, message: str, verb: str = "",
                        http_status: int = 200) -> None:
        quirks = self.state.spec.quirks
        if code == "badResumptionToken" and "token_503" in quirks:
            self._send(b"temporary overload", status=503,
                       content_type="text/plain", retry_after=True)
            return
        if code == "badResumptionToken" and "stale_token_no_error_code" in quirks:
            # 200 + 没有 errorCode 的错误体
            body = xml_builder.oai_envelope(
                f"<request>{xml_builder._e(verb)}</request>"
                f"<error>{xml_builder._e(message)}</error>")
            self._send(body)
            return
        self._send(xml_builder.render_error(verb, code, message))

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        try:
            params = self._params()
            verb = params.get("verb", "")
            if verb == "Identify":
                self._send(xml_builder.render_identify(self.state.spec.name))
            elif verb == "ListRecords":
                self._list_records(params)
            elif verb == "GetRecord":
                self._get_record(params)
            else:
                self._send_oai_error("badVerb", f"unknown verb {verb!r}", verb)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - 防御性
            self._send_oai_error("internalError", str(exc))

    do_POST = do_GET

    def _get_record(self, params: dict[str, str]) -> None:
        ident = params.get("identifier", "")
        rec = next((r for r in self.state.records
                    if r["oai_identifier"] == ident), None)
        if not rec:
            self._send_oai_error("idDoesNotExist", ident, "GetRecord")
            return
        body = (
            "<request>GetRecord</request><GetRecord>"
            + xml_builder.render_record(
                _as_repo_record(rec), self.state.publications[rec["record_uid"]],
                self.state.spec, self.state.aff_en)
            + "</GetRecord>"
        )
        self._send(xml_builder.oai_envelope(
            body, extra_ns="extra_xml_namespace" in self.state.spec.quirks))

    def _list_records(self, params: dict[str, str]) -> None:
        spec = self.state.spec
        token = params.get("resumptionToken")
        if token:
            info = self.state.take_token(token)
            if info is None:
                self._send_oai_error("badResumptionToken",
                                     "unknown or expired resumption token",
                                     "ListRecords")
                return
            if time.monotonic() > info["expires_at"]:
                # 过期：保留行但拒绝；cursor_only_resume 的仓储允许 _cursor 重入
                if "cursor_only_resume" in spec.quirks and "_cursor" in params:
                    info = dict(info)
                    info["cursor"] = int(params["_cursor"])
                else:
                    self._send_oai_error("badResumptionToken",
                                         "resumption token expired",
                                         "ListRecords")
                    return
            from_date, until_date, set_spec = (
                info["from"], info["until"], info["set"])
            cursor = info["cursor"]
        else:
            from_date = params.get("from")
            until_date = params.get("until")
            set_spec = params.get("set")
            cursor = int(params.get("_cursor", "0")) \
                if "cursor_only_resume" in spec.quirks else 0

        filtered = _filter_by_date(self.state.records, from_date, until_date)
        if set_spec:
            filtered = [r for r in filtered if r["set_spec"] == set_spec]
        total = len(filtered)
        page_size = spec.page_size
        page = filtered[cursor:cursor + page_size]
        next_cursor = cursor + len(page)

        if not page and cursor == 0:
            self._send_oai_error("noRecordsMatch", "no records in range",
                                 "ListRecords")
            return

        new_token = None
        if next_cursor < total:
            new_token = self.state.issue_token(next_cursor, from_date,
                                               until_date, set_spec)
        wrapped = [_as_repo_record(r) for r in page]
        body = xml_builder.render_list_records(
            wrapped, self.state.publications, spec, self.state.aff_en,
            new_token, cursor, total)
        self._send(body)


def _as_repo_record(raw: dict):
    from ..data_gen.model import Mention, RepoRecord
    return RepoRecord(
        repo_code=raw["repo_code"],
        oai_identifier=raw["oai_identifier"],
        record_uid=raw["record_uid"],
        datestamp=raw["datestamp"],
        set_spec=raw["set_spec"],
        status=raw["status"],
        version=raw["version"],
        mentions=[Mention(**m) for m in raw["mentions"]],
        title_override=raw.get("title_override"),
        doi_override=raw.get("doi_override"),
    )


def build_server(spec: RepoSpec, data_dir: Path = GEN_DATA_DIR) -> ThreadingHTTPServer:
    state = _RepoState(spec, data_dir)

    class _BoundHandler(OAIHandler):
        pass

    _BoundHandler.state = state
    httpd = ThreadingHTTPServer(("127.0.0.1", spec.port), _BoundHandler)
    httpd.repo_state = state  # type: ignore[attr-defined]
    httpd.repo_spec = spec    # type: ignore[attr-defined]
    return httpd


class ServerManager:
    """启动/停止全部模拟仓储。"""

    def __init__(self, data_dir: Path = GEN_DATA_DIR):
        self.data_dir = data_dir
        self.servers: list[ThreadingHTTPServer] = []
        self._threads: list[threading.Thread] = []

    def start(self) -> list[dict]:
        servers_json = _load_json(self.data_dir / "servers.json")
        for item in servers_json:
            spec = RepoSpec(
                code=item["code"], name=item["name"], port=item["port"],
                metadata_format=item["metadata_format"],
                set_spec=item["set_spec"], page_size=item["page_size"],
                quirks=item["quirks"],
            )
            httpd = build_server(spec, self.data_dir)
            thread = threading.Thread(
                target=httpd.serve_forever, name=f"oai-{spec.code}",
                daemon=True)
            thread.start()
            self._threads.append(thread)
            self.servers.append(httpd)
        return servers_json

    def stop(self) -> None:
        for httpd in self.servers:
            httpd.shutdown()
        for httpd in self.servers:
            httpd.server_close()
        self.servers.clear()
        self._threads.clear()

    def __enter__(self) -> "ServerManager":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
