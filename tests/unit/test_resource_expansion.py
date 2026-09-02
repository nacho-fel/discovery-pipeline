"""Offline tests for resource_expansion.py: HTML asset-link extraction,
search-results-page guarding, SSRF filtering, and idempotent persistence.
No network calls -- DNS resolution is always faked, matching
tests/unit/test_acquirer.py's convention.
"""

from discovery.db.models import SourceCandidate
from discovery.resource_expansion import (
    extract_downloadable_assets,
    is_search_results_page,
    persist_discovered_assets,
)

_GDR_LANDING_PAGE_HTML = """
<html><body>
<h1>Fervo Cape Station Well Cost Dataset</h1>
<a href="/records/123/report.pdf">Full technical report (PDF)</a>
<a href="/records/123/cost_data.xlsx">Cost breakdown workbook (XLSX)</a>
<a href="/records/123/raw_logs.zip">Raw drilling logs (ZIP)</a>
<a href="/records/123/cost_data.xlsx">Cost breakdown workbook (XLSX)</a>
<a href="https://unrelated.example/other.pdf">An unrelated external link</a>
<a href="/about">About this repository</a>
</body></html>
"""


def _public_resolver(host, port):
    return [(0, 0, 0, "", ("93.184.216.34", 0))]


def _private_resolver(host, port):
    return [(0, 0, 0, "", ("10.0.0.5", 0))]


def test_is_search_results_page_flags_listing_urls():
    assert is_search_results_page("https://gdr.openei.org/search?q=geothermal") is True
    assert is_search_results_page("https://gdr.openei.org/records?query=egs") is True
    assert is_search_results_page("https://gdr.openei.org/results/egs") is True


def test_is_search_results_page_allows_individual_record_urls():
    assert is_search_results_page("https://gdr.openei.org/records/123") is False


def test_extract_downloadable_assets_finds_known_formats_and_dedupes():
    assets = extract_downloadable_assets(
        _GDR_LANDING_PAGE_HTML,
        landing_url="https://gdr.openei.org/records/123",
        resolver=_public_resolver,
    )
    formats = {a.file_format for a in assets}
    urls = {a.asset_url for a in assets}

    assert formats == {"pdf", "xlsx", "zip"}
    assert "https://gdr.openei.org/records/123/report.pdf" in urls
    assert "https://gdr.openei.org/records/123/cost_data.xlsx" in urls
    # Duplicate href appeared twice in the HTML but only produces one asset
    # (report.pdf, cost_data.xlsx, raw_logs.zip, and the unrelated external pdf).
    assert len(assets) == 4


def test_extract_downloadable_assets_resolves_relative_links_against_landing_url():
    assets = extract_downloadable_assets(
        _GDR_LANDING_PAGE_HTML,
        landing_url="https://gdr.openei.org/records/123",
        resolver=_public_resolver,
    )
    urls = {a.asset_url for a in assets}
    assert "https://gdr.openei.org/records/123/report.pdf" in urls
    assert "https://unrelated.example/other.pdf" in urls  # absolute hrefs pass through untouched


def test_extract_downloadable_assets_refuses_search_results_pages():
    assets = extract_downloadable_assets(
        _GDR_LANDING_PAGE_HTML,
        landing_url="https://gdr.openei.org/search?q=egs",
        resolver=_public_resolver,
    )
    assert assets == []  # never mistake a search-results page for one resource's assets


def test_extract_downloadable_assets_filters_unsafe_urls():
    html = '<a href="http://169.254.169.254/report.pdf">metadata endpoint</a>'
    assets = extract_downloadable_assets(
        html, landing_url="https://gdr.openei.org/records/123", resolver=_private_resolver
    )
    assert assets == []


def test_extract_downloadable_assets_ignores_non_downloadable_links():
    assets = extract_downloadable_assets(
        _GDR_LANDING_PAGE_HTML,
        landing_url="https://gdr.openei.org/records/123",
        resolver=_public_resolver,
    )
    assert not any(a.asset_url.endswith("/about") for a in assets)


def test_persist_discovered_assets_is_idempotent(test_db):
    candidate = SourceCandidate(canonical_url="https://gdr.openei.org/records/123")
    test_db.add(candidate)
    test_db.flush()

    assets = extract_downloadable_assets(
        _GDR_LANDING_PAGE_HTML,
        landing_url="https://gdr.openei.org/records/123",
        resolver=_public_resolver,
    )
    first_rows = persist_discovered_assets(test_db, candidate, assets)
    second_rows = persist_discovered_assets(test_db, candidate, assets)

    assert len(first_rows) == 4
    assert {r.id for r in first_rows} == {r.id for r in second_rows}  # no duplicate rows on re-run
    assert len(candidate.assets) == 4
