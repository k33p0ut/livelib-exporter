from __future__ import annotations

import io
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

import livelib_exporter.cli as cli
from livelib_exporter.core import (
    EndpointConfig,
    ExportOptions,
    ExportResult,
    FetchResult,
    HttpConfig,
    LiveLibClient,
    LiveLibError,
    build_next_api_url,
    export_profile,
    extract_payload,
    normalize_authors,
    normalize_formats,
    normalize_rating,
    safe_filename,
    sort_records,
    validate_nickname,
)


class TextResponse:
    def __init__(
        self,
        body: str,
        url: str,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        self._body = body.encode("utf-8")
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self) -> TextResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url


def test_config_and_nickname_validation() -> None:
    with pytest.raises(ValueError):
        HttpConfig(timeout=0)
    with pytest.raises(ValueError):
        HttpConfig(retries=-1)
    with pytest.raises(ValueError):
        ExportOptions(formats=())
    with pytest.raises(ValueError):
        ExportOptions(csv_delimiter=";;")
    with pytest.raises(ValueError):
        EndpointConfig(api_root="http://example.test")
    assert validate_nickname(" reader ") == "reader"
    with pytest.raises(ValueError):
        validate_nickname("\n")


def test_client_retries_429_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    def opener(request: Any, timeout: float) -> TextResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            headers = Message()
            headers["Retry-After"] = "0"
            raise HTTPError(
                request.full_url,
                429,
                "rate limited",
                headers,
                io.BytesIO(b"slow down"),
            )
        return TextResponse("ok", request.full_url)

    client = LiveLibClient(
        http=HttpConfig(timeout=1, retries=1),
        opener=opener,
        sleeper=sleeps.append,
    )
    body, final_url = client.get_text("https://example.test/value")
    assert body == "ok"
    assert final_url == "https://example.test/value"
    assert calls == 2
    assert sleeps == [0.0]


def test_client_json_and_user_resolution_errors() -> None:
    client = LiveLibClient(
        http=HttpConfig(retries=0),
        opener=lambda request, timeout: TextResponse("not-json", request.full_url),
    )
    with pytest.raises(LiveLibError, match="non-JSON"):
        client.get_json("https://example.test/value")

    profile_client = LiveLibClient(
        http=HttpConfig(retries=0),
        endpoints=EndpointConfig(
            profile_root="https://example.test/reader/",
            api_root="https://example.test/api-root",
            site_root="https://example.test",
        ),
        opener=lambda request, timeout: TextResponse(
            '{"userId":77}', request.full_url, "text/html; charset=utf-8"
        ),
    )
    assert profile_client.resolve_user_id("reader") == 77


def test_payload_and_url_validation_errors() -> None:
    with pytest.raises(LiveLibError, match="must be a string"):
        build_next_api_url(123, api_root="https://example.test/root")
    with pytest.raises(LiveLibError, match="non-HTTPS"):
        build_next_api_url(
            "http://example.test/root/page",
            api_root="https://example.test/root",
        )
    with pytest.raises(LiveLibError, match="no object payload"):
        extract_payload({}, url="https://example.test")
    with pytest.raises(LiveLibError, match="not a list"):
        extract_payload({"payload": {"data": {}}}, url="https://example.test")
    with pytest.raises(LiveLibError, match="not an object"):
        extract_payload(
            {"payload": {"data": [], "pagination": [1]}},
            url="https://example.test",
        )


def test_small_normalizers_and_sorts() -> None:
    assert normalize_authors([{"name": "A"}, "B", None]) == "A, B"
    assert normalize_formats([{"slug": "ebook"}, 3]) == "ebook, 3"
    assert normalize_rating(4) == (4, 8, 4)
    assert normalize_rating(8) == (None, None, 8)
    assert safe_filename(" a/b:c ") == "a_b_c"

    records = [
        {"source_order": 2, "authors": "Z", "title": "B"},
        {"source_order": 1, "authors": "A", "title": "C"},
    ]
    assert [row["source_order"] for row in sort_records(records, "source")] == [1, 2]
    assert [row["source_order"] for row in sort_records(records, "title")] == [1, 2]


