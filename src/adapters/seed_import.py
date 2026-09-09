"""Channel C: manual and seed inputs (CSV/TSV, JSONL, a single URL).

Not a network `SearchAdapter` -- these are local, file-based sources -- but
every function here returns the same `RawSearchHit` shape the network
adapters do, so seed imports enter the identical normalization/dedup/
screening workflow (per the brief: "All channels must enter the same
canonical result and candidate workflow").
"""

import csv
import json
from pathlib import Path

from discovery.models.candidate import RawSearchHit

name = "seed_import"

_KNOWN_COLUMNS = {"url", "link", "title", "doi", "authors", "year", "publication_year", "snippet"}


def _split_authors(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]


def _row_to_hit(row: dict, *, rank: int) -> RawSearchHit:
    url = row.get("url") or row.get("link")
    year_raw = row.get("publication_year") or row.get("year")
    year = int(year_raw) if year_raw and str(year_raw).strip().isdigit() else None
    return RawSearchHit(
        title=row.get("title") or None,
        url=url or None,
        doi=row.get("doi") or None,
        authors=_split_authors(row.get("authors")),
        publication_year=year,
        snippet=row.get("snippet") or None,
        rank=rank,
        raw=dict(row),
    )


def import_delimited(path: Path, *, delimiter: str = ",") -> list[RawSearchHit]:
    """Import a CSV/TSV of candidate URLs. Header row required; recognized
    columns are `url`/`link`, `title`, `doi`, `authors`, `year`/`publication_year`,
    `snippet` -- unrecognized columns are preserved in `raw` but not mapped.
    """
    hits: list[RawSearchHit] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        for rank, row in enumerate(reader, start=1):
            if not (row.get("url") or row.get("link")):
                continue
            hits.append(_row_to_hit(row, rank=rank))
    return hits


def import_jsonl(path: Path) -> list[RawSearchHit]:
    """Import a JSONL file of candidate objects, one JSON object per line."""
    hits: list[RawSearchHit] = []
    with open(path, encoding="utf-8") as handle:
        for rank, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not (row.get("url") or row.get("link")):
                continue
            hits.append(_row_to_hit(row, rank=rank))
    return hits


def import_single_url(url: str, *, title: str | None = None, doi: str | None = None) -> RawSearchHit:
    """Wrap one manually supplied URL as a single-hit import."""
    return RawSearchHit(title=title, url=url, doi=doi, rank=1, raw={"url": url})
