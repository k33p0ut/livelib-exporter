"""Core fetching, normalization, validation, and export logic."""

from __future__ import annotations

import calendar
import csv
import io
import json
import logging
import os
import posixpath
import re
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

TOOL_VERSION = "1.1.0"
SCHEMA_VERSION = "1.1"

DEFAULT_PROFILE_ROOT = "https://www.livelib.ru/reader/"
DEFAULT_API_ROOT = "https://beta.api.livelib.ru/trisolaris"
DEFAULT_SITE_ROOT = "https://www.livelib.ru"
DEFAULT_USER_AGENT = (
    f"livelib-exporter/{TOOL_VERSION} "
    "(public profile backup tool; respectful automated client)"
)

DeduplicateMode = Literal["none", "status-id", "book-id"]
SortMode = Literal["source", "read-date", "title"]
OutputFormat = Literal["csv", "json"]
ProfileIdentifierType = Literal["nickname", "user-id"]
ProgressCallback = Callable[[int, int], None]
OpenUrl = Callable[..., Any]
Sleep = Callable[[float], None]
Now = Callable[[], datetime]


class LiveLibError(RuntimeError):
    """Base error for network, schema, or validation failures."""


class OutputExistsError(LiveLibError):
    """Raised when a deterministic output path already exists."""


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """Service endpoints, configurable for tests and future migrations."""

    profile_root: str = DEFAULT_PROFILE_ROOT
    api_root: str = DEFAULT_API_ROOT
    site_root: str = DEFAULT_SITE_ROOT

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError(f"{name} must be an absolute HTTPS URL: {value!r}")


@dataclass(frozen=True, slots=True)
class HttpConfig:
    """HTTP behavior and safety limits."""

    timeout: float = 30.0
    retries: int = 4
    pause: float = 0.5
    max_backoff: float = 30.0
    max_pages: int = 1_000
    user_agent: str = DEFAULT_USER_AGENT

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if self.retries < 0:
            raise ValueError("retries cannot be negative")
        if self.pause < 0:
            raise ValueError("pause cannot be negative")
        if self.max_backoff <= 0:
            raise ValueError("max_backoff must be greater than zero")
        if self.max_pages <= 0:
            raise ValueError("max_pages must be greater than zero")
        if not self.user_agent.strip():
            raise ValueError("user_agent cannot be empty")


@dataclass(frozen=True, slots=True)
class ExportOptions:
    """Normalization and output behavior."""

    output_dir: Path = Path(".")
    formats: tuple[OutputFormat, ...] = ("csv", "json")
    save_raw: bool = False
    fallback_to_status_date: bool = False
    deduplicate_by: DeduplicateMode = "none"
    sort_by: SortMode = "source"
    timestamped: bool = True
    overwrite: bool = False
    csv_delimiter: str = ","
    json_indent: int = 2

    def __post_init__(self) -> None:
        if not self.formats:
            raise ValueError("at least one output format is required")
        invalid = set(self.formats) - {"csv", "json"}
        if invalid:
            raise ValueError(f"unsupported output formats: {sorted(invalid)}")
        if self.deduplicate_by not in {"none", "status-id", "book-id"}:
            raise ValueError(f"unsupported deduplicate mode: {self.deduplicate_by}")
        if self.sort_by not in {"source", "read-date", "title"}:
            raise ValueError(f"unsupported sort mode: {self.sort_by}")
        if len(self.csv_delimiter) != 1:
            raise ValueError("csv_delimiter must contain exactly one character")
        if not 0 <= self.json_indent <= 8:
            raise ValueError("json_indent must be between 0 and 8")


@dataclass(frozen=True, slots=True)
class FetchResult:
    records: list[dict[str, Any]]
    raw_pages: list[dict[str, Any]]
    page_count: int
    ignored_items: int


@dataclass(frozen=True, slots=True)
class ExportResult:
    nickname: str
    user_id: int
    fetched_count: int
    exported_count: int
    duplicate_count: int
    ignored_items: int
    paths: Mapping[str, Path]
    records_without_read_date: int
    records_without_rating: int
    profile_identifier_type: ProfileIdentifierType = "nickname"


@dataclass(frozen=True, slots=True)
class NormalizedDate:
    value: str | None
    precision: Literal["day", "month", "year", "none"]


