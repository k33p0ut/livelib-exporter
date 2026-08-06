from __future__ import annotations

import json
from email.message import Message
from typing import Any

from livelib_exporter.core import EndpointConfig, HttpConfig, LiveLibClient


class FakeResponse:
    def __init__(self, payload: dict[str, Any], url: str) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url


def test_client_fetches_all_pages_and_reports_progress() -> None:
    root = "https://example.test/trisolaris"
    page1_url = f"{root}/api/users/7/arts/reads"
    advertised_page2_url = "https://example.test/api/users/7/arts/reads?offset=10"
    page2_url = f"{root}/api/users/7/arts/reads?offset=10"
    pages = {
        page1_url: {
            "payload": {
                "data": [{"id": 1}, "bad-item"],
                "pagination": {"next_page": advertised_page2_url},
            }
        },
        page2_url: {
            "payload": {
                "data": [{"id": 2}],
                "pagination": {"next_page": None},
            }
        },
    }

    def opener(request: Any, timeout: float) -> FakeResponse:
        assert timeout == 5
        return FakeResponse(pages[request.full_url], request.full_url)

    sleeps: list[float] = []
    progress: list[tuple[int, int]] = []
    client = LiveLibClient(
        http=HttpConfig(timeout=5, retries=0, pause=0.25),
        endpoints=EndpointConfig(
            profile_root="https://example.test/reader/",
            api_root=root,
            site_root="https://example.test",
        ),
        opener=opener,
        sleeper=sleeps.append,
    )
    result = client.fetch_read_books(7, progress=lambda page, count: progress.append((page, count)))

    assert [row["id"] for row in result.records] == [1, 2]
    assert result.page_count == 2
    assert result.ignored_items == 1
    assert sleeps == [0.25]
    assert progress == [(1, 1), (2, 2)]


def test_client_rewrites_every_short_pagination_url_and_fetches_667_records() -> None:
    root = "https://beta.api.livelib.ru/trisolaris"
    user_id = 10811
    requested_urls: list[str] = []

    def opener(request: Any, timeout: float) -> FakeResponse:
        assert timeout == 5
        url = request.full_url
        requested_urls.append(url)
        assert url.startswith(root + "/api/users/10811/arts/reads")
        assert "https://beta.api.livelib.ru/api/" not in url

        offset_text = url.split("offset=", 1)[1] if "offset=" in url else "0"
        offset = int(offset_text)
        remaining = 667 - offset
        page_size = min(10, remaining)
        data = [{"id": offset + index + 1} for index in range(page_size)]
        next_offset = offset + page_size
        next_page = (
            f"https://beta.api.livelib.ru/api/users/{user_id}/arts/reads?offset={next_offset}"
            if next_offset < 667
            else None
        )
        payload = {
            "payload": {
                "data": data,
                "pagination": {"next_page": next_page},
            }
        }
        return FakeResponse(payload, url)

    sleeps: list[float] = []
    client = LiveLibClient(
        http=HttpConfig(timeout=5, retries=0, pause=0.0, max_pages=100),
        endpoints=EndpointConfig(
            profile_root="https://www.livelib.ru/reader/",
            api_root=root,
            site_root="https://www.livelib.ru",
        ),
        opener=opener,
        sleeper=sleeps.append,
    )

    result = client.fetch_read_books(user_id)

    assert len(result.records) == 667
    assert result.page_count == 67
    assert result.records[0]["id"] == 1
    assert result.records[-1]["id"] == 667
    assert requested_urls[1] == (
        "https://beta.api.livelib.ru/trisolaris/api/users/10811/arts/reads?offset=10"
    )
    assert requested_urls[-1].endswith("offset=660")
    assert len(sleeps) == 66