def test_export_profile_full_orchestration(tmp_path: Path) -> None:
    class FakeClient:
        endpoints = EndpointConfig()

        def resolve_user_id(self, nickname: str) -> int:
            assert nickname == "reader"
            return 9

        def fetch_read_books(self, user_id: int, *, progress: Any = None) -> FetchResult:
            assert user_id == 9
            if progress:
                progress(1, 2)
            return FetchResult(
                records=[
                    {
                        "id": 1,
                        "rating": 5,
                        "read_date": {"year": 2024, "month": 1, "day": 2},
                        "art_edition": {"id": 100, "title": "A"},
                    },
                    {
                        "id": 2,
                        "rating": 4,
                        "status_set_at": "2024-02-03T00:00:00Z",
                        "art_edition": {"id": 100, "title": "A duplicate"},
                    },
                ],
                raw_pages=[{"payload": {}}],
                page_count=1,
                ignored_items=0,
            )

    progress: list[tuple[int, int]] = []
    result = export_profile(
        "reader",
        client=FakeClient(),  # type: ignore[arg-type]
        options=ExportOptions(
            output_dir=tmp_path,
            formats=("json",),
            timestamped=False,
            fallback_to_status_date=True,
            deduplicate_by="book-id",
        ),
        progress=lambda page, count: progress.append((page, count)),
        exported_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )
    assert result.user_id == 9
    assert result.fetched_count == 2
    assert result.exported_count == 1
    assert result.duplicate_count == 1
    assert result.paths["json"].exists()
    assert progress == [(1, 2)]


def test_cli_success_and_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    expected = ExportResult(
        nickname="reader",
        user_id=1,
        fetched_count=1,
        exported_count=1,
        duplicate_count=0,
        ignored_items=0,
        paths={"json": tmp_path / "out.json"},
        records_without_read_date=0,
        records_without_rating=0,
    )
    monkeypatch.setattr(cli, "export_profile", lambda *args, **kwargs: expected)
    assert cli.main(["reader", "--quiet", "--formats", "json"]) == 0

    monkeypatch.setattr(
        cli,
        "export_profile",
        lambda *args, **kwargs: (_ for _ in ()).throw(LiveLibError("broken")),
    )
    assert cli.main(["reader", "--quiet"]) == 1


def test_cli_numeric_profile_is_treated_as_user_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}
    expected = ExportResult(
        nickname="121955",
        user_id=121955,
        fetched_count=1,
        exported_count=1,
        duplicate_count=0,
        ignored_items=0,
        paths={"json": tmp_path / "out.json"},
        records_without_read_date=0,
        records_without_rating=0,
        profile_identifier_type="user-id",
    )

    def fake_export_profile(*args: Any, **kwargs: Any) -> ExportResult:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(cli, "export_profile", fake_export_profile)
    assert cli.main(["121955", "--quiet", "--formats", "json"]) == 0
    assert captured["args"] == ("121955",)
    assert captured["kwargs"]["user_id"] == 121955
    assert captured["kwargs"]["profile_identifier_type"] == "user-id"


def test_numeric_profile_skips_html_lookup_and_uses_api_source(tmp_path: Path) -> None:
    class NumericClient:
        endpoints = EndpointConfig()

        def resolve_user_id(self, nickname: str) -> int:
            raise AssertionError("numeric IDs must not trigger profile HTML lookup")

        def fetch_read_books(self, user_id: int, *, progress: Any = None) -> FetchResult:
            assert user_id == 121955
            return FetchResult(
                records=[
                    {
                        "id": 1,
                        "rating": 5,
                        "art_edition": {"id": 100, "title": "A"},
                    }
                ],
                raw_pages=[{"payload": {}}],
                page_count=1,
                ignored_items=0,
            )

    result = export_profile(
        "121955",
        user_id=121955,
        profile_identifier_type="user-id",
        client=NumericClient(),  # type: ignore[arg-type]
        options=ExportOptions(
            output_dir=tmp_path,
            formats=("json",),
            timestamped=False,
        ),
        exported_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )

    document = __import__("json").loads(
        result.paths["json"].read_text(encoding="utf-8")
    )
    assert result.profile_identifier_type == "user-id"
    assert document["profile_identifier"] == "121955"
    assert document["profile_identifier_type"] == "user-id"
    assert document["username"] is None
    assert document["source"] == (
        "https://beta.api.livelib.ru/trisolaris/api/users/121955/arts/reads"
    )
