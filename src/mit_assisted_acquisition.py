"""MIT-assisted acquisition workflow for restricted-access resources.

Per the explicit security constraint governing this whole pipeline: it never
stores or requests MIT Kerberos credentials, passwords, MFA tokens, or
browser cookies, and never automates systematic downloading from licensed
publishers. For any candidate whose `access_status` is restricted
(`licensed_mit_access`, `authentication_required`, `manual_acquisition_required`,
`unavailable` -- see `access_policy.is_restricted_access`), the only
supported path is:

  1. Export a prioritized queue (`build_acquisition_queue` /
     `export_queue_csv` / `export_queue_markdown` / `export_queue_html`) a
     human researcher reads: title, DOI, publisher, landing URL, a DOI-
     resolver or scholarly-search lookup link, predicted observation
     yield, and desired file formats.
  2. The researcher manually retrieves the file themselves -- via MIT VPN,
     Touchstone, LibKey Nomad, or any other route THEY control, entirely
     outside this pipeline -- and drops it into a local "manual
     acquisition inbox" directory, named `<candidate_id>.<ext>` (or listed
     in an optional explicit mapping file for filenames that can't carry
     the candidate id).
  3. `scan_inbox` matches inbox files back to queued candidates, validates
     format/hash exactly the way `acquirer.py`'s automated downloads are
     validated, and advances each match through the existing state
     machine (`paywalled -> manual_review_required -> acquisition_pending
     -> downloaded`, then `handoff.validate_and_prepare_candidate`) so a
     manually-acquired file reaches the handoff manifest through the
     identical validation path as an automated one -- with provenance
     recorded as `access_route="manual_acquisition"`, never a credential
     of any kind.
"""

import csv
import io
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy.orm import Session

from discovery import state_machine
from discovery.access_policy import is_restricted_access
from discovery.db.models import SourceCandidate
from discovery.handoff import validate_and_prepare_candidate
from discovery.hashing import file_sha256

logger = logging.getLogger(__name__)

# Formats this pipeline's own validation (magic-byte + content-type checks in
# acquirer.py) already knows how to confirm; manual acquisition is limited to
# the same allowlist so nothing downstream sees a format it can't validate.
_ALLOWED_MANUAL_FORMATS = {
    "pdf": ".pdf",
    "xlsx": ".xlsx",
    "xls": ".xls",
    "csv": ".csv",
    "tsv": ".tsv",
    "zip": ".zip",
    "docx": ".docx",
    "pptx": ".pptx",
    "json": ".json",
    "xml": ".xml",
}

_RESTRICTED_QUEUE_STATUSES = ("paywalled", "manual_review_required")
_INBOX_ELIGIBLE_STATUSES = ("paywalled", "manual_review_required", "acquisition_pending")


@dataclass(frozen=True)
class QueueEntry:
    candidate_id: str
    title: str | None
    doi: str | None
    publisher: str | None
    landing_url: str | None
    lookup_url: str | None
    access_status: str | None
    predicted_observation_yield: int
    desired_formats: list[str]


def _lookup_url(candidate: SourceCandidate) -> str | None:
    """A DOI resolver link when a DOI is known (always correct, and MIT's
    own link resolver intercepts it appropriately over VPN/Touchstone for
    subscribed content); otherwise a title-based Google Scholar search as a
    "find this" fallback. Deliberately never a fabricated MIT-specific deep
    link -- see module docstring.
    """
    if candidate.doi:
        return f"https://doi.org/{candidate.doi}"
    if candidate.normalized_title:
        return f"https://scholar.google.com/scholar?q={quote_plus(candidate.normalized_title)}"
    return None


def build_acquisition_queue(db: Session) -> list[QueueEntry]:
    """Every candidate currently routed to a restricted-access path,
    prioritized by predicted cost+technical observation yield (highest
    first) -- see screener.py's yield-aware ranking for how that predicted
    yield is set.
    """
    candidates = (
        db.query(SourceCandidate)
        .filter(SourceCandidate.screening_status.in_(_RESTRICTED_QUEUE_STATUSES))
        .filter(SourceCandidate.access_status.isnot(None))
        .all()
    )
    entries = []
    for c in candidates:
        if not is_restricted_access(c.access_status):
            continue
        predicted = (c.expected_cost_observation_yield or 0) + (
            c.expected_technical_observation_yield or 0
        )
        entries.append(
            QueueEntry(
                candidate_id=c.id,
                title=c.normalized_title,
                doi=c.doi,
                publisher=c.publisher,
                landing_url=c.canonical_url,
                lookup_url=_lookup_url(c),
                access_status=c.access_status,
                predicted_observation_yield=predicted,
                desired_formats=[c.file_format] if c.file_format else [],
            )
        )
    entries.sort(key=lambda e: e.predicted_observation_yield, reverse=True)
    return entries


_CSV_HEADER = [
    "candidate_id",
    "title",
    "doi",
    "publisher",
    "landing_url",
    "lookup_url",
    "access_status",
    "predicted_observation_yield",
    "desired_formats",
]