CSV_COLUMNS = [
    "source_order",
    "read_status_id",
    "livelib_book_id",
    "authors",
    "title",
    "read_date",
    "date_precision",
    "effective_read_date",
    "effective_date_source",
    "rating_5",
    "rating_10",
    "rating_raw",
    "status_set_at",
    "status_set_date",
    "notes",
    "publication_year",
    "livelib_url",
    "cover_url",
    "formats_owned",
    "community_rating",
    "community_marks_count",
    "community_reads_count",
    "community_wants_count",
]

_USER_ID_PATTERNS = (
    r'"userId"\s*:\s*(\d+)',
    r'\\"userId\\"\s*:\s*(\d+)',
    r'\["user-id","(\d+)","d"\]',
    r'\[\\"user-id\\",\\"(\d+)\\",\\"d\\"\]',
    r'"children":\[\["user-id","(\d+)","d"\]',
    r'\\"children\\":\[\[\\"user-id\\",\\"(\d+)\\",\\"d\\"\]',
    r'"c":\["","users","(\d+)"\]',
    r'\\"c\\":\[\\"\\",\\"users\\",\\"(\d+)\\"\]',
)


class LiveLibClient:
    """Small, dependency-free client for public LiveLib profile exports."""

    def __init__(
        self,
        http: HttpConfig | None = None,
        endpoints: EndpointConfig | None = None,
        *,
        opener: OpenUrl = urlopen,
        sleeper: Sleep = time.sleep,
        now: Now | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.http = http or HttpConfig()
        self.endpoints = endpoints or EndpointConfig()
        self._opener = opener
        self._sleep = sleeper
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.logger = logger or logging.getLogger(__name__)

    def get_text(self, url: str, *, accept: str = "*/*") -> tuple[str, str]:
        """Return response text and final URL, retrying only transient failures."""
        headers = {
            "User-Agent": self.http.user_agent,
            "Accept": accept,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
        }
        last_error: Exception | None = None

        for attempt in range(self.http.retries + 1):
            try:
                request = Request(url, headers=headers)
                with self._opener(request, timeout=self.http.timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    body = response.read().decode(charset, errors="replace")
                    return body, response.geturl()
            except HTTPError as exc:
                last_error = exc
                transient = exc.code == 429 or 500 <= exc.code < 600
                if not transient or attempt >= self.http.retries:
                    details = _read_http_error_preview(exc)
                    suffix = f" Response: {details}" if details else ""
                    raise LiveLibError(f"HTTP {exc.code} for {url}.{suffix}") from exc
                delay = retry_delay(
                    exc.headers.get("Retry-After"),
                    attempt=attempt,
                    now=self._now(),
                    max_backoff=self.http.max_backoff,
                )
                self.logger.warning(
                    "Transient HTTP %s; retrying in %.1fs (%s/%s)",
                    exc.code,
                    delay,
                    attempt + 1,
                    self.http.retries,
                )
                self._sleep(delay)
            except (URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt >= self.http.retries:
                    break
                delay = min(float(2**attempt), self.http.max_backoff)
                self.logger.warning(
                    "Network error; retrying in %.1fs (%s/%s): %s",
                    delay,
                    attempt + 1,
                    self.http.retries,
                    exc,
                )
                self._sleep(delay)

        raise LiveLibError(f"Could not fetch {url}: {last_error}") from last_error

    def get_json(self, url: str) -> dict[str, Any]:
        text, _ = self.get_text(url, accept="application/json,text/plain,*/*")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            preview = text[:500].replace("\n", " ")
            raise LiveLibError(
                f"LiveLib returned non-JSON data for {url}. Preview: {preview}"
            ) from exc
        if not isinstance(value, dict):
            raise LiveLibError(
                f"Unexpected JSON root for {url}: {type(value).__name__}"
            )
        return value

    def resolve_user_id(self, nickname: str) -> int:
        clean = validate_nickname(nickname)
        profile_url = self.endpoints.profile_root.rstrip("/") + "/" + quote(clean, safe="")
        html, final_url = self.get_text(profile_url, accept="text/html,*/*")
        return resolve_user_id_from_html(html, final_url=final_url, nickname=clean)

    def fetch_read_books(
        self,
        user_id: int,
        *,
        progress: ProgressCallback | None = None,
    ) -> FetchResult:
        if user_id <= 0:
            raise ValueError("user_id must be greater than zero")

        url: str | None = (
            self.endpoints.api_root.rstrip("/")
            + f"/api/users/{user_id}/arts/reads"
        )
        seen_urls: set[str] = set()
        records: list[dict[str, Any]] = []
        raw_pages: list[dict[str, Any]] = []
        ignored_items = 0
        page_number = 0

        while url:
            if page_number >= self.http.max_pages:
                raise LiveLibError(
                    f"Pagination exceeded safety limit of {self.http.max_pages} pages"
                )
            if url in seen_urls:
                raise LiveLibError(f"Pagination loop detected: {url}")
            seen_urls.add(url)

            response = self.get_json(url)
            raw_pages.append(response)
            page_records, next_page, page_ignored = extract_payload(response, url=url)
            records.extend(page_records)
            ignored_items += page_ignored
            page_number += 1

            if progress:
                progress(page_number, len(records))

            url = build_next_api_url(
                next_page,
                api_root=self.endpoints.api_root,
                current_url=url,
            )
            if url:
                self._sleep(self.http.pause)

        return FetchResult(
            records=records,
            raw_pages=raw_pages,
            page_count=page_number,
            ignored_items=ignored_items,
        )


def _read_http_error_preview(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:300].strip()
    except Exception:  # pragma: no cover - defensive fallback for odd HTTP handlers
        return ""


def retry_delay(
    retry_after: str | None,
    *,
    attempt: int,
    now: datetime,
    max_backoff: float,
) -> float:
    """Parse Retry-After seconds or HTTP date; otherwise use exponential backoff."""
    fallback = min(float(2**attempt), max_backoff)
    if not retry_after:
        return fallback

    stripped = retry_after.strip()
    try:
        seconds = float(stripped)
    except ValueError:
        seconds = -1
    if seconds >= 0:
        return min(seconds, max_backoff)

    try:
        retry_at = parsedate_to_datetime(stripped)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        base = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return min(max((retry_at - base).total_seconds(), 0.0), max_backoff)
    except (TypeError, ValueError, OverflowError):
        return fallback


def validate_nickname(nickname: str) -> str:
    clean = nickname.strip()
    if not clean:
        raise ValueError("nickname cannot be empty")
    if len(clean) > 100:
        raise ValueError("nickname is unexpectedly long")
    if any(ord(char) < 32 or ord(char) == 127 for char in clean):
        raise ValueError("nickname contains control characters")
    return clean


def resolve_user_id_from_html(html: str, *, final_url: str, nickname: str) -> int:
    lowered = html.lower()
    if any(token in lowered for token in ("капча", "captcha", "слишком много запросов")):
        raise LiveLibError(
            "LiveLib displayed a CAPTCHA or rate-limit page. Retry later or use --user-id."
        )

    for pattern in _USER_ID_PATTERNS:
        match = re.search(pattern, html)
        if match:
            return int(match.group(1))

    final_match = re.search(r"/users/(\d+)(?:/|$)", final_url)
    if final_match:
        return int(final_match.group(1))

    raise LiveLibError(
        f"Could not resolve numeric LiveLib user ID for {nickname!r}. "
        "The profile may be private, missing, or the page format may have changed. "
        "Pass the ID explicitly with --user-id."
    )


def build_next_api_url(
    next_page: Any,
    *,
    api_root: str,
    current_url: str | None = None,
) -> str | None:
    """Resolve, canonicalize, and validate a LiveLib pagination URL.

    The working API is served below ``<api_root>/api/``. LiveLib has been
    observed returning absolute pagination links below ``/api/`` on the same
    host, even though requesting those links directly returns a backend 404.
    Such links are aliases, not alternative endpoints: this function rewrites
    them back below the configured API root before the next request is made.

    Relative links and query-only links are resolved against ``current_url``
    when supplied. HTTPS, same-host, fragment, path, and traversal checks are
    applied before a canonical URL is returned.
    """
    if not next_page:
        return None
    if not isinstance(next_page, str):
        raise LiveLibError("pagination.next_page must be a string or null")

    root = api_root.rstrip("/")
    root_parts = urlparse(root)
    base = current_url or (root + "/")
    candidate = urljoin(base, next_page)
    candidate_parts = urlparse(candidate)

    if candidate_parts.scheme != "https":
        raise LiveLibError(f"Refusing non-HTTPS pagination URL: {candidate}")
    if candidate_parts.netloc.casefold() != root_parts.netloc.casefold():
        raise LiveLibError(f"Refusing pagination URL on another host: {candidate}")
    if candidate_parts.fragment:
        raise LiveLibError(f"Refusing pagination URL with a fragment: {candidate}")
    if candidate_parts.params:
        raise LiveLibError(f"Refusing pagination URL with path parameters: {candidate}")

    # Decode before normalizing so encoded traversal sequences cannot bypass
    # the path restriction. ``normpath`` also collapses duplicate separators.
    decoded_path = unquote(candidate_parts.path)
    path_segments = [segment for segment in decoded_path.split("/") if segment]
    if any(segment in {".", ".."} for segment in path_segments):
        raise LiveLibError(f"Refusing pagination URL with path traversal: {candidate}")

    normalized_path = posixpath.normpath(decoded_path)
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path

    root_path = posixpath.normpath(root_parts.path).rstrip("/")
    canonical_prefix = root_path + "/api"

    if normalized_path == "/api" or normalized_path.startswith("/api/"):
        # LiveLib's paginator omits the deployment prefix (currently
        # ``/trisolaris``). Preserve the API suffix and query, but request it
        # through the configured, known-working API root.
        suffix = normalized_path[len("/api") :]
        canonical_path = canonical_prefix + suffix
    elif normalized_path == canonical_prefix or normalized_path.startswith(
        canonical_prefix + "/"
    ):
        canonical_path = normalized_path
    else:
        raise LiveLibError(
            f"Refusing pagination URL outside LiveLib API path: {candidate}"
        )

    canonical_path = quote(canonical_path, safe="/-._~")
    return root_parts._replace(
        path=canonical_path,
        params="",
        query=candidate_parts.query,
        fragment="",
    ).geturl()


def extract_payload(
    response: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], str | None, int]:
    if response.get("error"):
        raise LiveLibError(f"LiveLib returned an error for {url}: {response['error']}")

    payload = response.get("payload")
    if not isinstance(payload, Mapping):
        raise LiveLibError(f"Response from {url} has no object payload")

    data = payload.get("data", [])
    if not isinstance(data, list):
        raise LiveLibError(f"payload.data from {url} is not a list")

    clean_items = [item for item in data if isinstance(item, dict)]
    ignored = len(data) - len(clean_items)

    pagination = payload.get("pagination") or {}
    if not isinstance(pagination, Mapping):
        raise LiveLibError(f"payload.pagination from {url} is not an object")

    next_page = pagination.get("next_page")
    if next_page is not None and not isinstance(next_page, str):
        raise LiveLibError(f"pagination.next_page from {url} is not a string")

    return clean_items, next_page, ignored


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def as_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_read_date(value: Any) -> NormalizedDate:
    if not isinstance(value, Mapping):
        return NormalizedDate(None, "none")

    year = as_int(value.get("year"))
    month = as_int(value.get("month"))
    day = as_int(value.get("day"))

    if year and month and day:
        try:
            return NormalizedDate(date(year, month, day).isoformat(), "day")
        except ValueError:
            pass
    if year and month and 1 <= month <= 12:
        return NormalizedDate(f"{year:04d}-{month:02d}", "month")
    if year and 1 <= year <= 9999:
        return NormalizedDate(f"{year:04d}", "year")
    return NormalizedDate(None, "none")


def normalize_authors(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    names: list[str] = []
    for author in value:
        if isinstance(author, Mapping):
            name = author.get("full_name") or author.get("name")
            if name:
                names.append(str(name).strip())
        elif author:
            names.append(str(author).strip())
    return ", ".join(name for name in names if name)


def normalize_formats(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    labels: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            label = (
                item.get("title")
                or item.get("name")
                or item.get("format")
                or item.get("slug")
                or item.get("id")
            )
            if label is not None:
                labels.append(str(label).strip())
        elif item is not None:
            labels.append(str(item).strip())
    return ", ".join(label for label in labels if label)


def iso_date_part(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T\s]|$)", text)
        if not match:
            return None
        try:
            return date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            return None


def normalize_rating(value: Any) -> tuple[int | None, int | None, int | None]:
    raw = as_int(value)
    if raw is None or raw == 0:
        return None, None, raw
    if 1 <= raw <= 5:
        return raw, raw * 2, raw
    return None, None, raw


def normalize_record(
    item: Mapping[str, Any],
    *,
    source_order: int,
    fallback_to_status_date: bool,
    site_root: str = DEFAULT_SITE_ROOT,
) -> dict[str, Any]:
    edition_value = item.get("art_edition")
    edition: Mapping[str, Any] = (
        edition_value if isinstance(edition_value, Mapping) else {}
    )

    read = normalize_read_date(item.get("read_date"))
    set_date = iso_date_part(item.get("status_set_at"))
    effective_date = read.value
    effective_source = "read_date" if read.value else ""
    if not effective_date and fallback_to_status_date and set_date:
        effective_date = set_date
        effective_source = "status_set_at"

    rating_5, rating_10, rating_raw = normalize_rating(item.get("rating"))

    relative_url = edition.get("url")
    book_url = urljoin(site_root.rstrip("/") + "/", str(relative_url)) if relative_url else ""

    stats_value = edition.get("stats")
    stats: Mapping[str, Any] = stats_value if isinstance(stats_value, Mapping) else {}

    return {
        "source_order": source_order,
        "read_status_id": as_int(item.get("id")),
        "livelib_book_id": as_int(edition.get("id")),
        "authors": normalize_authors(edition.get("authors")),
        "title": str(edition.get("title") or "").strip(),
        "read_date": read.value,
        "date_precision": read.precision,
        "effective_read_date": effective_date,
        "effective_date_source": effective_source,
        "rating_5": rating_5,
        "rating_10": rating_10,
        "rating_raw": rating_raw,
        "status_set_at": str(item.get("status_set_at") or ""),
        "status_set_date": set_date,
        "notes": str(item.get("notes") or ""),
        "publication_year": as_int(edition.get("publication_year")),
        "livelib_url": book_url,
        "cover_url": str(edition.get("cover_url") or ""),
        "formats_owned": normalize_formats(item.get("art_formats_owned")),
        "community_rating": as_float(stats.get("rating")),
        "community_marks_count": as_int(stats.get("marks_count")),
        "community_reads_count": as_int(stats.get("reads_count")),
        "community_wants_count": as_int(stats.get("wants_count")),
    }


def normalize_records(
    items: Iterable[Mapping[str, Any]],
    *,
    fallback_to_status_date: bool,
    site_root: str,
) -> list[dict[str, Any]]:
    return [
        normalize_record(
            item,
            source_order=index,
            fallback_to_status_date=fallback_to_status_date,
            site_root=site_root,
        )
        for index, item in enumerate(items, start=1)
    ]


def deduplicate_records(
    records: Sequence[dict[str, Any]], mode: DeduplicateMode
) -> tuple[list[dict[str, Any]], int]:
    if mode == "none":
        return list(records), 0

    field = "read_status_id" if mode == "status-id" else "livelib_book_id"
    seen: set[Any] = set()
    kept: list[dict[str, Any]] = []
    dropped = 0

    for record in records:
        key = record.get(field)
        if key is None:
            kept.append(record)
            continue
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(record)
    return kept, dropped


def _partial_date_sort_key(value: Any) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or not value:
        return (1, 9999, 12, 31)
    parts = value.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) >= 2 else 12
        if len(parts) >= 3:
            day = int(parts[2])
        else:
            day = calendar.monthrange(year, month)[1] if len(parts) >= 2 else 31
        date(year, month, day)
        return (0, year, month, day)
    except (ValueError, IndexError):
        return (1, 9999, 12, 31)


def sort_records(records: Sequence[dict[str, Any]], mode: SortMode) -> list[dict[str, Any]]:
    if mode == "source":
        return sorted(records, key=lambda row: as_int(row.get("source_order")) or 0)
    if mode == "title":
        return sorted(
            records,
            key=lambda row: (
                str(row.get("authors") or "").casefold(),
                str(row.get("title") or "").casefold(),
                as_int(row.get("source_order")) or 0,
            ),
        )
    return sorted(
        records,
        key=lambda row: (
            _partial_date_sort_key(row.get("effective_read_date") or row.get("read_date")),
            as_int(row.get("source_order")) or 0,
        ),
    )


def safe_filename(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "_", text).strip("._")
    return cleaned or "user"


def _atomic_write_bytes(path: Path, content: bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise OutputExistsError(f"Output already exists: {path}")

    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise


def _json_bytes(value: Any, *, indent: int) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=indent if indent else None,
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")


def _csv_bytes(records: Sequence[Mapping[str, Any]], *, delimiter: str) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=CSV_COLUMNS,
        extrasaction="ignore",
        delimiter=delimiter,
    )
    writer.writeheader()
    writer.writerows(records)
    return buffer.getvalue().encode("utf-8-sig")


def validate_user_id(user_id: int) -> int:
    """Validate a numeric LiveLib user ID."""
    if isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("user_id must be greater than zero")
    return user_id


def validate_profile_identifier(
    identifier: str, identifier_type: ProfileIdentifierType
) -> str:
    """Validate a CLI/library profile identifier without performing network I/O."""
    if identifier_type == "nickname":
        return validate_nickname(identifier)
    if identifier_type == "user-id":
        clean = identifier.strip()
        if not clean.isdecimal():
            raise ValueError("a user-id profile identifier must contain only digits")
        validate_user_id(int(clean))
        return clean
    raise ValueError(f"unsupported profile identifier type: {identifier_type!r}")


def build_profile_source_url(
    *,
    identifier: str,
    identifier_type: ProfileIdentifierType,
    user_id: int,
    profile_root: str,
    api_root: str,
) -> str:
    """Return the truthful source URL for nickname- and user-ID-based exports."""
    if identifier_type == "nickname":
        return profile_root.rstrip("/") + "/" + quote(identifier, safe="")
    return api_root.rstrip("/") + f"/api/users/{user_id}/arts/reads"


def build_export_document(
    *,
    nickname: str,
    user_id: int,
    fetched_count: int,
    records: Sequence[dict[str, Any]],
    duplicate_count: int,
    ignored_items: int,
    fallback_to_status_date: bool,
    deduplicate_by: DeduplicateMode,
    sort_by: SortMode,
    exported_at: datetime,
    profile_root: str,
    api_root: str = DEFAULT_API_ROOT,
    profile_identifier_type: ProfileIdentifierType = "nickname",
) -> dict[str, Any]:
    precision_counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("date_precision") or "none")
        precision_counts[key] = precision_counts.get(key, 0) + 1

    source_url = build_profile_source_url(
        identifier=nickname,
        identifier_type=profile_identifier_type,
        user_id=user_id,
        profile_root=profile_root,
        api_root=api_root,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "livelib-exporter", "version": TOOL_VERSION},
        "source": source_url,
        "profile_identifier": nickname,
        "profile_identifier_type": profile_identifier_type,
        "username": nickname if profile_identifier_type == "nickname" else None,
        "livelib_user_id": user_id,
        "exported_at": exported_at.astimezone(timezone.utc).isoformat(),
        "fetched_record_count": fetched_count,
        "exported_record_count": len(records),
        "duplicate_record_count": duplicate_count,
        "ignored_malformed_item_count": ignored_items,
        "fallback_to_status_date": fallback_to_status_date,
        "deduplicate_by": deduplicate_by,
        "sort_by": sort_by,
        "date_precision_counts": precision_counts,
        "records_without_read_date": sum(1 for row in records if not row.get("read_date")),
        "records_without_rating": sum(1 for row in records if row.get("rating_5") is None),
        "books": list(records),
    }


