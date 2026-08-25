from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lit_harvest.models import PaperRecord  # noqa: E402
from lit_harvest.zotero import ZoteroError, ZoteroLocalClient, plan_import  # noqa: E402


class ZoteroTests(unittest.TestCase):
    def test_template_falls_back_to_local_field_schema(self) -> None:
        client = ZoteroLocalClient()
        responses = [
            ZoteroError("No endpoint found", 404),
            ([{"field": "title"}, {"field": "DOI"}], {}, 200),
            ([{"creatorType": "author"}], {}, 200),
        ]
        with patch.object(client, "_json_request", side_effect=responses):
            template = client.get_template("journalArticle")
        self.assertEqual(template["itemType"], "journalArticle")
        self.assertIn("title", template)
        self.assertIn("DOI", template)
        self.assertEqual(template["creators"], [])

    def test_attachment_template_has_official_upload_fields(self) -> None:
        client = ZoteroLocalClient()
        with patch.object(
            client,
            "_json_request",
            side_effect=ZoteroError("No endpoint found", 404),
        ):
            template = client.get_template("attachment", "imported_file")
        self.assertEqual(template["linkMode"], "imported_file")
        self.assertIn("filename", template)
        self.assertIn("contentType", template)

    def test_plan_recovers_exact_parent_and_pdf_attachment_keys(self) -> None:
        record = PaperRecord(title="Example", doi="10.1000/example", local_pdf="paper.pdf")
        items = [
            {
                "key": "PARENT01",
                "data": {"itemType": "journalArticle", "title": "Example", "DOI": "10.1000/example"},
            },
            {
                "key": "ATTACH01",
                "data": {
                    "itemType": "attachment",
                    "parentItem": "PARENT01",
                    "contentType": "application/pdf",
                    "filename": "paper.pdf",
                },
            },
        ]
        counts = plan_import([record], items)
        self.assertEqual(counts, {"skip_exact": 1})
        self.assertEqual(record.zotero["item_key"], "PARENT01")
        self.assertEqual(record.zotero["attachment_key"], "ATTACH01")
        self.assertEqual(record.zotero["status"], "verified_existing_with_pdf")


if __name__ == "__main__":
    unittest.main()
