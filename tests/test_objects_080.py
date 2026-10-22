import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lit_harvest import assets, content_store, reading
from lit_harvest.article_document import element_reading_text, element_locator, jats_document
from lit_harvest.fulltext import FullTextError
from lit_harvest.models import PaperRecord
from lit_harvest.object_document import parse_object
from lit_harvest.settings import DEFAULT_CONFIG


def office(parts):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return stream.getvalue()


class Objects080(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = copy.deepcopy(DEFAULT_CONFIG)

    def doc(self):
        raw = b"identity-bound synthetic article"
        source = self.root / "source.xml"
        source.write_bytes(raw)
        return {"schema_version": 1, "parser": "article-document/4", "format": "jats",
            "article": {"title": "Synthetic article"}, "source_sha256": hashlib.sha256(raw).hexdigest(),
            "source": {"artifact": str(source), "url": "https://example.org/a/", "candidate": {"oa_verified": True}},
            "coverage": {}, "gaps": [], "elements": [{"id": "e00001", "kind": "supplement", "text": "Table S1",
                "resource_references": ["supp.csv"], "section": ["Back"]}]}

    def test_word_text_table_footnote_and_no_execution(self):
        raw = office({"word/document.xml": '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Supplement methods</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Chloride</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>',
                      "word/footnotes.xml": '<footnotes><footnote><p><t>Units mmol/L</t></p></footnote></footnotes>'})
        parsed = parse_object(raw)
        self.assertEqual(parsed["format"], "docx")
        self.assertEqual([e["kind"] for e in parsed["elements"]], ["paragraph", "table", "footnote"])
        self.assertEqual(parsed["elements"][1]["rows"][0][0]["text"], "Chloride")
        self.assertFalse(parsed["content_interpreted"])

    def test_sheet_coordinates_formula_is_not_executed(self):
        raw = office({"xl/workbook.xml": '<workbook xmlns:r="urn:r"><sheets><sheet name="S1" r:id="r1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/sharedStrings.xml": '<sst><si><t>Outcome</t></si></sst>',
            "xl/worksheets/sheet1.xml": '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1"><f>1+2</f><v>3</v></c></row></sheetData><mergeCells><mergeCell ref="A2:B2"/></mergeCells></worksheet>'})
        table = parse_object(raw)["elements"][0]
        self.assertEqual(table["rows"][0][0]["text"], "Outcome")
        self.assertEqual(table["rows"][0][1]["coordinate"], "C1")
        self.assertFalse(table["rows"][0][1]["formula_executed"])
        self.assertIn("A2:B2", element_reading_text(table))

    def test_negative_shared_string_index_rejected(self):
        raw = office({"xl/workbook.xml": '<workbook xmlns:r="urn:r"><sheet name="S1" r:id="r1"/></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/sharedStrings.xml": '<sst><si><t>Wrong last string</t></si></sst>',
            "xl/worksheets/sheet1.xml": '<worksheet><row><c r="A1" t="s"><v>-1</v></c></row></worksheet>'})
        with self.assertRaises(FullTextError):
            parse_object(raw)

    def test_active_and_path_archives_rejected(self):
        for extra in ("word/vbaProject.bin", "../outside", "/absolute"):
            with self.subTest(extra=extra), self.assertRaises(FullTextError):
                parse_object(office({"word/document.xml": "<body/>", extra: "bad"}))

    def test_expanded_archive_budget(self):
        raw = office({"word/document.xml": " " * 10000})
        with self.assertRaises(FullTextError):
            parse_object(raw, max_bytes=1000)

    def test_utf16_entity_is_rejected_before_expansion(self):
        xml = '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE body [<!ENTITY x "EXPANDED">]><body>&x;</body>'
        with self.assertRaises(FullTextError):
            parse_object(office({"word/document.xml": xml.encode("utf-16")}))

    def test_unread_image_tamper_rejected_for_reading_but_resume_can_recover(self):
        doc = self.doc()
        doc["elements"][0]["resource_references"] = ["a.png"]
        raw = b"\x89PNG\r\n\x1a\nsynthetic"
        with patch.object(assets, "_fetch_bounded", return_value=(raw, "https://example.org/a.png", "")):
            assets.acquire_assets(doc, self.root, object(), self.config)
        path = content_store.write_document(self.root, doc)
        Path(doc["elements"][0]["assets"][0]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Linked object source"):
            content_store.load_document(path)
        _, newer = assets.resume_assets(path, self.root / "recovery", self.config, local_only=True)
        self.assertEqual(newer["asset_summary"]["acquired"], 0)

    def test_text_types_and_login_html_rejected(self):
        parsed = parse_object(b"chloride,outcome\n113,death\n", name="s.csv")
        self.assertEqual(len(parsed["elements"][0]["rows"]), 2)
        with self.assertRaises(FullTextError):
            parse_object(b"<html>Login</html>", name="s.csv")
        with self.assertRaises(FullTextError):
            parse_object(b"arbitrary response", name="s.exe")

    def test_webp_is_inventory_not_read_and_challenge_is_not_docx(self):
        parsed = parse_object(b"RIFF\x0c\x00\x00\x00WEBPsynthetic", name="figure.webp")
        self.assertEqual(parsed["format"], "webp")
        self.assertEqual(parsed["elements"], [])
        with self.assertRaises(FullTextError) as error:
            parse_object(b'<html><script>const POW_CHALLENGE="x";</script></html>', name="s.docx")
        self.assertEqual(error.exception.code, "anti_bot")

    def test_object_enters_reading_and_tamper_rejected(self):
        doc = self.doc()
        with patch.object(assets, "_fetch_bounded", return_value=(b"chloride,outcome\n113,death\n", "https://example.org/a/supp.csv", "")):
            assets.acquire_assets(doc, self.root, object(), self.config)
        self.assertEqual(doc["asset_summary"]["text_extracted_objects"], 1)
        self.assertEqual(len(doc["elements"]), 2)
        bound = doc["elements"][1]
        self.assertIn("113", element_reading_text(bound))
        self.assertEqual(element_locator(doc, bound)["object_source"]["sha256"], bound["object_source"]["sha256"])
        document = content_store.write_document(self.root, doc)
        _, pack = reading.prepare_reading(document, self.root / "pack", self.config, question="What does the supplementary table show?", max_chars=50000)
        self.assertIn("113", json.dumps(pack["chunks"]))
        self.assertEqual(pack["state"], "prepared_not_read")
        Path(bound["object_source"]["artifact"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "Linked object source"):
            reading.prepare_reading(document, self.root / "badpack", self.config, question="Check")

    def test_anchor_and_tdm_never_fetched(self):
        doc = self.doc()
        doc["elements"][0]["resource_references"] = ["#SM1"]
        with patch.object(assets, "_fetch_bounded") as fetch:
            assets.acquire_assets(doc, self.root, object(), self.config)
            fetch.assert_not_called()
        doc["elements"][0]["resource_references"] = ["s.csv"]
        doc["source"]["candidate"] = {"oa_route": "author_manuscript_tdm", "license": "TDM", "oa_verified": True}
        with patch.object(assets, "_fetch_bounded") as fetch:
            assets.acquire_assets(doc, self.root, object(), self.config)
            fetch.assert_not_called()

    def test_repeat_url_object_not_duplicated_and_resume_no_network(self):
        doc = self.doc()
        other = copy.deepcopy(doc["elements"][0]); other["id"] = "e00002"
        doc["elements"].append(other)
        with patch.object(assets, "_fetch_bounded", return_value=(b"a,b\n1,2", "https://example.org/a/supp.csv", "")) as fetch:
            assets.acquire_assets(doc, self.root, object(), self.config)
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(doc["asset_summary"]["derived_elements"], 1)
        document = content_store.write_document(self.root, doc)
        with patch.object(assets, "_fetch_bounded") as fetch:
            _, newer = assets.resume_assets(document, self.root / "resumed", self.config, local_only=True)
            fetch.assert_not_called()
        self.assertEqual(newer["asset_summary"]["derived_elements"], 1)
        content_store.verify_document_sources(newer)

    def test_merge_retains_relation_evidence(self):
        left = PaperRecord(title="A", doi="10.1234/a", extra={"version_relations": [{"source": "x"}]})
        right = PaperRecord(title="A", doi="10.1234/a", extra={"version_relations": [{"source": "y"}]})
        left.merge(right)
        self.assertEqual(len(left.extra["version_relations"]), 2)

    def test_same_bytes_different_declaration_kinds_are_read_once(self):
        doc = self.doc()
        first = doc["elements"][0]
        first.update(kind="figure", resource_references=["a.csv"])
        second = copy.deepcopy(first)
        second.update(id="e00002", kind="supplement", resource_references=["b.csv"])
        doc["elements"].append(second)
        with patch.object(assets, "_fetch_bounded", return_value=(b"chloride\n113", "https://example.org/data", "")):
            assets.acquire_assets(doc, self.root, object(), self.config)
        self.assertEqual(doc["asset_summary"]["acquired"], 1)
        self.assertEqual(doc["asset_summary"]["text_extracted_objects"], 1)
        derived = [e for e in doc["elements"] if e.get("object_source")]
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["object_source"]["kind"], "supplement")
        self.assertEqual(derived[0]["object_source"]["also_declared_by"][0]["element_id"], "e00002")
        self.assertEqual(len([e for e in doc["elements"] if e.get("assets")]), 2)
        content_store.verify_document_sources(doc)


if __name__ == "__main__":
    unittest.main()
