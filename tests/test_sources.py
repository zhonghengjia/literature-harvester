from __future__ import annotations

from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lit_harvest.models import PaperRecord  # noqa: E402
from lit_harvest.sources import enrich_preprint_servers, search_pubmed  # noqa: E402


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
