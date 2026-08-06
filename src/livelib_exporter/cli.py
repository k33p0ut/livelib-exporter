"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .core import (
    ExportOptions,
    HttpConfig,
    LiveLibClient,
    LiveLibError,
    ProfileIdentifierType,
    export_profile,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="livelib-export",
        description=(
            "Export ratings, reading dates, and public book metadata from a "
            "public LiveLib profile."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("profile", help="LiveLib nickname or numeric user ID")
    parser.add_argument("--user-id", type=int, help="Numeric LiveLib user ID")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Destination directory",
    )
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=("csv", "json"),
        default=("csv", "json"),
        help="Normalized output formats",
    )
    parser.add_argument("--raw", action="store_true", help="Also save raw API pages")
    parser.add_argument(
        "--fallback-to-status-date",
        action="store_true",
        help=(
            "Use status_set_at only as effective_read_date when read_date is absent; "
            "the original read_date remains empty"
        ),
    )
    parser.add_argument(
        "--deduplicate-by",
        choices=("none", "status-id", "book-id"),
        default="none",
        help="Optional loss-aware duplicate removal",
    )
    parser.add_argument(
        "--sort",
        dest="sort_by",
        choices=("source", "read-date", "title"),
        default="source",
        help="Record order in normalized exports",
    )
    parser.add_argument(
        "--no-timestamp",
        action="store_true",
        help="Use deterministic filenames without a timestamp",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing existing output files",
    )
    parser.add_argument(
        "--csv-delimiter",
        default=",",
        help="Single-character CSV delimiter",
    )
    parser.add_argument("--json-indent", type=int, default=2, help="JSON indentation")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout, seconds")
    parser.add_argument("--retries", type=int, default=4, help="Transient error retries")
    parser.add_argument(
        "--pause",
        type=float,
        default=0.5,
        help="Delay between API pages, seconds",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1_000,
        help="Pagination safety limit",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    parser.add_argument("--verbose", action="store_true", help="Enable diagnostic logs")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def resolve_cli_profile(
    profile: str, explicit_user_id: int | None
) -> tuple[str, int | None, ProfileIdentifierType]:
    """Interpret an all-digit positional argument as a LiveLib numeric user ID."""
    clean = profile.strip()
    if not clean:
        raise ValueError("profile cannot be empty")

    if clean.isdecimal():
        parsed_id = int(clean)
        if parsed_id <= 0:
            raise ValueError("numeric LiveLib user ID must be greater than zero")
        if explicit_user_id is not None and explicit_user_id != parsed_id:
            raise ValueError(
                "numeric profile argument and --user-id refer to different users"
            )
        return clean, parsed_id, "user-id"

    return clean, explicit_user_id, "nickname"


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    try:
        http = HttpConfig(
            timeout=args.timeout,
            retries=args.retries,
            pause=args.pause,
            max_pages=args.max_pages,
        )
        options = ExportOptions(
            output_dir=args.output_dir,
            formats=tuple(args.formats),
            save_raw=args.raw,
            fallback_to_status_date=args.fallback_to_status_date,
            deduplicate_by=args.deduplicate_by,
            sort_by=args.sort_by,
            timestamped=not args.no_timestamp,
            overwrite=args.overwrite,
            csv_delimiter=args.csv_delimiter,
            json_indent=args.json_indent,
        )
        client = LiveLibClient(http=http)

        def progress(page: int, records: int) -> None:
            if not args.quiet:
                print(
                    f"\rPages: {page}; records: {records}",
                    end="",
                    flush=True,
                )

        profile, user_id, profile_identifier_type = resolve_cli_profile(
            args.profile, args.user_id
        )
        result = export_profile(
            profile,
            user_id=user_id,
            client=client,
            options=options,
            progress=progress,
            profile_identifier_type=profile_identifier_type,
        )
        if not args.quiet:
            print()
            if result.profile_identifier_type == "user-id":
                print(f"Profile: LiveLib ID {result.user_id}")
            else:
                print(f"Profile: {result.nickname} (LiveLib ID {result.user_id})")
            print(
                f"Fetched: {result.fetched_count}; exported: {result.exported_count}; "
                f"duplicates removed: {result.duplicate_count}"
            )
            if result.ignored_items:
                print(f"Malformed API items ignored: {result.ignored_items}")
            print(f"Without read date: {result.records_without_read_date}")
            print(f"Without rating: {result.records_without_rating}")
            for kind, path in result.paths.items():
                print(f"{kind.upper()}: {path.resolve()}")
        return 0
    except (ValueError, LiveLibError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
