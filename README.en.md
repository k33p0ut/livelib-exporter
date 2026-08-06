# livelib-exporter

A dependency-free command-line tool for backing up a public LiveLib user's read-books list to normalized CSV and JSON files.

This project is not affiliated with LiveLib and does not use an official API. It relies on an internal publicly reachable endpoint, which may change without notice.

The exporter handles LiveLib's current pagination quirk: the service may advertise a `/api/...` link even though the working route is under `/trisolaris/api/...`. The link is safely canonicalized before the next request.

## Features

- accepts either a LiveLib nickname or a numeric user ID;
- resolves the numeric ID automatically for nickname-based exports;
- bypasses profile HTML lookup for numeric-ID exports;
- downloads all pages with loop and page-count protection;
- retries `429` and temporary server errors;
- supports both numeric and HTTP-date `Retry-After` values;
- writes Excel-friendly UTF-8-with-BOM CSV;
- writes normalized JSON with an explicit schema version;
- preserves both five-point and ten-point ratings;
- preserves reading-date precision: day, month, year, or missing;
- keeps explicit `read_date` separate from technical `status_set_at`;
- supports opt-in deduplication and sorting;
- writes files atomically and refuses accidental overwrite by default;
- exposes a testable Python API;
- has no required third-party dependencies.

## Requirements

Python 3.10 or newer.

## Installation

```bash
python -m pip install .
```

For development:

```bash
python -m pip install -e ".[dev]"
```

## Usage

By nickname:

```bash
livelib-export nickname
python livelib_export.py nickname
```

By numeric LiveLib user ID:

```bash
livelib-export 121955
python livelib_export.py 121955
```

An all-digit positional argument is treated as a numeric user ID, not as a nickname. Numeric-ID mode requests the user API directly and never attempts `/reader/121955`.

Useful options:

```bash
livelib-export nickname --output-dir exports
livelib-export nickname --raw
livelib-export nickname --fallback-to-status-date
livelib-export nickname --no-timestamp --overwrite
livelib-export nickname --sort read-date
```

## Dates

- `read_date` — an explicit reading date supplied by the user;
- `date_precision` — `day`, `month`, `year`, or `none`;
- `status_set_at` — the original technical status timestamp;
- `status_set_date` — the calendar date extracted from that timestamp;
- `effective_read_date` — the processing date selected by the exporter;
- `effective_date_source` — the source of that selected date.

The technical status date is never silently presented as a reading date. It is used only with `--fallback-to-status-date`.

## Ratings

- `rating_raw` — the value returned by LiveLib;
- `rating_5` — normalized five-point rating;
- `rating_10` — the same rating multiplied by two.

Missing or zero ratings stay empty.

## Profile identity in JSON

Schema 1.1 includes:

- `profile_identifier` — the supplied nickname or numeric ID;
- `profile_identifier_type` — `nickname` or `user-id`;
- `username` — the nickname, or `null` for ID-only exports;
- `livelib_user_id` — the resolved numeric user ID;
- `source` — the profile URL for nickname exports or the API URL for ID-only exports.

## Python API

Nickname mode:

```python
from pathlib import Path

from livelib_exporter import ExportOptions, export_profile

result = export_profile(
    "nickname",
    options=ExportOptions(output_dir=Path("exports")),
)
```

Numeric-ID mode:

```python
result = export_profile(
    "121955",
    user_id=121955,
    profile_identifier_type="user-id",
    options=ExportOptions(output_dir=Path("exports")),
)
```

## Safety and limitations

- Use only public profiles and data you are allowed to access.
- Do not remove the inter-page delay without a good reason.
- Do not use the project for large-scale profile scraping.
- Raw exports may contain publicly returned notes; review them before publishing.
- The internal LiveLib endpoint is undocumented and can change without notice.

## Tests

```bash
pytest
```

Full development verification:

```bash
ruff check .
mypy src
pytest --cov=livelib_exporter
python -m build
```

## License

MIT. See [LICENSE](LICENSE).
