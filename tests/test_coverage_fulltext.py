from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

from reportlab.pdfgen import canvas

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if not SCRIPTS.is_dir():
    SCRIPTS = Path(__file__).resolve().parents[2] / "literature-harvester" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lit_harvest.coverage import build_coverage
from lit_harvest.downloader import download_record, sorted_candidates
from lit_harvest.fulltext import (
    FullTextError, extract_pdf_url, fetch_jats_fulltext,
    jats_to_markdown, resolve_landing_pdf,
)
from lit_harvest.models import PaperRecord
from lit_harvest.sources import enrich_preprints_by_title, _pdf_urls_in_structure

LANDING_HTML = b"""<html><head>
<meta property="citation_pdf_url" content="/content/10.1000/test.pdf">
</head><body>landing</body></html>"""


class _OfflineTests(unittest.TestCase):
    """Keep URL validation real, but never perform DNS or network I/O."""

    def setUp(self):
        dns = patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ])
        connect = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        opener = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden"))
        for guard in (dns, connect, opener):
            guard.start()
            self.addCleanup(guard.stop)


class _Response:
    def __init__(self, body: bytes, headers=None, url=None):
        self.body = body
        self.headers = headers or {}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit=-1):
        if limit < 0:
            limit = len(self.body)
        chunk, self.body = self.body[:limit], self.body[limit:]
        return chunk

    def read1(self, size=-1):
        return self.read(size)


class _SequenceClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, headers=None):
        self.calls.append((url, headers or {}))
        response = self.responses.pop(0)
        response.url = response.url or url
        return response

    def get_json(self, url, params=None, headers=None):
        raise AssertionError(f"unexpected get_json call: {url}")


def _synthetic_pdf(record):
    """A real PDF whose first-page title and DOI exercise the identity parser."""
    output = BytesIO()
    document = canvas.Canvas(output, invariant=1)
    document.drawString(48, 780, record.title)
    document.drawString(48, 760, "DOI: " + record.doi)
    document.drawString(48, 740, "Synthetic test fixture. Not a published paper.")
    document.showPage()
    document.save()
    return output.getvalue()


def _jats_fixture(pmcid="PMC1234567", title="PMC XML synthetic paper"):
    return (
        '<article><front><article-meta><article-id pub-id-type="pmcid">'
        + escape(pmcid) + '</article-id><title-group><article-title>'
        + escape(title) + '</article-title></title-group></article-meta></front>'
        '<body><sec><title>Results</title><p>' + "word " * 300
        + '</p></sec></body></article>'
    ).encode()


def _add_qualified_jats(record):
    url = (
        "https://pmc-oa-opendata.s3.amazonaws.com/"
        f"{record.pmcid}.1/{record.pmcid}.1.xml"
    )
    record.add_pdf_candidate(
        url, "pmc_oa_cloud", "CC BY", kind="jats", pmcid=record.pmcid,
        article_version=1, oa_route="open_access",
        metadata_url=f"https://pmc-oa-opendata.s3.amazonaws.com/metadata/{record.pmcid}.1.json",
    )
    return url


class FullTextTests(_OfflineTests):
    def test_extract_pdf_url_handles_both_attribute_orders(self) -> None:
        base = "https://repo.example.org/record/1"
        forward = '<meta name="citation_pdf_url" content="/files/paper.pdf">'
        backward = '<meta content="/files/other.pdf" name="citation_pdf_url">'
        self.assertEqual(extract_pdf_url(forward, base), "https://repo.example.org/files/paper.pdf")
        self.assertEqual(extract_pdf_url(backward, base), "https://repo.example.org/files/other.pdf")
        self.assertEqual(extract_pdf_url("<html>nothing</html>", base), "")

    def test_resolve_landing_pdf_follows_declared_url(self) -> None:
        client = _SequenceClient(_Response(LANDING_HTML))
        self.assertEqual(
            resolve_landing_pdf(client, "https://example.org/landing"),
            "https://example.org/content/10.1000/test.pdf",
        )
        self.assertEqual(len(client.calls), 1)

    def test_jats_conversion_requires_usable_body(self) -> None:
        xml = _jats_fixture(title="Test")
        markdown = jats_to_markdown(xml)
        self.assertIn("# Test", markdown)
        self.assertIn("## Results", markdown)
        self.assertIn("word word word", markdown)
        with self.assertRaises(FullTextError) as raised:
            jats_to_markdown(b"<article><body><p>short</p></body></article>")
        self.assertEqual(raised.exception.code, "no_usable_body")

    def test_fetch_jats_rejects_non_xml(self) -> None:
        client = _SequenceClient(_Response(b"<html>error</html>"))
        with self.assertRaises(FullTextError) as raised:
            fetch_jats_fulltext(client, "PMC1234567")
        self.assertEqual(raised.exception.code, "invalid_xml")