def write_export(
    *,
    nickname: str,
    user_id: int,
    fetched_count: int,
    records: Sequence[dict[str, Any]],
    raw_pages: Sequence[dict[str, Any]],
    duplicate_count: int,
    ignored_items: int,
    options: ExportOptions,
    endpoints: EndpointConfig,
    exported_at: datetime,
    profile_identifier_type: ProfileIdentifierType = "nickname",
) -> Mapping[str, Path]:
    timestamp = exported_at.astimezone().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{timestamp}" if options.timestamped else ""
    base = f"livelib_{safe_filename(nickname)}{suffix}"
    output_dir = options.output_dir

    document = build_export_document(
        nickname=nickname,
        user_id=user_id,
        fetched_count=fetched_count,
        records=records,
        duplicate_count=duplicate_count,
        ignored_items=ignored_items,
        fallback_to_status_date=options.fallback_to_status_date,
        deduplicate_by=options.deduplicate_by,
        sort_by=options.sort_by,
        exported_at=exported_at,
        profile_root=endpoints.profile_root,
        api_root=endpoints.api_root,
        profile_identifier_type=profile_identifier_type,
    )

    paths: dict[str, Path] = {}
    if "json" in options.formats:
        path = output_dir / f"{base}.json"
        _atomic_write_bytes(
            path,
            _json_bytes(document, indent=options.json_indent),
            overwrite=options.overwrite,
        )
        paths["json"] = path

    if "csv" in options.formats:
        path = output_dir / f"{base}.csv"
        _atomic_write_bytes(
            path,
            _csv_bytes(records, delimiter=options.csv_delimiter),
            overwrite=options.overwrite,
        )
        paths["csv"] = path

    if options.save_raw:
        raw_source_url = build_profile_source_url(
            identifier=nickname,
            identifier_type=profile_identifier_type,
            user_id=user_id,
            profile_root=endpoints.profile_root,
            api_root=endpoints.api_root,
        )
        raw_document = {
            "schema_version": SCHEMA_VERSION,
            "tool": {"name": "livelib-exporter", "version": TOOL_VERSION},
            "source": raw_source_url,
            "profile_identifier": nickname,
            "profile_identifier_type": profile_identifier_type,
            "username": nickname if profile_identifier_type == "nickname" else None,
            "livelib_user_id": user_id,
            "exported_at": exported_at.astimezone(timezone.utc).isoformat(),
            "pages": list(raw_pages),
        }
        path = output_dir / f"{base}_raw.json"
        _atomic_write_bytes(
            path,
            _json_bytes(raw_document, indent=options.json_indent),
            overwrite=options.overwrite,
        )
        paths["raw"] = path

    return paths


