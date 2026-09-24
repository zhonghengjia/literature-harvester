from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lit_harvest.http import HttpError
from lit_harvest.models import PaperRecord
from lit_harvest.sources import (
    PAGE_SIZES, SearchResults, enrich_core, enrich_openalex_content,
    enrich_preprints_by_title, openalex_content_policy,
    search_arxiv, search_crossref, search_europe_pmc, search_openalex,
    search_pubmed, search_semantic_scholar,
)


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


class JSONClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        response = next(self.pages)
        if isinstance(response, Exception):
            raise response
        return response


def work(number, **overrides):
    return {
        "id": f"https://openalex.org/W{number}", "title": f"Study {number}",
        "ids": {"pmid": None, "pmcid": None}, **overrides,
    }


def atom(entries):
    values = "".join(
        f'<entry><id>http://arxiv.org/abs/{identifier}</id><title>{title}</title>'
        f'<link type="application/pdf" href="https://arxiv.org/pdf/{identifier}" />'
        '<published>2024-01-01T00:00:00Z</published></entry>'
        for identifier, title in entries
    )
    return ('<feed xmlns="http://www.w3.org/2005/Atom">' + values + '</feed>').encode()


class SourceUpgradeTests(unittest.TestCase):
    def setUp(self):
        # Pagination fixtures test request order without real time delays.
        self.enterContext(patch("lit_harvest.sources._ProviderPacer.slot", return_value=nullcontext()))

    def test_openalex_cursor_respects_supported_page_cap_and_null_ids(self):
        client = JSONClient([
            {"results": [work(n) for n in range(100)], "meta": {"next_cursor": "page-two"}},
            {"results": [work(100)], "meta": {"next_cursor": None}},
        ])
        results = search_openalex(client, "clinical trial", 101)
        self.assertIsInstance(results, SearchResults)
        self.assertEqual(len(results), 101)
        self.assertEqual([call[1]["per_page"] for call in client.calls], [100, 1])
        self.assertEqual([call[1]["cursor"] for call in client.calls], ["*", "page-two"])
        self.assertTrue(all(record.pmid == record.pmcid == "" for record in results))
        self.assertFalse(any("None" in key for record in results for key in record.identity_keys))

    def test_openalex_keeps_primary_location_and_explicit_content_separately(self):
        client = JSONClient([{"results": [work(
            123, ids={"pmid": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                      "pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123/"},
            primary_location={"is_oa": True, "pdf_url": "https://repo.example/a.pdf",
                              "version": "acceptedVersion"},
            best_oa_location={"is_oa": True, "license": "cc-by"},
            open_access={"is_oa": True},
            type="review",
            has_content={"pdf": True},
            content_urls={"pdf": "https://content.openalex.org/works/W123.pdf",
                          "grobid_xml": "https://evil.example/works/W123.grobid-xml"},
        )]}])
        record = search_openalex(client, "x", 1)[0]
        self.assertEqual(record.pmid, "12345678")
        self.assertEqual(record.pmcid, "PMC123")
        self.assertIn("review", record.extra["publication_types"])
        self.assertIn("https://repo.example/a.pdf", [c["url"] for c in record.pdf_candidates])
        self.assertEqual(record.extra["openalex_content_urls"],
                         {"pdf": "https://content.openalex.org/works/W123.pdf"})
        self.assertNotIn("https://content.openalex.org/works/W123.pdf",
                         [c["url"] for c in record.pdf_candidates])

    def test_pagination_failure_preserves_previous_records_and_retry_metadata(self):
        error = HttpError("rate limited", 429)
        error.retry_after = 75
        error.retry_at = "2026-09-09T01:00:00Z"
        client = JSONClient([
            {"results": [work(1)], "meta": {"next_cursor": "resume-token"}},
            error,
        ])
        results = search_openalex(client, "x", 3)
        self.assertEqual(len(results), 1)
        self.assertEqual(results.source_status["status"], "partial")
        self.assertEqual(results.source_status["retry_after"], 75)
        self.assertEqual(results.source_status["continuation"], "resume-token")
        self.assertEqual(results.source_status["http_status"], 429)

    def test_repeated_cursor_stops_with_partial_status(self):
        client = JSONClient([
            {"results": [work(1)], "meta": {"next_cursor": "*"}},
        ])
        results = search_openalex(client, "x", 10)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(results.source_status["status"], "partial")

    def test_invalid_and_excessive_total_limits_never_request(self):
        for limit in (0, -1, True, 1.5, 10001):
            client = JSONClient([])
            with self.assertRaises(ValueError):
                search_openalex(client, "x", limit)
            self.assertEqual(client.calls, [])

    def test_europe_pmc_cursor_and_publication_types(self):
        client = JSONClient([
            {"resultList": {"result": [{"id": "12", "source": "MED", "title": "Review",
                                       "pubTypeList": {"pubType": ["Review"]},
                                       "pmcid": "PMC12", "isOpenAccess": "N"}]},
             "nextCursorMark": "epmc-2"},
            {"resultList": {"result": [{"id": "PPR2", "source": "PPR", "title": "Preprint",
                                       "pubTypeList": {"pubType": ["Preprint"]}}]}},
        ])
        results = search_europe_pmc(client, "x", 1001)
        self.assertEqual(client.calls[0][1]["pageSize"], 1000)
        self.assertEqual(client.calls[1][1]["cursorMark"], "epmc-2")
        self.assertEqual(results[0].extra["publication_types"], ["review"])
        self.assertEqual(results[1].item_type, "preprint")
        self.assertEqual(results[0].pmid, "12")
        self.assertEqual(results[0].pdf_candidates[0]["source"], "pmc_oa_cloud")
        self.assertIsNot(results[0].is_oa, True)

    def test_europe_pmc_does_not_promote_restricted_fulltext_links(self):
        client = JSONClient([{"resultList": {"result": [{
            "id": "1", "title": "Restricted", "isOpenAccess": "N",
            "fullTextUrlList": {"fullTextUrl": [
                {"url": "https://publisher.example/private.pdf", "documentStyle": "pdf",
                 "availability": "Subscription required"},
            ]},
        }]}}])
        self.assertEqual(search_europe_pmc(client, "x", 1)[0].pdf_candidates, [])

    def test_crossref_cursor_and_raw_publication_type(self):
        client = JSONClient([
            {"message": {"items": [{"title": ["A"], "type": "journal-article"},
                                   {"title": ["B"], "type": "posted-content"}],
                         "next-cursor": "crossref-2"}},
            {"message": {"items": [{"title": ["C"]}], "next-cursor": "crossref-last"}},
        ])
        with patch.dict(PAGE_SIZES, {"crossref": 2}):
            results = search_crossref(client, "x", 3)
        self.assertEqual(len(results), 3)
        self.assertEqual(client.calls[1][1]["cursor"], "crossref-2")
        self.assertEqual(results[0].extra["publication_types"], ["journal-article"])

    def test_s2_paginated_relevance_cap_is_explicit(self):
        pages = []
        for offset in range(0, 1000, 100):
            payload = {"data": [{"paperId": str(n), "title": f"S2 {n}",
                                "publicationTypes": ["Review"]}
                               for n in range(offset, offset + 100)]}
            payload["next"] = offset + 100
            pages.append(payload)
        client = JSONClient(pages)
        results = search_semantic_scholar(client, "x", 1001)
        self.assertEqual(len(results), 1000)
        self.assertEqual(len(client.calls), 10)
        self.assertTrue(all(c[1]["limit"] <= 100 for c in client.calls))
        self.assertEqual(results.source_status["effective_limit"], 1000)
        self.assertIn("1000", results.source_status["limit_reason"])
        self.assertEqual(results[0].extra["publication_types"], ["review"])

    def test_pubmed_pages_then_bounded_fetch_and_registers_pmc_resolver(self):
        class PubMedClient:
            def __init__(self):
                self.params = []
                self.fetch_sizes = []

            def get_json(self, url, params=None):
                self.params.append(params)
                start = params["retstart"]
                return {"esearchresult": {"count": "501",
                        "idlist": [str(n) for n in range(10000000 + start,
                                   10000000 + min(start + params["retmax"], 501))]}}

            def request(self, url, headers=None):
                ids = parse_qs(urlparse(url).query)["id"][0].split(",")
                self.fetch_sizes.append(len(ids))
                records = "".join(
                    '<PubmedArticle><MedlineCitation><PMID>' + pmid + '</PMID>'
                    '<Article><ArticleTitle>Trial ' + pmid + '</ArticleTitle>'
                    '<PublicationTypeList><PublicationType>Randomized Controlled Trial</PublicationType>'
                    '</PublicationTypeList></Article></MedlineCitation><PubmedData><ArticleIdList>'
                    '<ArticleId IdType="pmc">PMC' + pmid + '</ArticleId>'
                    '</ArticleIdList></PubmedData></PubmedArticle>' for pmid in ids
                )
                return Response(("<PubmedArticleSet>" + records + "</PubmedArticleSet>").encode())

        client = PubMedClient()
        results = search_pubmed(client, "x", 501)
        self.assertEqual(len(results), 501)
        self.assertEqual([p["retstart"] for p in client.params], [0, 500])
        self.assertLessEqual(max(client.fetch_sizes), 100)
        self.assertEqual(results[0].extra["publication_types"], ["randomized controlled trial"])
        self.assertEqual(results[0].pdf_candidates[0]["source"], "pmc_oa_cloud")
        self.assertIsNone(results[0].is_oa)

    def test_arxiv_offset_and_legacy_identifier_are_preserved(self):
        class ArxivClient:
            def __init__(self):
                self.calls = []
                self.responses = iter([atom([("hep-th/9901001", "Old paper"),
                                             ("2401.00001", "First")]),
                                       atom([("2401.00002v2", "Second")])])

            def request(self, url, headers=None):
                self.calls.append(parse_qs(urlparse(url).query))
                return Response(next(self.responses))
        client = ArxivClient()
        with patch.dict(PAGE_SIZES, {"arxiv": 2}):
            records = search_arxiv(client, "quantum", 3)
        self.assertEqual([r.arxiv_id for r in records],
                         ["hep-th/9901001", "2401.00001", "2401.00002v2"])
        self.assertEqual([c["start"] for c in client.calls], [["0"], ["2"]])

    @patch("lit_harvest.sources.is_public_https_url", side_effect=lambda u: u.startswith("https://"))
    def test_core_force_examines_all_exact_works_and_all_locations(self, _public):
        record = PaperRecord(title="Original", doi="10.1234/original")
        record.add_pdf_candidate("https://publisher.example/broken.pdf", "openalex")
        data = {"results": [
            {"doi": "10.1234/wrong", "downloadUrl": "https://repo.example/wrong.pdf"},
            {"doi": "10.1234/original", "downloadUrl": "https://repo.example/a.pdf",
             "sourceFulltextUrls": ["https://repo.example/record/a"]},
            {"doi": "https://doi.org/10.1234/original", "downloadUrl": "https://repo.example/b.pdf",
             "sourceFulltextUrls": ["https://repo.example/record/b"]},
        ]}
        unused = JSONClient([])
        self.assertEqual(enrich_core(unused, [record], "secret"), (0, 0, []))
        self.assertEqual(unused.calls, [])
        result = enrich_core(JSONClient([data]), [record], "secret", force=True)
        self.assertEqual(result, (1, 1, []))
        urls = {c["url"] for c in record.pdf_candidates}
        self.assertEqual(len(urls), 5)
        self.assertNotIn("https://repo.example/wrong.pdf", urls)
        self.assertIn("https://repo.example/b.pdf", urls)
        self.assertIn("https://repo.example/record/a", urls)

    def test_force_preprint_title_retry_retains_distinct_doi_relation(self):
        record = PaperRecord(title="Exact title for an independently deposited manuscript",
                             doi="10.1234/published", year=2024, authors=["Jane Smith"])
        record.add_pdf_candidate("https://publisher.example/broken.pdf", "openalex")
        record.download_status = "failed"
        client = JSONClient([{"resultList": {"result": [{
            "title": record.title, "pubYear": "2023", "doi": "10.21203/rs.test",
            "authorList": {"author": [{"fullName": "Jane Smith"}]},
            "fullTextUrlList": {"fullTextUrl": [{"documentStyle": "pdf",
                                                "url": "https://repo.example/preprint.pdf"}]},
        }]}}])
        self.assertEqual(enrich_preprints_by_title(client, [record], force=True), (1, 1, []))
        self.assertEqual(record.doi, "10.1234/published")
        self.assertEqual(record.extra["preprint_doi"], "10.21203/rs.test")
        self.assertEqual(record.pdf_candidates[-1]["version"], "submittedVersion")

    def test_content_never_spends_or_registers_metered_candidates(self):
        class NoNetwork:
            def get_json(self, *_a, **_k):
                raise AssertionError("Content policy must not contact a metered endpoint")
        record = PaperRecord(title="OA", openalex_id="W123", is_oa=True, extra={
            "openalex_has_content": {"pdf": True},
            "openalex_content_urls": {"pdf": "https://content.openalex.org/works/W123.pdf"},
            "openalex_content_license": "cc-by",
        })
        self.assertEqual(openalex_content_policy()["status"], "disabled")
        count, attempted, errors = enrich_openalex_content(
            NoNetwork(), [record], "secret", enabled=True, free_only=True, max_files=5)
        self.assertEqual((count, attempted), (0, 0))
        self.assertTrue(errors)
        self.assertEqual(record.extra["openalex_content"]["status"], "blocked")
        self.assertTrue(record.extra["openalex_content"]["declared_pdf"])
        self.assertTrue(record.extra["openalex_content"]["license_verified"])
        self.assertEqual(record.pdf_candidates, [])
        self.assertEqual(openalex_content_policy(enabled=True, free_only=False)["status"], "blocked")

    def test_content_metadata_does_not_imply_article_license(self):
        record = PaperRecord(title="No license", openalex_id="W123", is_oa=True, extra={
            "openalex_has_content": {"pdf": True},
            "openalex_content_urls": {"pdf": "https://content.openalex.org/works/W123.pdf"},
        })
        enrich_openalex_content(object(), [record])
        self.assertFalse(record.extra["openalex_content"]["license_verified"])


class ProviderPacingTests(unittest.TestCase):
    def test_waits_between_completed_requests_and_not_before_first(self):
        from lit_harvest.sources import _ProviderPacer
        pacer = _ProviderPacer()
        with patch("lit_harvest.sources.time.monotonic", side_effect=[10.0, 11.0, 13.0]), \
                patch("lit_harvest.sources.time.sleep") as sleep:
            with pacer.slot(3.0):
                sleep.assert_not_called()
            with pacer.slot(3.0):
                sleep.assert_called_once_with(2.0)

    def test_failed_request_still_sets_next_request_interval(self):
        from lit_harvest.sources import _ProviderPacer
        pacer = _ProviderPacer()
        with patch("lit_harvest.sources.time.monotonic", side_effect=[10.0, 10.1, 10.34]), \
                patch("lit_harvest.sources.time.sleep") as sleep:
            with self.assertRaises(RuntimeError), pacer.slot(0.34):
                raise RuntimeError("synthetic failure")
            with pacer.slot(0.34):
                self.assertAlmostEqual(sleep.call_args.args[0], 0.24)


if __name__ == "__main__":
    unittest.main()