def export_queue_csv(entries: list[QueueEntry]) -> str:
    """Render `entries` as CSV text."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_HEADER)
    for e in entries:
        writer.writerow(
            [
                e.candidate_id,
                e.title or "",
                e.doi or "",
                e.publisher or "",
                e.landing_url or "",
                e.lookup_url or "",
                e.access_status or "",
                e.predicted_observation_yield,
                ";".join(e.desired_formats),
            ]
        )
    return buffer.getvalue()


def export_queue_markdown(entries: list[QueueEntry]) -> str:
    """Render `entries` as a Markdown table."""
    lines = [
        "| Candidate | Title | DOI | Publisher | Landing URL | Lookup | Access | Yield |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            f"| {e.candidate_id[:8]} | {e.title or ''} | {e.doi or ''} | {e.publisher or ''} "
            f"| {e.landing_url or ''} | {e.lookup_url or ''} | {e.access_status or ''} "
            f"| {e.predicted_observation_yield} |"
        )
    return "\n".join(lines) + "\n"


def export_queue_html(entries: list[QueueEntry]) -> str:
    """Render `entries` as a simple HTML table."""
    rows = "\n".join(
        f"<tr><td>{e.candidate_id}</td><td>{e.title or ''}</td><td>{e.doi or ''}</td>"
        f'<td>{e.publisher or ""}</td><td><a href="{e.landing_url or "#"}">{e.landing_url or ""}</a></td>'
        f'<td><a href="{e.lookup_url or "#"}">{e.lookup_url or ""}</a></td>'
        f"<td>{e.access_status or ''}</td><td>{e.predicted_observation_yield}</td></tr>"
        for e in entries
    )
    return (
        "<table><thead><tr><th>Candidate</th><th>Title</th><th>DOI</th><th>Publisher</th>"
        "<th>Landing URL</th><th>Lookup</th><th>Access</th><th>Yield</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


@dataclass(frozen=True)
class InboxMatchResult:
    filename: str
    candidate_id: str | None
    matched: bool
    validated: bool
    reason: str | None


def scan_inbox(
    db: Session,
    inbox_dir: Path,
    *,
    acquired_root: Path,
    mapping: dict[str, str] | None = None,
) -> list[InboxMatchResult]:
    """Match files in `inbox_dir` back to queued candidates, validate them,
    and advance matches through the state machine to `ingestion_ready`.

    Matching is filename-based, not content-sniffed guesswork: either the
    file is named `<candidate_id>.<ext>` directly, or `mapping` (filename ->
    candidate_id) supplies the association explicitly -- both are
    provenance a human controls, never an automated guess that could
    silently attach the wrong file to the wrong candidate. Nothing here
    ever reads, stores, or requests a credential of any kind -- the file is
    assumed to already be sitting in `inbox_dir`, however the researcher
    got it there.
    """
    mapping = mapping or {}
    results: list[InboxMatchResult] = []
    if not inbox_dir.exists():
        return results

    for path in sorted(inbox_dir.iterdir()):
        if not path.is_file():
            continue
        candidate_id = mapping.get(path.name) or path.stem
        candidate = db.query(SourceCandidate).filter(SourceCandidate.id == candidate_id).first()
        if candidate is None:
            results.append(
                InboxMatchResult(path.name, None, False, False, "no matching candidate_id")
            )
            continue

        ext = path.suffix.lower()
        file_format = next((fmt for fmt, e in _ALLOWED_MANUAL_FORMATS.items() if e == ext), None)
        if file_format is None:
            results.append(
                InboxMatchResult(
                    path.name, candidate.id, True, False, f"unrecognized format {ext!r}"
                )
            )
            continue

        if candidate.screening_status not in _INBOX_ELIGIBLE_STATUSES:
            results.append(
                InboxMatchResult(
                    path.name,
                    candidate.id,
                    True,
                    False,
                    f"candidate in unexpected state {candidate.screening_status!r}",
                )
            )
            continue

        acquired_root.mkdir(parents=True, exist_ok=True)
        dest = acquired_root / f"{candidate.id}{ext}"
        shutil.copy2(path, dest)

        if candidate.screening_status == "paywalled":
            state_machine.apply_transition(candidate, "manual_review_required")
        if candidate.screening_status == "manual_review_required":
            state_machine.apply_transition(candidate, "acquisition_pending")
        candidate.local_acquired_path = str(dest)
        candidate.sha256 = file_sha256(dest)
        candidate.file_format = file_format
        candidate.access_route = "manual_acquisition"
        state_machine.apply_transition(candidate, "downloaded")
        db.flush()

        validated = validate_and_prepare_candidate(db, candidate)
        results.append(
            InboxMatchResult(
                path.name, candidate.id, True, validated, None if validated else "validation failed"
            )
        )
        logger.info(
            "mit_assisted_acquisition.inbox_matched",
            # "filename" collides with LogRecord's own reserved attribute of
            # the same name (the source file of the logging call itself) --
            # passing it in `extra` always raises KeyError once this logger
            # actually reaches a handler. Latent until now: the alembic
            # fileConfig disable_existing_loggers bug (see alembic/env.py)
            # was silently disabling this logger before this line was ever
            # reached.
            extra={
                "candidate_id": candidate.id,
                "inbox_filename": path.name,
                "validated": validated,
            },
        )

    return results
