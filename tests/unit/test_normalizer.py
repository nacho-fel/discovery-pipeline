from discovery.normalizer import normalize_doi, normalize_title, normalize_url


def test_normalize_doi_strips_url_prefix():
    assert normalize_doi("https://doi.org/10.1234/ABC.2024") == "10.1234/abc.2024"


def test_normalize_doi_strips_doi_scheme():
    assert normalize_doi("doi:10.5678/xyz") == "10.5678/xyz"


def test_normalize_doi_rejects_non_doi_shape():
    assert normalize_doi("not-a-doi") is None


def test_normalize_doi_none_input():
    assert normalize_doi(None) is None


def test_normalize_url_lowercases_and_strips_www():
    assert normalize_url("HTTPS://WWW.Example.com/Report") == "https://example.com/Report"


def test_normalize_url_collapses_http_to_https():
    assert normalize_url("http://example.com/x").startswith("https://")


def test_normalize_url_drops_fragment():
    assert "#" not in normalize_url("https://example.com/x#section2")


def test_normalize_url_strips_tracking_params():
    result = normalize_url("https://example.com/report?utm_source=twitter&id=42")
    assert "utm_source" not in result
    assert "id=42" in result


def test_normalize_url_preserves_meaningful_query_params():
    result = normalize_url("https://example.com/doc?report_id=7")
    assert "report_id=7" in result


def test_normalize_url_strips_trailing_slash_except_root():
    assert normalize_url("https://example.com/report/") == "https://example.com/report"
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_normalize_url_collapses_index_pages():
    # Collapsed to the directory path, then trailing-slash-stripped like any
    # other non-root path -- consistent with normalize_url's general rule.
    assert normalize_url("https://example.com/docs/index.html") == "https://example.com/docs"


def test_normalize_url_rejects_non_http_scheme():
    assert normalize_url("ftp://example.com/file") is None


def test_normalize_title_folds_unicode_and_case():
    assert normalize_title("Coûts de Forage") == "couts de forage"


def test_normalize_title_collapses_whitespace():
    assert normalize_title("Drilling   Cost   Report") == "drilling cost report"


def test_normalize_title_strips_surrounding_punctuation():
    assert normalize_title("  Drilling Cost Report.  ") == "drilling cost report"


def test_normalize_title_none_input():
    assert normalize_title(None) is None
