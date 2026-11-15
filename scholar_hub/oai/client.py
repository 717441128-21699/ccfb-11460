"""httpx 封装的 OAI-PMH 客户端。

只依赖标准 OAI-PMH 动词；对魔改仓储的兼容（x-oai-verb、503 令牌过期、
无 errorCode 错误体）在这里吸收。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


class OAIError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class OAIClientError(OAIError):
    """传输层异常（HTTP 非 200 且可推断为协议错误时使用）。"""


@dataclass
class PageResult:
    xml_text: str
    used_token: str | None
    next_token: str | None
    cursor: int
    total: int


class OAIClient:
    def __init__(self, base_url: str, quirks: list[str] | None = None,
                 timeout: float = 15.0):
        self.base_url = base_url
        self.quirks = set(quirks or [])
        # 全程只访问注册进来的本地地址
        self._http = httpx.Client(timeout=timeout, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OAIClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 底层
    def _verb_param(self) -> str:
        return "x-oai-verb" if "nonstandard_verbs" in self.quirks else "verb"

    def _get(self, params: dict[str, str], retries: int = 2) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._http.get(self.base_url, params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(0.2 * (attempt + 1))
                continue
            # 503 且携带 Retry-After：令牌过期魔改或临时过载
            if resp.status_code == 503 and "token_503" in self.quirks:
                raise OAIError("badResumptionToken",
                               "token expired (signalled via HTTP 503)")
            if resp.status_code == 503 and attempt < retries:
                time.sleep(float(resp.headers.get("Retry-After", "0.5")))
                continue
            if resp.status_code != 200:
                raise OAIClientError("httpError",
                                     f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp
        raise OAIClientError("transportError", str(last_exc))

    def identify(self) -> str:
        resp = self._get({self._verb_param(): "Identify"})
        return resp.text

    def list_records_page(self, metadata_prefix: str,
                          from_date: str | None = None,
                          until_date: str | None = None,
                          set_spec: str | None = None,
                          resumption_token: str | None = None,
                          cursor_hint: int | None = None) -> PageResult:
        params: dict[str, str] = {self._verb_param(): "ListRecords"}
        used_token = None
        if resumption_token:
            params["resumptionToken"] = resumption_token
            used_token = resumption_token
            # 支持 _cursor 扩展的仓储，令牌过期时可带游标重入
            if "cursor_only_resume" in self.quirks and cursor_hint is not None:
                params["_cursor"] = str(cursor_hint)
        else:
            params["metadataPrefix"] = metadata_prefix
            if from_date:
                params["from"] = from_date
            if until_date:
                params["until"] = until_date
            if set_spec:
                params["set"] = set_spec
            if "cursor_only_resume" in self.quirks and cursor_hint:
                params["_cursor"] = str(cursor_hint)

        resp = self._get(params)
        return PageResult(
            xml_text=resp.text,
            used_token=used_token,
            next_token=None,
            cursor=0,
            total=0,
        )
