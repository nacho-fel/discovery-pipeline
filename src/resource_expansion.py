"""Repository-resource expansion: turn one multi-asset landing page into its
individual downloadable child assets (`ResourceAsset` rows).

Redefines the discovery unit from "document" to "research resource": a GDR-
style dataset page, an OSTI record, or a ScienceDirect article can each
reference several distinct downloadable files (a PDF report plus an XLSX
cost workbook, say). This module extracts those child links from a landing
page's own HTML -- already fetched by an adapter/acquirer elsewhere, never
by this module itself, so every actual outbound request still goes through
the single, centrally SSRF-guarded path in `acquirer.py`.

The most important safety property here is guarding against mistaking a
repository *search-results* page for an individual dataset: a search page's
links point at many unrelated records, not at child assets of ONE resource,
so `is_search_results_page` refuses to enumerate those at all.
"""

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from sqlalchemy.orm import Session

from discovery.acquirer import is_safe_url
from discovery.db.models import ResourceAsset, SourceCandidate

# Extension -> file_format, matching the "research resource, not just
# document" formats named in the architecture brief.
_DOWNLOADABLE_EXTENSIONS = {
    ".pdf": "pdf",
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".csv": "csv",
    ".tsv": "tsv",
    ".zip": "zip",
    ".json": "json",
    ".xml": "xml",
    ".docx": "docx",
    ".pptx": "pptx",
}

# URL substrings that mark a page as a search/listing view rather than a
# single record -- deliberately conservative (false positives just mean a
# few genuine dataset pages get skipped, which is far safer than the
# alternative of mistaking a results listing for one resource's assets).
_SEARCH_RESULTS_MARKERS = ("/search", "/results", "?q=", "&q=", "?query=", "&query=")


@dataclass(frozen=True)
class DiscoveredAsset:
    asset_url: str
    file_format: str
    label: str | None


class _AnchorExtractor(HTMLParser):
    """Collects every `<a href>` and its visible link text."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._current_href = href
            self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href is not None:
            self.hrefs.append((self._current_href, "".join(self._current_text_parts).strip()))
            self._current_href = None
            self._current_text_parts = []


def is_search_results_page(landing_url: str) -> bool:
    """True if `landing_url` looks like a search/listing page rather than an
    individual dataset/record -- these must never be auto-enumerated as if
    every link on the page were a child asset of one resource.
    """
    lowered = landing_url.lower()
    return any(marker in lowered for marker in _SEARCH_RESULTS_MARKERS)


def extract_downloadable_assets(
    html: str, *, landing_url: str, resolver=None
) -> list[DiscoveredAsset]:
    """Parse already-fetched `html` for links to known downloadable file
    formats, resolved to absolute URLs and SSRF-checked via the same
    `is_safe_url` every other outbound request in this pipeline goes
    through. Returns `[]` for a page `is_search_results_page` flags, and
    for any page with no recognizable asset links (the common case: most
    landing pages are themselves the one resource, with no separate child
    assets -- that's not an error, just nothing to expand).

    `resolver` is passed through to `is_safe_url` (see its docstring); tests
    always inject a fake one, matching acquirer.py's own convention.
    """
    if is_search_results_page(landing_url):
        return []

    parser = _AnchorExtractor()
    parser.feed(html)

    seen: set[str] = set()
    assets: list[DiscoveredAsset] = []
    for href, text in parser.hrefs:
        absolute = urljoin(landing_url, href)
        path = urlparse(absolute).path.lower()
        file_format = next(
            (fmt for ext, fmt in _DOWNLOADABLE_EXTENSIONS.items() if path.endswith(ext)), None
        )
        if file_format is None or absolute in seen:
            continue
        kwargs = {"resolver": resolver} if resolver is not None else {}
        safe, _reason = is_safe_url(absolute, **kwargs)
        if not safe:
            continue
        seen.add(absolute)
        assets.append(DiscoveredAsset(asset_url=absolute, file_format=file_format, label=text or None))
    return assets


def persist_discovered_assets(
    db: Session, candidate: SourceCandidate, assets: list[DiscoveredAsset]
) -> list[ResourceAsset]:
    """Persist `assets` as `ResourceAsset` rows under `candidate`.

    Idempotent by `(parent_candidate_id, asset_url)` -- re-running expansion
    on the same landing page (e.g. a resumed run) never duplicates a child
    asset; the DB's own unique constraint is the final guard, this is just
    an idempotent get-or-create so callers don't need their own dedup pass.
    """
    rows: list[ResourceAsset] = []
    for asset in assets:
        existing = (
            db.query(ResourceAsset)
            .filter(
                ResourceAsset.parent_candidate_id == candidate.id,
                ResourceAsset.asset_url == asset.asset_url,
            )
            .first()
        )
        if existing is not None:
            rows.append(existing)
            continue
        row = ResourceAsset(
            parent_candidate=candidate,  # relationship assignment, not the raw FK column,
            # so candidate.assets reflects the new row immediately (in-memory,
            # no re-query needed) via SQLAlchemy's backref population.
            asset_url=asset.asset_url,
            file_format=asset.file_format,
            label=asset.label,
        )
        db.add(row)
        db.flush()
        rows.append(row)
    return rows
