from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from livelib_exporter.core import (
    EndpointConfig,
    ExportOptions,
    LiveLibError,
    build_next_api_url,
    deduplicate_records,
    iso_date_part,
    normalize_read_date,
    normalize_record,
    resolve_user_id_from_html,
    retry_delay,
    sort_records,
    write_export,
)


def test_resolve_user_id_from_html_multiple_shapes() -> None:
    assert (
        resolve_user_id_from_html(
            '{"userId":10811}',
            final_url="https://www.livelib.ru/reader/example",
            nickname="example",
        )
        == 10811
    )
    assert (
        resolve_user_id_from_html(
            "no embedded id",
            final_url="https://www.livelib.ru/users/42/",
            nickname="example",
        )
        == 42
    )


def test_resolve_user_id_rejects_captcha() -> None:
    with pytest.raises(LiveLibError, match="CAPTCHA"):
        resolve_user_id_from_html(
            "captcha",
            final_url="https://www.livelib.ru/reader/example",
            nickname="example",
        )


def test_next_page_canonicalizes_both_observed_livelib_api_paths() -> None:
    root = "https://beta.api.livelib.ru/trisolaris"
    canonical = "https://beta.api.livelib.ru/trisolaris/api/users/10811/arts/reads?offset=10"

    assert build_next_api_url(
        "/trisolaris/api/users/10811/arts/reads?offset=10",
        api_root=root,
    ) == canonical
    assert build_next_api_url(
        "https://beta.api.livelib.ru/api/users/10811/arts/reads?offset=10",
        api_root=root,
    ) == canonical
    assert build_next_api_url(
        "/api/users/10811/arts/reads?offset=10",
        api_root=root,
    ) == canonical


def test_next_page_resolves_query_only_link_against_current_page() -> None:
    root = "https://beta.api.livelib.ru/trisolaris"
    current = "https://beta.api.livelib.ru/trisolaris/api/users/10811/arts/reads?offset=10"
    assert build_next_api_url(
        "?offset=20",
        api_root=root,
        current_url=current,
    ) == "https://beta.api.livelib.ru/trisolaris/api/users/10811/arts/reads?offset=20"


def test_next_page_is_constrained_to_api_origin_and_paths() -> None:
    root = "https://beta.api.livelib.ru/trisolaris"
    with pytest.raises(LiveLibError, match="another host"):
        build_next_api_url("https://example.com/api/page/2", api_root=root)
    with pytest.raises(LiveLibError, match="outside LiveLib API path"):
        build_next_api_url("https://beta.api.livelib.ru/other", api_root=root)
    with pytest.raises(LiveLibError, match="outside LiveLib API path"):
        build_next_api_url("https://beta.api.livelib.ru/apiary/page/2", api_root=root)
    with pytest.raises(LiveLibError, match="path traversal"):
        build_next_api_url(
            "https://beta.api.livelib.ru/api/%2e%2e/private", api_root=root
        )
    with pytest.raises(LiveLibError, match="fragment"):
        build_next_api_url("https://beta.api.livelib.ru/api/page/2#x", api_root=root)


def test_retry_after_seconds_and_http_date() -> None:
    now = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)
    assert retry_delay("7", attempt=0, now=now, max_backoff=30) == 7
    assert retry_delay(
        "Thu, 06 Aug 2026 00:00:12 GMT",
        attempt=0,
        now=now,
        max_backoff=30,
    ) == 12
    assert retry_delay("broken", attempt=3, now=now, max_backoff=30) == 8


def test_normalize_read_date_precision() -> None:
    assert normalize_read_date({"year": 2024, "month": 11, "day": 2}).value == "2024-11-02"
    assert normalize_read_date({"year": 2024, "month": 11}).precision == "month"
    assert normalize_read_date({"year": 2024}).precision == "year"
    assert normalize_read_date({}).precision == "none"


def test_iso_date_part() -> None:
    assert iso_date_part("2024-11-11T14:15:16+03:00") == "2024-11-11"
    assert iso_date_part("2024-11-11Z") == "2024-11-11"
    assert iso_date_part("invalid") is None


def test_normalize_record_keeps_real_and_fallback_dates_separate() -> None:
    item = {
        "id": 100,
        "rating": 5,
        "status_set_at": "2024-11-11T12:00:00+03:00",
        "read_date": None,
        "art_edition": {
            "id": 200,
            "title": "Test Book",
            "url": "/book/200",
            "authors": [{"full_name": "Test Author"}],
            "stats": {"rating": 4.2, "marks_count": 10},
        },
    }
    record = normalize_record(
        item,
        source_order=1,
        fallback_to_status_date=True,
    )
    assert record["read_date"] is None
    assert record["effective_read_date"] == "2024-11-11"
    assert record["effective_date_source"] == "status_set_at"
    assert record["rating_5"] == 5
    assert record["rating_10"] == 10


def test_deduplicate_is_opt_in_and_loss_aware() -> None:
    records = [
        {"source_order": 1, "read_status_id": 1, "livelib_book_id": 10},
        {"source_order": 2, "read_status_id": 2, "livelib_book_id": 10},
        {"source_order": 3, "read_status_id": None, "livelib_book_id": None},
    ]
    kept, dropped = deduplicate_records(records, "book-id")
    assert [row["source_order"] for row in kept] == [1, 3]
    assert dropped == 1
    untouched, dropped = deduplicate_records(records, "none")
    assert untouched == records
    assert dropped == 0


def test_sort_read_date_places_unknown_last_and_preserves_ties() -> None:
    records = [
        {"source_order": 3, "read_date": None, "title": "Unknown"},
        {"source_order": 2, "read_date": "2020-05", "title": "Month"},
        {"source_order": 1, "read_date": "2020-05-01", "title": "Day"},
    ]
    result = sort_records(records, "read-date")
    assert [row["title"] for row in result] == ["Day", "Month", "Unknown"]


def test_write_export_is_utf8_bom_and_refuses_overwrite(tmp_path: Path) -> None:
    records = [
        {
            "source_order": 1,
            "authors": "Автор",
            "title": "Книга",
            "read_date": "2024-11-11",
            "date_precision": "day",
            "rating_5": 5,
            "rating_10": 10,
        }
    ]
    options = ExportOptions(
        output_dir=tmp_path,
        timestamped=False,
        formats=("csv", "json"),
    )
    paths = write_export(
        nickname="reader",
        user_id=1,
        fetched_count=1,
        records=records,
        raw_pages=[],
        duplicate_count=0,
        ignored_items=0,
        options=options,
        endpoints=EndpointConfig(),
        exported_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )
    assert paths["csv"].read_bytes().startswith(b"\xef\xbb\xbf")
    parsed = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert parsed["schema_version"] == "1.1"
    assert parsed["books"][0]["rating_10"] == 10

    with pytest.raises(LiveLibError, match="already exists"):
        write_export(
            nickname="reader",
            user_id=1,
            fetched_count=1,
            records=records,
            raw_pages=[],
            duplicate_count=0,
            ignored_items=0,
            options=options,
            endpoints=EndpointConfig(),
            exported_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
        )

    text = paths["csv"].read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows[0]["authors"] == "Автор"