class PreprintEnrichmentTests(_OfflineTests):
    def test_title_match_accepts_exact_title_only(self) -> None:
        client = type(
            "Fake",
            (),
            {
                "get_json": lambda self, url, params=None: {
                    "resultList": {
                        "result": [
                            {
                                "title": "A different manuscript",
                                "pubYear": 2024,
                                "authorList": {"author": [{"fullName": "Jane Smith"}]},
                                "fullTextUrlList": {"fullTextUrl": [{"documentStyle": "pdf", "url": "https://x/a.pdf"}]},
                            },
                            {
                                "title": "Sepsis associated encephalopathy review",
                                "pubYear": 2024,
                                "authorList": {"author": [{"fullName": "Alice Zhang"}]},
                                "doi": "10.1101/2024.01.01.000001",
                                "fullTextUrlList": {"fullTextUrl": [{"documentStyle": "pdf", "url": "https://x/b.pdf"}]},
                            },
                        ]
                    }
                },
            },
        )()
        record = PaperRecord(
            title="Sepsis associated encephalopathy review",
            authors=["Alice Zhang"],
            year=2024,
            doi="10.1038/test",
        )
        enriched, attempted, errors = enrich_preprints_by_title(client, [record])
        self.assertEqual(attempted, 1)
        self.assertEqual(enriched, 1)
        self.assertFalse(errors)
        self.assertIn("europe_pmc_preprint", {c["source"] for c in record.pdf_candidates})

    def test_title_match_resolves_latest_10_1101_version_via_official_api(self) -> None:
        class Fake:
            def get_json(self, url, params=None):
                if "europepmc" in url:
                    return {
                        "resultList": {
                            "result": [
                                {
                                    "title": "Sepsis associated encephalopathy review",
                                    "pubYear": 2024,
                                    "authorList": {"author": [{"fullName": "Alice Zhang"}]},
                                    "doi": "10.1101/2024.01.01.000001",
                                }
                            ]
                        }
                    }
                if "/medrxiv/" in url:
                    return {
                        "collection": [
                            {
                                "doi": "10.1101/2024.01.01.000001",
                                "version": "1",
                                "license": "cc_by_nc_nd",
                            },
                            {
                                "doi": "10.1101/2024.01.01.000001",
                                "version": "3",
                                "license": "cc_by_nc_nd",
                            },
                        ]
                    }
                return {"collection": []}

        record = PaperRecord(
            title="Sepsis associated encephalopathy review",
            authors=["Alice Zhang"],
            year=2024,
            doi="10.1038/test",
        )
        enriched, attempted, errors = enrich_preprints_by_title(Fake(), [record])
        self.assertEqual((enriched, attempted, errors), (1, 1, []))
        candidate = next(item for item in record.pdf_candidates if item["source"] == "medrxiv")
        self.assertEqual(
            candidate["url"],
            "https://www.medrxiv.org/content/10.1101/2024.01.01.000001v3.full.pdf",
        )
        self.assertFalse(any(item["kind"] == "landing" for item in record.pdf_candidates))

    def test_title_match_keeps_doi_landing_when_official_api_has_no_record(self) -> None:
        class Fake:
            def get_json(self, url, params=None):
                if "europepmc" in url:
                    return {
                        "resultList": {
                            "result": [
                                {
                                    "title": "Sepsis associated encephalopathy review",
                                    "pubYear": 2024,
                                    "authorList": {"author": [{"fullName": "Alice Zhang"}]},
                                    "doi": "10.1101/2024.01.01.000001",
                                }
                            ]
                        }
                    }
                return {"collection": []}

        record = PaperRecord(
            title="Sepsis associated encephalopathy review",
            authors=["Alice Zhang"],
            year=2024,
            doi="10.1038/test",
        )
        enriched, attempted, errors = enrich_preprints_by_title(Fake(), [record])
        self.assertEqual((enriched, attempted, errors), (1, 1, []))
        self.assertEqual(record.pdf_candidates[0]["source"], "europe_pmc_preprint_landing")
        self.assertEqual(record.pdf_candidates[0]["kind"], "landing")

    def test_title_match_rejects_author_year_mismatch(self) -> None:
        client = type(
            "Fake",
            (),
            {
                "get_json": lambda self, url, params=None: {
                    "resultList": {
                        "result": [
                            {
                                "title": "Sepsis associated encephalopathy review",
                                "pubYear": 2018,
                                "authorList": {"author": [{"fullName": "Bob Li"}]},
                                "fullTextUrlList": {"fullTextUrl": [{"documentStyle": "pdf", "url": "https://x/b.pdf"}]},
                            }
                        ]
                    }
                },
            },
        )()
        record = PaperRecord(
            title="Sepsis associated encephalopathy review",
            authors=["Alice Zhang"],
            year=2024,
            doi="10.1038/test",
        )
        enriched, _attempted, _errors = enrich_preprints_by_title(client, [record])
        self.assertEqual(enriched, 0)
        self.assertEqual(record.pdf_candidates, [])

    def test_pdf_urls_structure_walker(self) -> None:
        payload = {"response": {"results": {"result": [{"file": {"url": "https://x/paper.pdf"}}]}}}
        self.assertEqual(_pdf_urls_in_structure(payload), ["https://x/paper.pdf"])


