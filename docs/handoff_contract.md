# Handoff contract with geocost

This repo and geocost (currently at `MIT/Agentic System/Agentic System` --
its parent folder was renamed by something outside this build's control
mid-session; see the top-level report) are deliberately **separate,
loosely-coupled systems**. This repo never writes into geocost's database or
imports its code; geocost is never modified by this repo. The only
connection is a **file**: a versioned, self-hashed JSON manifest.

## Producing a manifest

```bash
uv run discovery handoff <discovery_run_id>
```

This validates every candidate currently `downloaded` (file still exists,
hash still matches, has enough metadata to be worth ingesting), advances
valid ones to `ingestion_ready` then `handed_off`, and writes
`data/handoff/manifest_<run_id>_<hash_prefix>.json`.

## Schema

```json
{
  "schema_version": "1.0",
  "discovery_run_id": "uuid",
  "generated_at": "iso-8601 timestamp",
  "entry_count": 2,
  "entries": [
    {
      "candidate_id": "uuid",
      "source_document_id": "uuid-or-null-until-registration",
      "local_path": "absolute-or-relative-path-to-the-acquired-file",
      "sha256": "hex",
      "title": "string-or-null",
      "authors": ["string", "..."],
      "publication_year": 2022,
      "doi": "string-or-null",
      "canonical_url": "string-or-null",
      "organization": "string-or-null",
      "jurisdiction": "string-or-null",
      "source_type": "government_technical_report",
      "evidence_tier": "government_technical",
      "technology_domains": ["egs", "oil_and_gas"],
      "expected_evidence": ["drilling_cost", "rate_of_penetration"],
      "discovery_run_id": "uuid",
      "ready_for_ingestion": true
    }
  ],
  "manifest_sha256": "hex -- sha256 over the entries list, sort_keys=True JSON"
}
```

`manifest_sha256` is computed by `discovery.fingerprint.manifest_fingerprint`
over the entries' own serialized form -- the same self-hashing shape
geocost's own `extraction/selection_manifest.py` uses for its own
`manifest_sha256`, so a consumer can verify the file wasn't hand-edited after
being written.

## Compatibility verification (this build)

geocost was **not modified**. Everything below was verified by (a) reading
`geocost/db/models.py` and `config/source_manifest.yaml` directly at their
current on-disk location, and (b) generating a real manifest end-to-end,
offline, from this repo:

```python
# Built a candidate through the real dedup/normalization path, wrote a
# fake-but-magic-byte-valid PDF, drove it through the real state machine
# (screened_accept -> acquisition_pending -> downloaded), then called the
# real validate_and_prepare_candidate() + build_manifest().
```

Result: a well-formed manifest was written, `HandoffManifest.model_validate_json`
round-tripped it cleanly, and `manifest_fingerprint()` recomputed over the
loaded entries matched the stored `manifest_sha256` exactly. The schema
itself is sound and ready to be consumed.

### Finding: the originally-drafted importer example was wrong

The first version of this document illustrated inserting a `SourceDocument`
row directly via geocost's ORM. That's a real bug, not just a style choice:
geocost's actual ingestion path (`IngestionPipeline.ingest_document`)
computes the SHA-256, runs Docling parsing, and creates `DocumentElement`/
`DocumentChunk` rows **in the same step** as creating the `SourceDocument`
row. A hand-inserted `SourceDocument` row skips all of that -- it would sit
in the database with `ingestion_status="pending"`, zero chunks, zero
elements, and be invisible to retrieval/extraction. It would look imported
but not actually be usable.

**Correct approach**, verified against `geocost/ingestion/pipeline.py` and
`config/source_manifest.yaml`'s real schema (both read directly, unmodified):

