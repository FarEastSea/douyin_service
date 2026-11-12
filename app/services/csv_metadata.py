"""Spreadsheet-safe serialization of the application's metadata CSV sidecars."""
from __future__ import annotations

import csv
import io
from typing import Any, Mapping
import unicodedata


METADATA_COLUMNS = (
    "aweme_id", "author", "title", "work_type", "file_index", "published_at",
    "duration_ms", "width", "height", "music_title", "music_author", "hashtags",
    "digg_count", "comment_count", "collect_count", "share_count", "play_count",
)


def spreadsheet_safe_cell(value: Any) -> Any:
    """Mark formula-like text as literal, leaving typed numbers and nulls intact.

    Probe normalized text only: spreadsheet importers can ignore leading spaces,
    controls or format marks and recognize compatibility forms of the operators.
    The original Unicode content is retained after a single literal marker.
    """
    if not isinstance(value, str):
        return value
    probe = unicodedata.normalize("NFKC", value)
    for index, character in enumerate(probe):
        if character.isspace() or unicodedata.category(character) in {"Cc", "Cf"}:
            continue
        return "'" + value if character in "=+-@" else value
    return value


def metadata_csv_values(data: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the CSV-specific list representation without changing source data."""
    values = {column: data[column] for column in METADATA_COLUMNS}
    values["hashtags"] = ",".join(data["hashtags"])
    return values


def metadata_csv_projection(data: Mapping[str, Any], *, safe: bool = False) -> list[str]:
    values = metadata_csv_values(data)
    return [
        "" if values[column] is None else str(
            spreadsheet_safe_cell(values[column]) if safe else values[column]
        )
        for column in METADATA_COLUMNS
    ]


def metadata_csv_bytes(data: Mapping[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=METADATA_COLUMNS)
    writer.writeheader()
    writer.writerow({key: spreadsheet_safe_cell(value)
                     for key, value in metadata_csv_values(data).items()})
    return output.getvalue().encode("utf-8-sig")
