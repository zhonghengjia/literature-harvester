from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lit_harvest.models import PaperRecord  # noqa: E402
from lit_harvest.sources import (  # noqa: E402
    enrich_core,
    enrich_preprint_servers,
    enrich_unpaywall,
    search_openalex,
    search_pubmed,
)


PUBMED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <Journal>
          <JournalIssue><Volume>12</Volume><Issue>3</Issue><PubDate><Year>2024</Year></PubDate></JournalIssue>
          <Title>Critical Care Testing</Title>
        </Journal>
        <ArticleTitle>Hyperchloremia &amp; clinical outcomes</ArticleTitle>
        <Pagination><MedlinePgn>10-20</MedlinePgn></Pagination>
        <ELocationID EIdType="doi">10.1234/Test.DOI</ELocationID>
        <Abstract>
          <AbstractText Label="BACKGROUND">Background text.</AbstractText>
          <AbstractText Label="RESULTS">Results text.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><ForeName>Jane</ForeName><LastName>Doe</LastName></Author>
          <Author><CollectiveName>Study Group</CollectiveName></Author>
        </AuthorList>
        <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1234/Test.DOI</ArticleId>
        <ArticleId IdType="pmc">PMC9999999</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return self.body


class _PubMedClient:
    def __init__(self) -> None:
        self.search_params = {}
        self.fetch_url = ""

    def get_json(self, _url, params):
        self.search_params = params
        return {"esearchresult": {"idlist": ["12345678"]}}

    def request(self, url, headers=None):
        self.fetch_url = url
        return _Response(PUBMED_XML)


class _PreprintClient:
    def get_json(self, url, params=None):
        if "/medrxiv/" not in url:
            return {"collection": []}
        return {
            "collection": [
                {
                    "doi": "10.1101/2020.09.09.20191205",
                    "version": "1",
                    "license": "cc_by_nc_nd",
                    "abstract": "Old abstract",
                },
                {
                    "doi": "10.1101/2020.09.09.20191205",
                    "version": "2",
                    "license": "cc_by_nc_nd",
                    "abstract": "Latest abstract",
                },
            ]
        }


class SourceTests(unittest.TestCase):
    def test_openalex_retains_every_oa_pdf_and_landing_location(self) -> None:
        client = type(
            "Fake",
            (),
            {
                "get_json": lambda self, url, params=None: {
                    "results": [
                        {
                            "display_name": "Multiple locations",
                            "ids": {"doi": "https://doi.org/10.1234/multi"},
                            "open_access": {"is_oa": True, "oa_status": "green"},
                            "primary_location": {"source": {"display_name": "Journal"}},
                            "best_oa_location": {
                                "is_oa": True,
                                "pdf_url": "https://publisher.example/paper.pdf",
                                "landing_page_url": "https://publisher.example/article",
                                "version": "publishedVersion",
                            },
                            "locations": [
                                {
                                    "is_oa": True,
                                    "pdf_url": "https://repo.example/accepted.pdf",
                                    "version": "acceptedVersion",
                                },
                                {
                                    "is_oa": True,
                                    "landing_page_url": "https://repo.example/record/1",
                                    "version": "acceptedVersion",
                                },
                                {
                                    "is_oa": False,
                                    "pdf_url": "https://closed.example/file.pdf",
                                },
                            ],
                        }
                    ]
                },
            },
        )()
        record = search_openalex(client, "test", 5)[0]
        urls = {candidate["url"] for candidate in record.pdf_candidates}
        self.assertIn("https://publisher.example/paper.pdf", urls)
        self.assertIn("https://publisher.example/article", urls)
        self.assertIn("https://repo.example/accepted.pdf", urls)
        self.assertIn("https://repo.example/record/1", urls)
        self.assertNotIn("https://closed.example/file.pdf", urls)
        self.assertEqual(
            next(item for item in record.pdf_candidates if item["url"].endswith("/record/1"))["kind"],
            "landing",
        )

    def test_unpaywall_keeps_landing_fallback_beside_direct_pdf(self) -> None:
        client = type(
            "Fake",
            (),
            {
                "get_json": lambda self, url, params=None: {
                    "is_oa": True,
                    "oa_status": "green",
                    "oa_locations": [
                        {
                            "url_for_pdf": "https://repo.example/paper.pdf",
                            "url_for_landing_page": "https://repo.example/record/1",
                            "version": "acceptedVersion",
                        },
                        {"url": "https://other.example/record/2", "version": "submittedVersion"},
                    ],
                },
            },
        )()
        record = PaperRecord(title="Unpaywall record", doi="10.1234/unpaywall")
        enriched, attempted, errors = enrich_unpaywall(client, [record], "contact@example.org")
        self.assertEqual((enriched, attempted, errors), (1, 1, []))
        kinds = {candidate["url"]: candidate["kind"] for candidate in record.pdf_candidates}
        self.assertEqual(kinds["https://repo.example/paper.pdf"], "pdf")
        self.assertEqual(kinds["https://repo.example/record/1"], "landing")
        self.assertEqual(kinds["https://other.example/record/2"], "landing")

    @patch("lit_harvest.sources.is_public_https_url", return_value=True)
    def test_core_requires_key_and_exact_doi(self, _public) -> None:
        record = PaperRecord(title="CORE record", doi="10.1234/core")
        self.assertEqual(enrich_core(object(), [record], ""), (0, 0, []))
        client = type(
            "Fake",
            (),
            {
                "get_json": lambda self, url, params=None, headers=None: {
                    "results": [
                        {"doi": "10.1234/not-core", "downloadUrl": "https://core.example/wrong"},
                        {
                            "doi": "https://doi.org/10.1234/core",
                            "downloadUrl": "https://core.example/download/1",
                            "license": "CC BY",
                        },
                    ]
                },
            },
        )()
        enriched, attempted, errors = enrich_core(client, [record], "secret")
        self.assertEqual((enriched, attempted, errors), (1, 1, []))
        self.assertEqual(record.pdf_candidates[0]["url"], "https://core.example/download/1")
        self.assertEqual(record.pdf_candidates[0]["version"], "repositoryVersion")

    def test_pubmed_search_batches_ids_and_parses_metadata(self) -> None:
        client = _PubMedClient()
        records = search_pubmed(
            client,
            "hyperchloremia",
            10,
            contact_email="contact@example.org",
            api_key="secret-key",
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.title, "Hyperchloremia & clinical outcomes")
        self.assertEqual(record.authors, ["Jane Doe", "Study Group"])
        self.assertEqual(record.doi, "10.1234/test.doi")
        self.assertEqual(record.pmid, "12345678")
        self.assertEqual(record.pmcid, "PMC9999999")
        self.assertIn("BACKGROUND: Background text.", record.abstract)
        self.assertEqual(client.search_params["tool"], "literature_harvester")
        self.assertIn("api_key=secret-key", client.fetch_url)

    def test_preprint_enrichment_uses_latest_version(self) -> None:
        record = PaperRecord(
            title="A medRxiv study", doi="10.1101/2020.09.09.20191205", sources=["crossref"]
        )
        count, attempted, errors = enrich_preprint_servers(_PreprintClient(), [record])
        self.assertEqual((count, attempted, errors), (1, 1, []))
        candidate = next(item for item in record.pdf_candidates if item["source"] == "medrxiv")
        self.assertTrue(candidate["url"].endswith("v2.full.pdf"))
        self.assertEqual(record.item_type, "preprint")
        self.assertIn("medrxiv", record.sources)


if __name__ == "__main__":
    unittest.main()