```python
# ILLUSTRATIVE -- run inside geocost's own venv, using its own CLI/config.
# Not executed anywhere in this build; no code was added to geocost.
import hashlib
import shutil
import json
from pathlib import Path

import yaml

manifest = json.loads(Path("manifest_xxx.json").read_text())
corpus_root = Path("data/corpus")          # geocost's Settings.corpus_root
manifest_path = Path("config/source_manifest.yaml")

def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

source_manifest = yaml.safe_load(manifest_path.read_text()) or {"sources": []}
existing_by_sha = {s.get("sha256") for s in source_manifest["sources"]}

for entry in manifest["entries"]:
    if entry["sha256"] in existing_by_sha:
        continue  # already registered

    source_hash = entry["sha256"]
    technology_domains = entry["technology_domains"]
    if len(technology_domains) > 1:
        raise ValueError(
            "Multiple technology_domains are not supported by geocost's singular "
            "technology_domain field"
        )

    source_path = Path(entry["local_path"])
    dest = corpus_root / f"{source_path.stem}_{source_hash[:12]}{source_path.suffix}"
    if dest.exists():
        if file_sha256(dest) != source_hash:
            raise ValueError(f"Destination already exists with a different SHA-256: {dest}")
    else:
        shutil.copy2(entry["local_path"], dest)

    source_manifest["sources"].append({
        "filename": dest.name,
        "title": entry["title"],
        "authors": "; ".join(entry["authors"]) if entry["authors"] else None,
        "publication_year": entry["publication_year"],
        "source_type": entry["source_type"],
        "evidence_tier": entry["evidence_tier"],
        # geocost's technology_domain is a single value; multiple domains were
        # rejected above instead of being silently reduced.
        "technology_domain": (technology_domains or [None])[0],
        "jurisdiction": entry["jurisdiction"],
        "doi_or_url": entry["doi"] or entry["canonical_url"],
        "sha256": entry["sha256"],
        "notes": f"Discovered via discovery-pipeline run {entry['discovery_run_id']}",
        "reviewed": False,   # descriptive only in the current pipeline.py --
                              # not currently code-enforced, see note below
        "included": True,    # this IS enforced: pipeline.py skips a source
                              # whose manifest entry has included: false
    })
    existing_by_sha.add(source_hash)

manifest_path.write_text(yaml.safe_dump(source_manifest, sort_keys=False, allow_unicode=True))
# Then, separately, an operator runs the real geocost pipeline, which does
# the SHA-256 dedup / Docling parse / chunk / canonical-work reconciliation
# itself:
#   uv run geocost ingest --input ./data/corpus
```

This is simpler than the original draft *and* a cleaner boundary: it's a
file copy plus a YAML append, never a direct write into geocost's database,
and geocost's own SHA-256-based dedup (`ingestion/dedup.py`) makes re-running
the copy/append step safe (a byte-identical file already in the corpus is a
no-op at ingest time; this importer's own `existing_by_sha` check additionally
avoids even attempting a duplicate copy/append).

### Remaining integration work (not decided unilaterally by this repo)

1. **`technology_domain` is singular in geocost, plural in this repo's
   candidates.** geocost's column is `String(64)`, one value; a discovered
   candidate can legitimately span more than one (e.g. `["egs",
   "oil_and_gas"]` for a comparative study). The importer above takes the
   first value as the simplest default -- whoever owns geocost's schema
   should decide whether that's acceptable or whether the column needs to
   become a join table / denormalized CSV.
2. **`mime_type` is not populated by this repo's manifest at all.**
   `AcquisitionAttempt.content_type` (already captured, e.g.
   `"application/pdf"`) is the natural source; the importer sketch above
   doesn't set it, since deciding whether to map it 1:1 or re-derive it from
   the file extension during geocost's own parse step is a geocost-side call.
3. **`source_document_id` stays `null` in this repo's `source_candidate`
   table** after a manifest is produced -- this repo's database is never
   informed which `SourceDocument.id` geocost eventually assigned. Closing
   this loop (e.g. a small read-only reconciliation script that looks up
   geocost's `SourceDocument` by `sha256` and writes the id back) is
   possible but wasn't built, since it would be the first place this repo's
   database accepts information derived from geocost, and that coupling
   direction deserves an explicit decision rather than being added
   unilaterally.
4. **Path portability.** `local_path` in a manifest generated on Windows
   contains backslashes (confirmed in the generated example above); a
   geocost host on Linux/macOS would need `Path(entry["local_path"])`
   (which normalizes this automatically) rather than raw string handling.
5. **`reviewed` in `source_manifest.yaml` is currently decorative.** Grepping
   `geocost/ingestion/pipeline.py` found only `included: false` actually
   gates ingestion; `reviewed` is set by existing curated entries but isn't
   read anywhere in the code. Don't rely on it as a real gate without
   confirming that's still true when this integration is actually built.

None of the above blocks the manifest format itself -- they're decisions
about the geocost-side consumer, which this build deliberately left
unmade rather than encoding a guess into geocost's schema.
