from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from purple_agent import DEDUP_KEY, PurpleAgent


def record(record_id: str, *, partner: str = "156", hs: str = "85") -> dict[str, Any]:
    return {
        "year": 2021,
        "reporter": "840",
        "partner": partner,
        "flow": "M",
        "hs": hs,
        "cmdCode": "85",
        "tradeValue": 10,
        "netWeight": 2,
        "qty": 1,
        "record_id": record_id,
    }


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")


class ScriptedSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float,
    ) -> FakeResponse:
        self.calls.append((url, params or {}))
        if not self.responses:
            raise AssertionError(f"unexpected request: {url} {params}")
        return self.responses.pop(0)


def page(
    task_id: str,
    rows: list[dict[str, Any]],
    *,
    total_rows: int,
    page_size: int,
) -> dict[str, Any]:
    return {
        "ok": True,
        "task_id": task_id,
        "total_rows": total_rows,
        "page_size": page_size,
        "data": rows,
    }


def test_canonicalize_filters_totals_and_duplicates() -> None:
    agent = PurpleAgent()
    total = record("TOTAL", partner="WLD", hs="TOTAL")
    total["isTotal"] = True
    rows, totals_dropped, duplicate_rows = agent._canonicalize(
        [record("b"), total, record("a"), record("b")]
    )

    assert [row["record_id"] for row in rows] == ["a", "b"]
    assert totals_dropped == 1
    assert duplicate_rows == 1


def test_retry_log_and_counters_reflect_real_429() -> None:
    payload = page("T4_rate_limit_429", [record("a")], total_rows=1, page_size=1)
    session = ScriptedSession([FakeResponse(429), FakeResponse(200, payload)])
    agent = PurpleAgent(session=session, sleep=lambda _: None, monotonic=lambda: 0.0)
    agent._reset("T4_rate_limit_429")

    assert agent._fetch_page("http://mock", page=1) == payload
    assert agent.request_count == 2
    assert agent.retry_count == 1
    assert agent.http_status_counts == {429: 1, 500: 0}
    assert "status=429" in "\n".join(agent.log_lines)
    assert "exponential_backoff_seconds=1" in "\n".join(agent.log_lines)


def test_fetches_server_reported_pages_without_configuring_mock() -> None:
    session = ScriptedSession(
        [
            FakeResponse(
                200,
                page(
                    "T2_multi_page",
                    [record("a"), record("b")],
                    total_rows=3,
                    page_size=2,
                ),
            ),
            FakeResponse(
                200,
                page("T2_multi_page", [record("c")], total_rows=3, page_size=2),
            ),
        ]
    )
    agent = PurpleAgent(session=session, sleep=lambda _: None, monotonic=lambda: 0.0)
    agent._reset("T2_multi_page")

    rows, totals_dropped, duplicate_rows, page_size = agent._fetch_all_rows(
        "http://mock"
    )

    assert [row["record_id"] for row in rows] == ["a", "b", "c"]
    assert totals_dropped == 0
    assert duplicate_rows == 0
    assert page_size == 2
    assert session.calls == [
        ("http://mock/records", {"page": 1}),
        ("http://mock/records", {"page": 2, "page_size": 2}),
    ]
    assert all("configure" not in url for url, _ in session.calls)


def test_live_order_instability_uses_one_full_snapshot_without_task_name() -> None:
    session = ScriptedSession(
        [
            FakeResponse(
                200,
                page(
                    "renamed_dynamic_task",
                    [record("b"), record("a")],
                    total_rows=3,
                    page_size=2,
                ),
            ),
            FakeResponse(
                200,
                page(
                    "renamed_dynamic_task",
                    [record("c"), record("b"), record("a")],
                    total_rows=3,
                    page_size=3,
                ),
            ),
        ]
    )
    agent = PurpleAgent(session=session, sleep=lambda _: None, monotonic=lambda: 0.0)
    agent._reset("renamed_dynamic_task")

    rows, _, _, _ = agent._fetch_all_rows("http://mock")

    assert [row["record_id"] for row in rows] == ["a", "b", "c"]
    assert session.calls[-1] == (
        "http://mock/records",
        {"page": 1, "page_size": 3},
    )


def test_outputs_are_contract_complete_and_canonical(tmp_path: Path) -> None:
    agent = PurpleAgent(monotonic=lambda: 3.0)
    agent._reset("T1_single_page")
    agent.request_count = 1
    agent.successful_requests = 1
    agent._log("INFO", "request_complete", request=1, page=1, status=200)

    agent._write_outputs(
        tmp_path,
        [record("b"), record("a")],
        totals_dropped=0,
        duplicate_rows=0,
        page_size=1000,
    )

    data_rows = [json.loads(line) for line in (tmp_path / "data.jsonl").read_text().splitlines()]
    metadata = json.loads((tmp_path / "metadata.json").read_text())
    run_log = (tmp_path / "run.log").read_text()
    assert [row["record_id"] for row in data_rows] == ["b", "a"]
    assert metadata["row_count"] == 2
    assert metadata["dedup_key"] == list(DEDUP_KEY)
    assert metadata["query"] == {
        "reporter": "840",
        "partner": "156",
        "flow": "M",
        "hs": "85",
        "year": 2021,
    }
    assert "event=complete" in run_log
    assert "warnings=0 errors=0" in run_log
