from contextlib import nullcontext
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

# Works next to the isolated proposal package, or when moved to canonical tests/.
here = Path(__file__).resolve().parent
sys.path.insert(0, str(here if (here / "lit_harvest").is_dir() else here.parent / "scripts"))
from lit_harvest import downloader, sources
from lit_harvest.fulltext import FullTextError, jats_to_markdown
from lit_harvest.models import PaperRecord


def atom(identifier):
    return ('<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            f'<id>http://arxiv.org/{identifier}</id><title>Study</title>'
            '</entry></feed>').encode()


def jats(body, extra=""):
    return ('<article><front><article-meta><article-id pub-id-type="pmc">123</article-id>'
            '<title-group><article-title>Study</article-title></title-group>'
            + extra + '</article-meta></front><body>' + body + '</body></article>').encode()


class SourceAuditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("lit_harvest.sources._ProviderPacer.slot", return_value=nullcontext()))

    def test_legacy_and_modern_ids_retain_complete_identity(self):
        for identifier in ("hep-ex/0307015v1", "math.GT/0309136", "math.GT/0309136v2", "2401.00001v2"):
            for query in (identifier, "arxiv:" + identifier, "https://arxiv.org/abs/" + identifier):
                with self.subTest(query=query):
                    self.assertEqual(sources.detect_identifier(query), ("arxiv", identifier))
            client = Mock()
            client.request.return_value = io.BytesIO(atom("abs/" + identifier))
            results = sources.search_arxiv(client, identifier, 1)
            self.assertEqual([r.arxiv_id for r in results], [identifier])

    def test_unrelated_text_does_not_match_identifier_suffix(self):
        self.assertEqual(sources.detect_identifier("Discuss math.GT/0309136"),
                         ("text", "Discuss math.GT/0309136"))

    def test_arxiv_fragment_error_is_error(self):
        client = Mock()
        client.request.return_value = io.BytesIO(atom("api/errors#incorrect_id_format_for_1234.12345"))
        results = sources.search_arxiv(client, "1234.12345", 1)
        self.assertEqual(results.source_status["status"], "error")
        self.assertTrue(results.source_status["errors"])

    def test_arxiv_later_error_preserves_valid_page(self):
        client = Mock()
        client.request.side_effect = [io.BytesIO(atom("abs/2401.00001v1")),
                                     io.BytesIO(atom("api/errors#bad_query"))]
        with patch.dict(sources.PAGE_SIZES, {"arxiv": 1}):
            results = sources.search_arxiv(client, "topic", 2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results.source_status["status"], "partial")
        self.assertEqual(results.source_status["continuation"], 1)

    def test_ordinary_prose_containers_are_rendered(self):
        prose = "A meaningful clinical prose sentence. " * 20
        for container in ("boxed-text", "disp-quote", "speech", "statement", "sec"):
            with self.subTest(container=container):
                output = jats_to_markdown(jats(f'<{container}><p>{prose}</p></{container}>'), "PMC123")
                self.assertIn(prose.strip(), output)

    def test_only_emitted_body_prose_qualifies(self):
        for body in ('<sec><title>' + 'heading ' * 100 + '</title></sec>',
                     '<fig><caption><p>' + 'caption ' * 100 + '</p></caption></fig>',
                     '<p>short</p>'):
            with self.subTest(body=body[:30]):
                with self.assertRaises(FullTextError) as caught:
                    jats_to_markdown(jats(body, '<abstract><p>' + 'abstract ' * 100 + '</p></abstract>'), "PMC123")
                self.assertEqual(caught.exception.code, "no_usable_body")

    def test_tables_and_formulas_are_still_excluded(self):
        prose = "A meaningful clinical prose sentence. " * 20
        output = jats_to_markdown(jats('<p>' + prose + '<inline-formula>SECRET_FORMULA</inline-formula></p>'
                                      '<table-wrap><p>SECRET_TABLE</p></table-wrap>'), "PMC123")
        self.assertIn(prose.strip(), output)
        self.assertNotIn("SECRET_FORMULA", output)
        self.assertNotIn("SECRET_TABLE", output)

    def test_jats_conflicting_candidate_never_fetches_or_saves(self):
        record = PaperRecord(title="Study", pmcid="PMC123")
        record.add_pdf_candidate("https://example.org/PMC999.xml", "pmc_oa_cloud", "CC BY",
                                 kind="jats", pmcid="PMC999", oa_route="open_access")
        with tempfile.TemporaryDirectory() as tmp, patch.object(downloader, "HttpClient"), \
                patch.object(downloader, "fetch_jats_url", return_value="# Wrong article\n" + "prose " * 100) as fetch:
            downloader.download_record(record, Path(tmp) / "pdfs")
            fetch.assert_not_called()
            self.assertNotEqual(record.download_status, "fulltext_only")
            self.assertFalse(record.local_fulltext)
            self.assertFalse((Path(tmp) / "fulltext").exists())

    def test_jats_matching_candidate_still_saves(self):
        record = PaperRecord(title="Study", pmcid="PMC123")
        record.add_pdf_candidate("https://example.org/PMC123.xml", "pmc_oa_cloud", "CC BY",
                                 kind="jats", pmcid="PMC123", oa_route="open_access")
        with tempfile.TemporaryDirectory() as tmp, patch.object(downloader, "HttpClient"), \
                patch.object(downloader, "fetch_jats_url", return_value="# Study\n" + "prose " * 100) as fetch:
            downloader.download_record(record, Path(tmp) / "pdfs")
            self.assertEqual(fetch.call_args.args[2], "PMC123")
            self.assertEqual(record.download_status, "fulltext_only")
            self.assertTrue(Path(record.local_fulltext).is_file())


if __name__ == "__main__":
    unittest.main()
