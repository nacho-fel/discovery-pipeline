"""Domain-based access-status inference, loaded from config/access_policy.yaml.

Applied at candidate-normalization time (before screening, before any
acquisition attempt) so a known-restricted repository (OnePetro, SPE,
Elsevier, Wiley, Springer, ...) is preserved as a metadata-only candidate
from the moment it's created -- see `screener.py`'s
`screened_accept -> paywalled` routing, which skips straight to that state
for any RESTRICTED_ACCESS_STATUSES value instead of ever creating an
`AcquisitionAttempt`.

Six access states, matching how a candidate can actually be obtained:
  - open_access             -- directly downloadable, no login/paywall
  - licensed_mit_access     -- available through an MIT Library license
                                 (typically via Touchstone/LibKey Nomad/VPN),
                                 never through stored credentials
  - authentication_required -- needs some login this pipeline cannot
                                 automate (not necessarily MIT-licensed)
  - manual_acquisition_required -- no known automatable path at all; a human
                                 must source it (a request form, an archive
                                 that requires manual navigation, etc.)
  - metadata_only           -- confirmed to exist but no file was ever
                                 retrievable, even by a human, at this time
  - unavailable             -- known to no longer be retrievable at all
                                 (dead link, withdrawn, etc.)

Never guessed automatically for a URL absent from `access_policy.yaml`:
`infer_access_status` returns `None` for anything unlisted, exactly as
before -- an unlisted domain is not assumed "unavailable", it's simply
unclassified until the acquirer's own runtime response settles it.
"""

from pathlib import Path
from urllib.parse import urlparse

import yaml

ACCESS_STATES = frozenset(
    {
        "open_access",
        "licensed_mit_access",
        "authentication_required",
        "manual_acquisition_required",
        "metadata_only",
        "unavailable",
    }
)

# Any of these means "do not attempt an automated download" -- screener.py
# routes an accepted candidate straight to the `paywalled` (metadata-only)
# screening state for any of them, never into `acquisition_pending`. This
# pipeline never stores or requests credentials of any kind (see
# mit_assisted_acquisition.py), so anything short of `open_access` is, by
# definition, a candidate this code cannot fetch itself.
#
# `metadata_only` belongs here too even though nothing is "restricted" about
# it in the licensed-access sense -- it means "confirmed to exist but no
# file was ever retrievable, even by a human", so attempting an automated
# download is pointless *and* unsafe to treat as an open resource. It was
# missing from this set until a 2026-08-21 access-state audit found it,
# which meant an accepted metadata_only candidate could reach
# `acquisition_pending` and have `acquire()` actually invoked on it -- see
# `is_automatable_access` below for the positive-allowlist check that now
# guards every automated-acquisition entry point directly, not just this
# denylist.
RESTRICTED_ACCESS_STATUSES = frozenset(
    {
        "licensed_mit_access",
        "authentication_required",
        "manual_acquisition_required",
        "metadata_only",
        "unavailable",
    }
)


def load_access_policy(path: Path) -> dict[str, str]:
    """Load the domain -> access_status mapping."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mapping: dict[str, str] = data.get("domain_access_status", {})
    return mapping


def infer_access_status(url: str | None, *, policy: dict[str, str]) -> str | None:
    """Return the known access status for `url`'s domain, or None if unlisted.

    Matches the domain and any of its parent domains (e.g. a subdomain of a
    listed restricted-access publisher still counts), never a substring match.
    """
    if not url:
        return None
    hostname = urlparse(url).hostname
    if not hostname:
        return None
    hostname = hostname.lower().removeprefix("www.")

    parts = hostname.split(".")
    for start in range(len(parts) - 1):
        candidate_domain = ".".join(parts[start:])
        if candidate_domain in policy:
            return policy[candidate_domain]
    return None


def is_restricted_access(access_status: str | None) -> bool:
    """True if `access_status` means "do not attempt an automated download"."""
    return access_status in RESTRICTED_ACCESS_STATUSES


def is_automatable_access(access_status: str | None) -> bool:
    """True only for the cases automated acquisition may actually proceed
    for: a confirmed `open_access` candidate, or an unclassified one
    (`None` -- no domain policy matched at normalization time).

    This is the positive allowlist counterpart to `is_restricted_access`,
    and is deliberately the check every automated-acquisition entry point
    (`cli.py`'s `acquire` command, `acquisition_pilot.py`,
    `multi_format_pilot.py`) applies immediately before calling
    `acquirer.acquire()` -- not just relying on a candidate having reached
    `acquisition_pending` through the "correct" screening path, since a
    manual reviewer or a future code path could in principle transition a
    restricted candidate there directly (see `state_machine.py`'s
    `manual_review_required -> acquisition_pending` edge).

    `None` is intentionally still automatable: `acquirer.py`'s own runtime
    checks (SSRF, 401/403 handling, magic-byte validation) are the actual
    authoritative safety gate for a URL this pipeline has no prior
    classification for -- "unclassified" is not the same claim as
    "confirmed restricted", and treating every unlisted domain as
    forbidden would silently starve discovery of anything not already in
    `config/access_policy.yaml`. A URL that turns out to require auth is
    caught at request time (`error_type="paywalled_or_forbidden"`) and
    never silently accepted.
    """
    return access_status is None or access_status == "open_access"