class DownloaderRecoveryTests(_OfflineTests):
    def test_landing_candidate_resolves_then_downloads(self) -> None:
        record = PaperRecord(
            title="Landing test paper with synthetic verified identity", doi="10.1000/test"
        )
        pdf_body = _synthetic_pdf(record)
        client = _SequenceClient(
            _Response(LANDING_HTML),
            _Response(pdf_body, url="https://example.org/content/10.1000/test.pdf"),
        )
        record.add_pdf_candidate("https://example.org/landing", "unpaywall_landing", kind="landing")
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("lit_harvest.downloader.HttpClient", return_value=client):
                download_record(record, Path(temp_dir) / "pdfs", allow_fulltext_fallback=False)
            self.assertEqual(record.download_status, "downloaded", record.failure_reason)
            self.assertEqual(record.identity_status, "verified")
            self.assertEqual(Path(record.local_pdf).read_bytes(), pdf_body)
            self.assertEqual(record.sha256, hashlib.sha256(pdf_body).hexdigest())
        self.assertEqual(client.calls[1][1]["Referer"], "https://example.org/landing")
        self.assertEqual(record.retrieval_type, "legal_oa_pdf")
        self.assertEqual(len(client.calls), 2)

    def test_jats_fallback_when_no_pdf_exists(self) -> None:
        record = PaperRecord(title="PMC XML synthetic paper", pmcid="PMC1234567")
        url = _add_qualified_jats(record)
        client = _SequenceClient(_Response(_jats_fixture(record.pmcid)))
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("lit_harvest.downloader.HttpClient", return_value=client):
                download_record(record, Path(temp_dir) / "pdfs")
            self.assertEqual(record.download_status, "fulltext_only", record.failure_reason)
            self.assertTrue(Path(record.local_fulltext).is_file())
            self.assertIn("word word word", Path(record.local_fulltext).read_text(encoding="utf-8"))
            self.assertEqual(Path(record.local_fulltext).parent.parent, Path(temp_dir))
        self.assertTrue(record.local_fulltext.endswith(".fulltext.md"))
        self.assertEqual(Path(record.local_fulltext).parent.name, "fulltext")
        self.assertEqual(record.fulltext_format, "markdown_from_jats")
        self.assertEqual(record.retrieval_type, "generated_jats_markdown")
        self.assertEqual(client.calls[0][0], url)
        self.assertFalse(record.local_pdf)

    def test_sorted_candidates_runs_direct_pdfs_before_landings(self) -> None:
        record = PaperRecord(title="Order test")
        record.add_pdf_candidate("https://x/landing", "core_landing", kind="landing")
        record.add_pdf_candidate("https://x/direct.pdf", "core")
        order = sorted_candidates(record)
        self.assertEqual(order[0]["url"], "https://x/direct.pdf")
        self.assertEqual(order[1]["kind"], "landing")


class CoverageReportTests(_OfflineTests):
    def test_coverage_counts_fulltext_and_buckets_failures(self) -> None:
        good = PaperRecord(title="Good synthetic paper with verified identity", doi="10.1000/good")
        pdf_body = _synthetic_pdf(good)
        good.add_pdf_candidate("https://example.org/good.pdf", "unpaywall")
        fulltext = PaperRecord(title="XML synthetic paper", pmcid="PMC1")
        _add_qualified_jats(fulltext)
        closed = PaperRecord(title="Closed paper", doi="10.1000/closed")
        closed.download_status = "no_oa_version"
        manifest = {"query": "test", "source_status": {}, "download_attempts": []}
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_dir = Path(temp_dir) / "pdfs"
            with patch("lit_harvest.downloader.HttpClient",
                       return_value=_SequenceClient(_Response(pdf_body))):
                download_record(good, pdf_dir, allow_fulltext_fallback=False)
            with patch("lit_harvest.downloader.HttpClient",
                       return_value=_SequenceClient(_Response(_jats_fixture("PMC1")))):
                download_record(fulltext, pdf_dir)
            self.assertEqual(good.download_status, "downloaded", good.failure_reason)
            self.assertEqual(good.identity_status, "verified")
            self.assertEqual(good.sha256, hashlib.sha256(Path(good.local_pdf).read_bytes()).hexdigest())
            self.assertEqual(fulltext.download_status, "fulltext_only", fulltext.failure_reason)
            self.assertTrue(Path(fulltext.local_fulltext).is_file())
            report = build_coverage(manifest, [good, fulltext, closed])
            self.assertEqual(report["record_count"], 3)
            self.assertEqual(report["downloaded"], 1)
            self.assertEqual(report["pdf_downloaded"], 1)
            self.assertEqual(report["fulltext_only"], 1)
            self.assertEqual(report["readable_fulltext"], 2)
            # A usable reader remains a PDF gap, not a second downloaded PDF.
            self.assertEqual(report["recoverable"], {
                "pmc_without_pdf": 1, "doi_without_candidate": 1,
            })
            self.assertEqual(report["unverified_legacy_pdf_records"], 0)


if __name__ == "__main__":
    unittest.main()
