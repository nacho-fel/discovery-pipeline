# Discovery Pipeline

An upstream internet-discovery system for the MIT EGS/geothermal cost-evidence
research project: it finds, registers, deduplicates, screens, and acquires
candidate research resources (PDFs, datasets, spreadsheets, papers,
disclosures) that may contain extraction-cost evidence, then hands validated
files to the existing [geocost](../../document-to-db%20pipeline)
document-to-dataset pipeline via a versioned, self-hashed handoff manifest.

**This pipeline discovers and acquires research resources likely to generate
large numbers of evidence-backed cost and technical observations for the
existing geocost database. Source count is an intermediate metric; observation
yield, evidence quality and coverage are the final objectives.**

This is a **separate repository** from geocost by design: it owns its own
database, config, and CLI, and never writes into geocost's live database or
models directly. See [docs/handoff_contract.md](docs/handoff_contract.md) for
the exact contract between the two systems.

## Quick start

```bash
uv sync
cp .env.example .env   # fill in SERPAPI_API_KEY / CONTACT_EMAIL if you have them
uv run discovery init-database
```

## Commands

```bash
uv run discovery plan                          # build query plan, zero network calls
uv run discovery run --execute-network          # execute a bounded discovery run
uv run discovery run --execute-network --expand # ...plus one frontier-expansion round
uv run discovery resume <run_id> --execute-network
uv run discovery status <run_id>
uv run discovery review --status manual_review_required
uv run discovery acquire --execute-network
uv run discovery handoff <run_id>
uv run discovery report <run_id>

uv run discovery import csv seeds.csv
uv run discovery import jsonl seeds.jsonl
uv run discovery import url https://example.com/report.pdf

# Production discovery: adaptive, coverage-based, not a fixed request count
# -- see docs/adaptive_discovery.md.
uv run discovery adaptive-dry-run               # quota/batch estimate, zero network calls
uv run discovery adaptive-run --execute-network

# Restricted-access resources (licensed publishers, MIT-subscribed
# databases): this pipeline never automates that download -- see
# docs/mit_assisted_acquisition.md.
uv run discovery acquisition-queue --format csv
uv run discovery scan-inbox
```

Every command that would make a real HTTP call requires an explicit
`--execute-network` flag; without it, commands only plan/inspect. No live
provider calls are made in tests -- every adapter is exercised against
`httpx.MockTransport` fixtures.

**`DATABASE_URL` must be set explicitly for any command that touches a
non-default database** -- same convention as geocost, for the same reason
(multiple parallel working copies during development):

```bash
DATABASE_URL="sqlite:///data/some_copy.db" uv run discovery stats
```

## Development

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv run discovery migrate   # apply Alembic migrations
```

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full pipeline design
and [docs/handoff_contract.md](docs/handoff_contract.md) for the manifest
schema shared with geocost.
