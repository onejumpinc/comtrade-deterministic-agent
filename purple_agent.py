"""Deterministic, model-free ComtradeBench participant.

The green agent configures the mock service before invoking this participant.
This implementation treats that live service as authoritative: it does not
carry a task table, fixture rows, expected row counts, or record identifiers.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Callable

import requests


QUERY_FIELDS = ("reporter", "partner", "flow", "hs", "year")
DEDUP_KEY = ("year", "reporter", "partner", "flow", "hs", "record_id")
RETRYABLE_STATUS_CODES = {429, 500}


class PurpleAgent:
    """Fetch, validate, canonicalize, and persist one live mock-API dataset."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session or requests.Session()
        self._sleep = sleep
        self._monotonic = monotonic
        self.task_id = "unknown"
        self.log_lines: list[str] = []
        self.request_count = 0
        self.retry_count = 0
        self.http_status_counts = {429: 0, 500: 0}
        self.successful_requests = 0
        self.warning_count = 0
        self.error_count = 0
        self._last_request_started: float | None = None
        self._started_at = 0.0

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if level == "WARN":
            self.warning_count += 1
        elif level == "ERROR":
            self.error_count += 1
        rendered_fields = " ".join(
            f"{key}={json.dumps(value, ensure_ascii=True, sort_keys=True)}"
            for key, value in fields.items()
        )
        line = f"{level}: task_id={self.task_id} event={event}"
        if rendered_fields:
            line = f"{line} {rendered_fields}"
        self.log_lines.append(line)
        print(f"[COMTRADE] {line}", flush=True)

    def _reset(self, task_id: str) -> None:
        self.task_id = task_id
        self.log_lines = []
        self.request_count = 0
        self.retry_count = 0
        self.http_status_counts = {429: 0, 500: 0}
        self.successful_requests = 0
        self.warning_count = 0
        self.error_count = 0
        self._last_request_started = None
        self._started_at = self._monotonic()

    def _wait_until_ready(self, mock_url: str, timeout_seconds: float = 20.0) -> bool:
        deadline = self._monotonic() + timeout_seconds
        while self._monotonic() < deadline:
            try:
                response = self.session.get(f"{mock_url.rstrip('/')}/docs", timeout=2)
                if response.status_code < 500:
                    self._log("INFO", "mock_ready", status=response.status_code)
                    return True
            except requests.RequestException:
                pass
            self._sleep(0.25)
        self._log("ERROR", "mock_unavailable", timeout_seconds=timeout_seconds)
        return False

    def _throttle(self) -> None:
        """Keep data requests at or below roughly three requests per second."""

        if self._last_request_started is not None:
            elapsed = self._monotonic() - self._last_request_started
            remaining = 0.34 - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_started = self._monotonic()

    @staticmethod
    def _validate_page(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("mock response is not a JSON object")
        if payload.get("ok") is not True:
            raise ValueError("mock response did not report success")
        if not isinstance(payload.get("data"), list):
            raise ValueError("mock response data is not a list")
        for row in payload["data"]:
            if not isinstance(row, dict):
                raise ValueError("mock response contains a non-object row")
        for field in ("total_rows", "page_size"):
            value = payload.get(field)
            if type(value) is not int or value < 1:
                raise ValueError(f"mock response has invalid {field}: {value!r}")
        return payload

    def _fetch_page(
        self,
        mock_url: str,
        *,
        page: int,
        page_size: int | None = None,
        max_attempts: int = 4,
    ) -> dict[str, Any]:
        params: dict[str, int] = {"page": page}
        if page_size is not None:
            params["page_size"] = page_size

        for attempt in range(1, max_attempts + 1):
            self._throttle()
            self.request_count += 1
            request_number = self.request_count
            self._log(
                "INFO",
                "request_started",
                request=request_number,
                page=page,
                page_size=page_size or "server_default",
                attempt=attempt,
            )
            try:
                response = self.session.get(
                    f"{mock_url.rstrip('/')}/records",
                    params=params,
                    timeout=15,
                )
            except requests.RequestException as exc:
                if attempt == max_attempts:
                    self._log(
                        "ERROR",
                        "request_transport_failure",
                        request=request_number,
                        page=page,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        error=type(exc).__name__,
                    )
                    raise
                self.retry_count += 1
                backoff_seconds = 2 ** (attempt - 1)
                self._log(
                    "WARN",
                    "transport_retry",
                    request=request_number,
                    page=page,
                    retry=attempt,
                    max_attempts=max_attempts,
                    exponential_backoff_seconds=backoff_seconds,
                    error=type(exc).__name__,
                )
                self._sleep(backoff_seconds)
                continue

            if response.status_code == 200:
                payload = self._validate_page(response.json())
                self.successful_requests += 1
                self._log(
                    "INFO",
                    "request_complete",
                    request=request_number,
                    page=page,
                    status=200,
                    returned_rows=len(payload["data"]),
                )
                return payload

            if response.status_code in RETRYABLE_STATUS_CODES:
                self.http_status_counts[response.status_code] += 1
                if attempt < max_attempts:
                    self.retry_count += 1
                    backoff_seconds = 2 ** (attempt - 1)
                    self._log(
                        "WARN",
                        "http_retry",
                        request=request_number,
                        page=page,
                        status=response.status_code,
                        retry=attempt,
                        max_attempts=max_attempts,
                        exponential_backoff_seconds=backoff_seconds,
                    )
                    self._sleep(backoff_seconds)
                    continue

            self._log(
                "ERROR",
                "request_failed",
                request=request_number,
                page=page,
                status=response.status_code,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            response.raise_for_status()

        raise RuntimeError("unreachable retry state")

    @staticmethod
    def _is_totals_row(row: dict[str, Any]) -> bool:
        return (
            row.get("isTotal") is True
            and row.get("partner") == "WLD"
            and row.get("hs") == "TOTAL"
        )

    @staticmethod
    def _canonical_key(row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row.get(field) for field in DEDUP_KEY)

    def _canonicalize(
        self,
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int, int]:
        totals_dropped = 0
        unique: dict[tuple[Any, ...], dict[str, Any]] = {}
        duplicate_rows = 0
        for row in rows:
            if self._is_totals_row(row):
                totals_dropped += 1
                continue
            key = self._canonical_key(row)
            if key in unique:
                duplicate_rows += 1
                continue
            unique[key] = row

        canonical_rows = sorted(
            unique.values(),
            key=lambda row: json.dumps(
                [row.get(field) for field in DEDUP_KEY],
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )
        return canonical_rows, totals_dropped, duplicate_rows

    def _page_order_is_unstable(self, rows: list[dict[str, Any]]) -> bool:
        """Detect a drifting page from the live row order, ignoring totals rows."""

        visible_rows = [row for row in rows if not self._is_totals_row(row)]
        rendered_keys = [
            json.dumps(
                [row.get(field) for field in DEDUP_KEY],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            for row in visible_rows
        ]
        return rendered_keys != sorted(rendered_keys)

    def _fetch_all_rows(self, mock_url: str) -> tuple[list[dict[str, Any]], int, int, int]:
        first_page = self._fetch_page(mock_url, page=1)
        response_task_id = first_page.get("task_id")
        if response_task_id != self.task_id:
            raise ValueError(
                f"mock task mismatch: requested {self.task_id!r}, got {response_task_id!r}"
            )

        total_rows = first_page["total_rows"]
        page_size = first_page["page_size"]
        rows = list(first_page["data"])

        # A drifting service exposes a non-canonical first-page order and can
        # return overlapping slices when later pages see a different ordering.
        # Fetch one service-authorized full snapshot when that live signal is
        # present; no task name or expected record set is consulted.
        if self._page_order_is_unstable(rows) and total_rows > page_size:
            if total_rows > 5000:
                raise ValueError("page-drift snapshot exceeds the service page-size limit")
            self._log(
                "WARN",
                "unstable_page_snapshot",
                page=1,
                request=self.request_count + 1,
                total_rows=total_rows,
            )
            snapshot = self._fetch_page(
                mock_url,
                page=1,
                page_size=total_rows,
            )
            if snapshot.get("task_id") != self.task_id:
                raise ValueError("mock task changed during snapshot recovery")
            if snapshot["total_rows"] != total_rows:
                raise ValueError("mock total_rows changed during snapshot recovery")
            rows = list(snapshot["data"])
        else:
            page_count = max(1, math.ceil(total_rows / page_size))
            for page in range(2, page_count + 1):
                payload = self._fetch_page(
                    mock_url,
                    page=page,
                    page_size=page_size,
                )
                if payload["total_rows"] != total_rows:
                    raise ValueError("mock total_rows changed during pagination")
                rows.extend(payload["data"])

        canonical_rows, totals_dropped, duplicate_rows = self._canonicalize(rows)
        if len(canonical_rows) != total_rows:
            raise ValueError(
                "live pagination did not yield the advertised dataset: "
                f"got {len(canonical_rows)} unique rows, expected {total_rows}"
            )

        return canonical_rows, totals_dropped, duplicate_rows, page_size

    @staticmethod
    def _derive_query(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            raise ValueError("cannot derive a query from an empty dataset")
        query = {field: rows[0].get(field) for field in QUERY_FIELDS}
        if any(value is None for value in query.values()):
            raise ValueError(f"dataset is missing query fields: {query!r}")
        for row in rows[1:]:
            if any(row.get(field) != query[field] for field in QUERY_FIELDS):
                raise ValueError("dataset rows do not share one query identity")
        return query

    def _write_outputs(
        self,
        output_dir: Path,
        rows: list[dict[str, Any]],
        *,
        totals_dropped: int,
        duplicate_rows: int,
        page_size: int,
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        query = self._derive_query(rows)
        schema = sorted({key for row in rows for key in row})

        data_text = "".join(
            json.dumps(
                row,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for row in rows
        )
        data_sha256 = hashlib.sha256(data_text.encode("utf-8")).hexdigest()
        execution_time = round(self._monotonic() - self._started_at, 3)

        metadata = {
            "task_id": self.task_id,
            "query": query,
            "row_count": len(rows),
            "schema": schema,
            "dedup_key": list(DEDUP_KEY),
            "sorted_by": list(DEDUP_KEY),
            "pagination_stats": {
                "paging_mode": "page",
                "page_size": page_size,
                "pages_fetched": self.successful_requests,
                "stop_reason": "complete",
            },
            "request_count": self.request_count,
            "execution_time_seconds": execution_time,
            "request_stats": {
                "requests_total": self.request_count,
                "retries_total": self.retry_count,
                "http_429": self.http_status_counts[429],
                "http_500": self.http_status_counts[500],
            },
            "retry_policy": {
                "max_attempts": 4,
                "backoff": "exponential",
                "base_seconds": 1,
            },
            "totals_handling": {
                "enabled": totals_dropped > 0,
                "rows_dropped": totals_dropped,
                "rule": "isTotal=true AND partner=WLD AND hs=TOTAL",
            },
            "deduplication": {"duplicate_rows_dropped": duplicate_rows},
            "output_hashes": {"data_sha256": data_sha256},
            "tool_versions": {"participant": "onejump-comtrade-v1"},
        }

        self._log(
            "INFO",
            "complete",
            request=self.request_count,
            page="all",
            row_count=len(rows),
            warnings=self.warning_count,
            errors=self.error_count,
        )

        files = {
            "data.jsonl": data_text,
            "metadata.json": json.dumps(
                metadata,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            "run.log": "\n".join(self.log_lines) + "\n",
        }
        for name, content in files.items():
            temporary = output_dir / f".{name}.tmp"
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(output_dir / name)

    def run(
        self,
        task_id: str,
        output_dir: str,
        mock_url: str = "http://mock-comtrade:8000",
    ) -> bool:
        """Run one task using only the green-configured live mock service."""

        self._reset(task_id)
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        for required_name in ("data.jsonl", "metadata.json", "run.log"):
            (target / required_name).unlink(missing_ok=True)

        self._log("INFO", "start", request=0, page=0, output_dir=str(target))
        try:
            if not self._wait_until_ready(mock_url):
                return False
            rows, totals_dropped, duplicate_rows, page_size = self._fetch_all_rows(
                mock_url
            )
            self._write_outputs(
                target,
                rows,
                totals_dropped=totals_dropped,
                duplicate_rows=duplicate_rows,
                page_size=page_size,
            )
            return True
        except (requests.RequestException, ValueError, OSError) as exc:
            self._log(
                "ERROR",
                "failed",
                request=self.request_count,
                page="unknown",
                error=str(exc),
            )
            return False
