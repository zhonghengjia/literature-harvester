"""Offline Zotero matching, attachment-state and preprint-type regressions."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import ANY, Mock
from reportlab.pdfgen import canvas

CANONICAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANONICAL / "scripts"))
from lit_harvest import zotero
from lit_harvest.models import PaperRecord

TITLE = "Synthetic clinical outcomes after a randomized intervention"
DOI = "10.1234/synthetic"

def item(key="PARENT01", doi=DOI):
    return {"key": key, "data": {"itemType": "journalArticle", "title": TITLE, "DOI": doi}}

def client(items=()):
    fake = Mock()
    fake.get_items.return_value = list(items)
    fake.get_collections.return_value = []
    fake.create_item.return_value = "NEWPARENT"
    fake.get_item.side_effect = lambda key: item(key)
    fake.upload_attachment.return_value = "NEWATTACH"
    return fake

class ZoteroAuditRegression(unittest.TestCase):
    def test_replanned_parent_does_not_receive_stale_attachment_key(self):
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "synthetic.pdf"
            writer = canvas.Canvas(str(pdf))
            for n, text in enumerate([TITLE, "Jane Doe", "DOI: " + DOI]):
                writer.drawString(35, 790 - n * 20, text)
            writer.showPage()
            writer.save()
            row = PaperRecord(TITLE, doi=DOI, authors=["Jane Doe"], local_pdf=str(pdf),
                zotero={"item_key": "OLDPARENT", "attachment_key": "OLDATTACH", "status": "imported_with_pdf"})
            fake = client([item("NEWPARENT")])
            result = zotero.import_records(fake, [row], query="synthetic")
            fake.upload_attachment.assert_called_once_with("NEWPARENT", pdf, "", checkpoint=ANY)
            self.assertEqual(result, {"attached_pdf_to_existing": 1})

    def test_title_match_with_conflicting_doi_is_held(self):
        row = PaperRecord(TITLE, doi=DOI, local_pdf="unused.pdf")
        fake = client([item(doi="10.1234/different")])
        result = zotero.import_records(fake, [row], query="synthetic")
        self.assertEqual(result, {"manual_review": 1})
        self.assertEqual(row.zotero["match_reason"], "doi_conflict")
        fake.create_item.assert_not_called()
        fake.upload_attachment.assert_not_called()

    def test_preprint_template_failure_does_not_create_journal_article(self):
        row = PaperRecord(TITLE, doi=DOI, item_type="preprint")
        fake = client()
        fake.get_template.side_effect = [zotero.ZoteroError("temporary failure", 503),
            {"itemType": "journalArticle", "title": ""}]
        result = zotero.import_records(fake, [row], query="synthetic")
        self.assertEqual(result, {"item_create_failed": 1})
        fake.create_item.assert_not_called()
        fake.get_template.assert_called_once_with("preprint")

    def test_nonconflicting_match_and_explicit_resume_are_preserved(self):
        for doi in [DOI, ""]:
            with self.subTest(doi=doi):
                row = PaperRecord(TITLE, doi=DOI, local_pdf="unused.pdf")
                self.assertEqual(zotero.plan_import([row], [item(doi=doi)]), {"attach_existing": 1})
        row = PaperRecord(TITLE, doi=DOI, local_pdf="unused.pdf", zotero={
            "item_key": "PARENT01", "attachment_key": "ATTACH01", "status": "parent_created_attachment_failed"})
        self.assertEqual(zotero.plan_import([row], [item()]), {"resume_attachment": 1})
        self.assertEqual(row.zotero["attachment_key"], "ATTACH01")
        fake = client()
        fake.get_template.return_value = {"itemType": "preprint", "title": ""}
        row.item_type = "preprint"
        self.assertEqual(zotero._record_to_item(fake, row, "", "")["itemType"], "preprint")

if __name__ == "__main__":
    unittest.main(verbosity=2)
