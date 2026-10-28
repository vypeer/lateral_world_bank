"""World Bank API v2 client: paging, retries with backoff, HTTP-200 error bodies.

The client returns every page together with its exact response bytes so the caller can land
them verbatim. It knows nothing about storage.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, cast
from urllib.parse import urlencode

import requests

from wbpipe.config import ApiSettings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class WorldBankApiError(RuntimeError):
    """The API answered, but with an error (including HTTP 200 ``[{"message": [...]}]`` bodies)."""


class PagingError(RuntimeError):
    """Pages were inconsistent with each other (e.g. source updated mid-read, rows lost)."""


class RetriesExhausted(RuntimeError):
    """A transient failure persisted after all retries."""


class _Response(Protocol):
    status_code: int
    content: bytes
    headers: Any


class Session(Protocol):
    def get(self, url: str, timeout: float) -> _Response: ...


@dataclass(frozen=True)
class ApiPage:
    """One page of an API response, exactly as received."""

    path: str
    params: dict[str, str]
    request_url: str
    page: int
    http_status: int
    fetched_at: datetime
    body: bytes
    meta: dict[str, Any]
    rows: list[dict[str, Any]]

    @property
    def lastupdated(self) -> str | None:
        value = self.meta.get("lastupdated")
        return str(value) if value else None


class WorldBankClient:
    def __init__(
        self,
        settings: ApiSettings,
        session: Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._session = session if session is not None else cast(Session, requests.Session())
        self._sleep = sleep

    def build_url(self, path: str, params: dict[str, str]) -> str:
        # ';' (multi-country) and ':' (date range) are kept literal, as the API documents them.
        return f"{self._settings.base_url}/{path}?{urlencode(params, safe=':;')}"

    def fetch_all(self, path: str, params: dict[str, str] | None = None) -> list[ApiPage]:
        """Fetch every page of ``path`` and verify the pages add up to ``meta.total``."""
        return list(self.iter_pages(path, params))

    def iter_pages(self, path: str, params: dict[str, str] | None = None) -> Iterator[ApiPage]:
        first = self._fetch_page(path, params or {}, page=1)
        yield first
        pages, total = int(first.meta["pages"]), int(first.meta["total"])
        seen = len(first.rows)
        for page_no in range(2, pages + 1):
            page = self._fetch_page(path, params or {}, page=page_no)
            for key in ("pages", "total", "lastupdated"):
                if page.meta.get(key) != first.meta.get(key):
                    raise PagingError(
                        f"{path}: meta.{key} changed between page 1 and page {page_no} "
                        f"({first.meta.get(key)!r} -> {page.meta.get(key)!r})"
                    )
            seen += len(page.rows)
            yield page
        if seen != total:
            raise PagingError(
                f"{path}: received {seen} rows across {pages} pages, meta says {total}"
            )

    def first_page(
        self, path: str, params: dict[str, str] | None = None, per_page: int | None = None
    ) -> ApiPage:
        """Only page 1, e.g. with per_page=1 as a cheap probe of meta (total, lastupdated)."""
        return self._fetch_page(path, params or {}, page=1, per_page=per_page)

    def _fetch_page(
        self, path: str, params: dict[str, str], page: int, per_page: int | None = None
    ) -> ApiPage:
        query = {
            "format": "json",
            **params,
            "per_page": str(per_page or self._settings.per_page),
            "page": str(page),
        }
        url = self.build_url(path, query)
        status, body = self._get_with_retries(url)
        payload = json.loads(body)
        meta, rows = _split_payload(payload, url)
        if int(meta.get("page", page)) != page:
            raise PagingError(f"{url}: asked for page {page}, got page {meta.get('page')}")
        return ApiPage(
            path=path,
            params=query,
            request_url=url,
            page=page,
            http_status=status,
            fetched_at=datetime.now(timezone.utc),
            body=body,
            meta=meta,
            rows=rows,
        )

    def fetch_file(self, url: str) -> bytes:
        """A non-API download (e.g. a reference workbook), with the same retry policy."""
        return self._get_with_retries(url, expect_json=False)[1]

    def _get_with_retries(self, url: str, expect_json: bool = True) -> tuple[int, bytes]:
        attempts = self._settings.max_retries + 1
        last_error = ""
        for attempt in range(attempts):
            retry_after: float | None = None
            try:
                resp = self._session.get(url, timeout=self._settings.timeout_seconds)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200 and not expect_json:
                    return resp.status_code, resp.content
                if resp.status_code == 200:
                    try:
                        json.loads(resp.content)
                    except ValueError:
                        # Gateways occasionally return HTML with a 200; treat as transient.
                        last_error = "HTTP 200 with a non-JSON body"
                    else:
                        return resp.status_code, resp.content
                elif resp.status_code in RETRYABLE_STATUS:
                    last_error = f"HTTP {resp.status_code}"
                    retry_after = _parse_retry_after(resp.headers)
                else:
                    raise WorldBankApiError(
                        f"{url}: HTTP {resp.status_code}: {resp.content[:200]!r}"
                    )
            if attempt + 1 < attempts:
                delay = retry_after if retry_after is not None else self._backoff(attempt)
                log.warning(
                    "%s failed (%s); retry %d/%d in %.1fs",
                    url,
                    last_error,
                    attempt + 1,
                    attempts - 1,
                    delay,
                )
                self._sleep(delay)
        raise RetriesExhausted(f"{url}: giving up after {attempts} attempts ({last_error})")

    def _backoff(self, attempt: int) -> float:
        base = self._settings.backoff_seconds
        return float(base * 2**attempt + random.uniform(0, base))


def _split_payload(payload: Any, url: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (meta, rows), turning the API's in-band error format into an exception."""
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        if "message" in payload[0]:
            messages = "; ".join(
                f"{m.get('id')} {m.get('key')}: {m.get('value')}" for m in payload[0]["message"]
            )
            raise WorldBankApiError(f"{url}: {messages}")
        if len(payload) == 2 and "pages" in payload[0]:
            # An empty result set comes back as [meta, null].
            return payload[0], payload[1] or []
    raise WorldBankApiError(f"{url}: unexpected response shape: {str(payload)[:200]}")


def _parse_retry_after(headers: Any) -> float | None:
    try:
        value = headers.get("Retry-After") if headers is not None else None
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
