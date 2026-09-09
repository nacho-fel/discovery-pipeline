"""Conservative, deterministic normalization of DOI / URL / title.

URL canonicalization only ever operates on the URL string as given -- it never
follows a redirect (that happens, deliberately, only in `acquirer.py` during
actual download, per the brief: "Follow safe redirects only during
acquisition, not normalization").
"""

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_DOI_PREFIX_RE = re.compile(r"^(https?://)?(dx\.)?doi\.org/", re.IGNORECASE)
_DOI_SCHEME_RE = re.compile(r"^doi:\s*", re.IGNORECASE)
_DOI_SHAPE_RE = re.compile(r"^10\.\d{4,9}/\S+$")

# Params that identify *tracking/session*, never a document's actual identity
# -- stripped so `?utm_source=twitter` and a bare link normalize identically.
# Deliberately conservative: any param not in this list is preserved, since a
# provider's real content-selecting param (e.g. a report `?id=`) must survive.
_TRACKING_PARAM_PREFIXES = ("utm_", "ref_", "fbclid", "gclid", "mc_cid", "mc_eid")
_TRACKING_PARAM_EXACT = {"ref", "source", "cid", "sid", "spm"}

_INDEX_PAGE_RE = re.compile(r"/(index|default)\.(html?|php|aspx?)$", re.IGNORECASE)


def normalize_doi(raw: str | None) -> str | None:
    """Normalize a DOI to bare lowercase `10.xxxx/yyyy` form, or None if unparseable."""
    if not raw:
        return None
    candidate = raw.strip()
    candidate = _DOI_PREFIX_RE.sub("", candidate)
    candidate = _DOI_SCHEME_RE.sub("", candidate)
    candidate = candidate.strip().strip("/").lower()
    if not _DOI_SHAPE_RE.match(candidate):
        return None
    return candidate


def normalize_url(raw: str | None) -> str | None:
    """Canonicalize a URL for dedup purposes: lowercase scheme/host, strip `www.`,
    collapse `http` to `https`, drop the fragment, strip trailing slash and
    trailing index pages, strip known tracking params, and sort the remaining
    query params for a stable key. Never follows redirects.
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https", ""):
        return None

    scheme = "https" if parsed.scheme in ("http", "https") else parsed.scheme
    netloc = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path or "/"
    path = _INDEX_PAGE_RE.sub("/", path)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    kept_params = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in _TRACKING_PARAM_EXACT:
            continue
        if any(key_lower.startswith(prefix) for prefix in _TRACKING_PARAM_PREFIXES):
            continue
        kept_params.append((key, value))
    kept_params.sort()
    query = urlencode(kept_params, doseq=True)

    return urlunparse((scheme, netloc, path, "", query, ""))


def normalize_title(raw: str | None) -> str | None:
    """Conservatively normalize a title for matching: unicode-fold, lowercase,
    collapse whitespace, and strip surrounding punctuation. Never rewrites
    internal words (no stopword removal, no stemming) -- that belongs to fuzzy
    matching, not this deterministic pass.
    """
    if not raw:
        return None
    folded = unicodedata.normalize("NFKD", raw)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.lower()
    folded = re.sub(r"\s+", " ", folded).strip()
    folded = folded.strip(" .:-—–")  # noqa: RUF001 -- em/en dash are deliberate, not typos
    return folded or None
