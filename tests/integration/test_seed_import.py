import json

from discovery.adapters.seed_import import import_delimited, import_jsonl, import_single_url


def test_import_csv(tmp_path):
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text(
        "url,title,doi,authors,year\n"
        "https://example.com/a.pdf,Report A,10.1/a,\"Jane Doe, John Roe\",2021\n"
        "https://example.com/b.pdf,Report B,,,\n",
        encoding="utf-8",
    )
    hits = import_delimited(csv_path, delimiter=",")
    assert len(hits) == 2
    assert hits[0].title == "Report A"
    assert hits[0].doi == "10.1/a"
    assert hits[0].authors == ["Jane Doe", "John Roe"]
    assert hits[0].publication_year == 2021
    assert hits[0].rank == 1


def test_import_csv_skips_rows_without_url(tmp_path):
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text("url,title\n,Missing URL\nhttps://example.com/c.pdf,Has URL\n", encoding="utf-8")
    hits = import_delimited(csv_path, delimiter=",")
    assert len(hits) == 1
    assert hits[0].title == "Has URL"


def test_import_tsv(tmp_path):
    tsv_path = tmp_path / "seeds.tsv"
    tsv_path.write_text("url\ttitle\nhttps://example.com/d.pdf\tReport D\n", encoding="utf-8")
    hits = import_delimited(tsv_path, delimiter="\t")
    assert len(hits) == 1
    assert hits[0].title == "Report D"


def test_import_jsonl(tmp_path):
    jsonl_path = tmp_path / "seeds.jsonl"
    lines = [
        json.dumps({"url": "https://example.com/e.pdf", "title": "Report E", "doi": "10.1/e"}),
        json.dumps({"url": "https://example.com/f.pdf", "title": "Report F"}),
        "",  # blank lines are skipped
    ]
    jsonl_path.write_text("\n".join(lines), encoding="utf-8")
    hits = import_jsonl(jsonl_path)
    assert len(hits) == 2
    assert hits[0].doi == "10.1/e"


def test_import_single_url():
    hit = import_single_url("https://example.com/g.pdf", title="Report G", doi="10.1/g")
    assert hit.url == "https://example.com/g.pdf"
    assert hit.title == "Report G"
    assert hit.doi == "10.1/g"