def export_profile(
    nickname: str,
    *,
    user_id: int | None = None,
    client: LiveLibClient | None = None,
    options: ExportOptions | None = None,
    progress: ProgressCallback | None = None,
    exported_at: datetime | None = None,
    profile_identifier_type: ProfileIdentifierType = "nickname",
) -> ExportResult:
    clean_nickname = validate_profile_identifier(nickname, profile_identifier_type)
    active_client = client or LiveLibClient()
    active_options = options or ExportOptions()
    if profile_identifier_type == "user-id":
        parsed_id = validate_user_id(int(clean_nickname))
        if user_id is not None and validate_user_id(user_id) != parsed_id:
            raise ValueError(
                "numeric profile identifier and --user-id refer to different users"
            )
        resolved_id = parsed_id
    else:
        resolved_id = (
            validate_user_id(user_id)
            if user_id is not None
            else active_client.resolve_user_id(clean_nickname)
        )
    fetched = active_client.fetch_read_books(resolved_id, progress=progress)

    normalized = normalize_records(
        fetched.records,
        fallback_to_status_date=active_options.fallback_to_status_date,
        site_root=active_client.endpoints.site_root,
    )
    deduplicated, duplicate_count = deduplicate_records(
        normalized, active_options.deduplicate_by
    )
    sorted_records = sort_records(deduplicated, active_options.sort_by)
    timestamp = exported_at or datetime.now(timezone.utc)

    paths = write_export(
        nickname=clean_nickname,
        user_id=resolved_id,
        fetched_count=len(fetched.records),
        records=sorted_records,
        raw_pages=fetched.raw_pages,
        duplicate_count=duplicate_count,
        ignored_items=fetched.ignored_items,
        options=active_options,
        endpoints=active_client.endpoints,
        exported_at=timestamp,
        profile_identifier_type=profile_identifier_type,
    )

    return ExportResult(
        nickname=clean_nickname,
        user_id=resolved_id,
        fetched_count=len(fetched.records),
        exported_count=len(sorted_records),
        duplicate_count=duplicate_count,
        ignored_items=fetched.ignored_items,
        paths=paths,
        records_without_read_date=sum(
            1 for record in sorted_records if not record.get("read_date")
        ),
        records_without_rating=sum(
            1 for record in sorted_records if record.get("rating_5") is None
        ),
        profile_identifier_type=profile_identifier_type,
    )
