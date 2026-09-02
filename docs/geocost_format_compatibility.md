# geocost format-compatibility matrix

Read-only audit of what geocost's `DocumentParser`
(`src/geocost/parsing/parser.py` in the geocost repo) can actually process
today, against the full set of "research resource" formats this pipeline
now discovers/acquires. **Nothing in the geocost repo was modified to
produce this** -- this is an audit of its current state, informing which of
this pipeline's formats can already flow straight through
`discovery handoff` into geocost, and which need a future geocost-side
ingestion adapter before they're useful there.

## What the audit found

`DocumentParser.parse_file` dispatches purely on file extension:

```python
DOCLING_SUFFIXES: ClassVar[set[str]] = {".pdf", ".docx"}
LEGACY_EXCEL_SUFFIXES: ClassVar[set[str]] = {".xls"}
EXCEL_SUFFIXES: ClassVar[set[str]] = {".xlsx"}
CSV_SUFFIXES: ClassVar[set[str]] = {".csv"}
...
raise ValueError(f"Unsupported file format: {suffix}")
```

PDF and DOCX go through Docling (with a PyMuPDF/pdfplumber fallback for
documents Docling can't convert); XLS goes through a dedicated `xlrd`-based
importer, XLSX through `openpyxl`; CSV has its own direct importer. Every
other extension raises `ValueError` -- there is no generic/passthrough
handler.

## Compatibility matrix

| Format | This pipeline discovers it? | geocost can parse it today? | Notes |
|---|---|---|---|
| PDF | Yes (primary format) | **Yes** -- Docling primary, PyMuPDF/pdfplumber fallback | Fully compatible now |
| DOCX | Yes | **Yes** -- Docling | Fully compatible now |
| XLSX / XLS | Yes (new structured-data templates) | **Yes** -- dedicated `openpyxl`/`xlrd` importers | Fully compatible now; geocost's docstring specifically calls out "legacy Excel workbooks (e.g. EIA well-cost series)" as a motivating case |
| CSV | Yes | **Yes** -- dedicated CSV importer | Fully compatible now |
| TSV | Yes | **No** -- not in `CSV_SUFFIXES`, would raise `ValueError` | Needs a geocost-side adapter (likely trivial: TSV is CSV with a different delimiter) |
| ZIP archives + contained assets | Yes (`ResourceAsset`) | **No** -- no archive-extraction step | Needs a geocost-side adapter that unzips and re-dispatches each member by its own extension; this pipeline's `ResourceAsset` model already represents individual archive members as separate assets, so the data shape is ready on this side |
| HTML data tables | Yes | **No** -- no HTML/table-scraping path | Needs a geocost-side adapter (or this pipeline pre-extracting the table to CSV before handoff, which the existing CSV path would then already support) |
| JSON / XML datasets | Yes | **No** | Needs a geocost-side adapter; no existing generic-data-file importer to build from |
| PPTX | Yes | **No** -- outside `DOCLING_SUFFIXES` (Docling itself supports PPTX, but geocost's `DOCLING_SUFFIXES` doesn't include it) | Likely the *cheapest* gap to close on geocost's side: extending `DOCLING_SUFFIXES` to include `.pptx` may be sufficient, since the underlying Docling converter already handles it -- but that is a geocost-repo change, out of scope here |
| Dataset landing pages, no single downloadable file (metadata-only) | Yes (`access_status="metadata_only"`) | **N/A** -- there's no file to parse | Not a parser gap; these never reach `discovery handoff` as a file-bearing entry in the first place |

## What this means for `discovery handoff` today

Four of the nine formats this pipeline now discovers (PDF, DOCX, XLSX/XLS,
CSV) already flow straight through geocost's existing parser with zero
changes on either side. The remaining five (TSV, ZIP, HTML tables,
JSON/XML, PPTX) are real candidates this pipeline can acquire and validate,
but geocost cannot yet parse them -- a candidate acquired in one of these
formats should still be tracked and represented (its `ResourceAsset`/
`file_format` fields make that explicit), but a handoff manifest listing
it as `ready_for_ingestion=True` would currently fail on geocost's side.
This pipeline does not attempt to work around that by pre-converting
formats geocost can't read; it surfaces the gap here instead, since closing
it is a geocost-side decision.
