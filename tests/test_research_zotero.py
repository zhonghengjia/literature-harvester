"""Research-selected local upload/readback protocol; no real library writes."""
from argparse import Namespace
import copy
import hashlib
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import literature_harvester as cli
from lit_harvest import pipeline, zotero
from test_identity_downloader import good_pdf, paper


def attachment(md5=None, parent="PARENT", key="CHILD", **changes):
    data = {"itemType": "attachment", "parentItem": parent,
            "linkMode": "imported_file", "md5": md5, **changes}
    return {"key": key, "data": data}


class UploadReadbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.pdf = Path(temp.name) / "synthetic.pdf"
        self.pdf.write_bytes(good_pdf())
        self.md5 = hashlib.md5(self.pdf.read_bytes(), usedforsecurity=False).hexdigest()
        self.client = zotero.ZoteroLocalClient(auth_cache=Path(temp.name) / "auth.json")
        self.client.get_template = Mock(return_value={"itemType": "attachment", "filename": ""})
        self.client.create_item = Mock(return_value="CHILD")
        self.client.get_item = Mock(side_effect=[attachment(), attachment(self.md5)])
        self.client._json_request = Mock(side_effect=[(
            {"uploadKey": "synthetic", "url": "http://127.0.0.1:23119/api/upload"}, {}, 200),
            (None, {}, 204)])
        self.client._raw_request = Mock(return_value=(b"", {}, 204))

    def test_success_checks_relationship_and_actual_file_after_registration(self):
        self.assertEqual(self.client.upload_attachment("PARENT", self.pdf), "CHILD")
        self.assertEqual(self.client.get_item.call_count, 2)
        self.assertEqual(self.client._json_request.call_count, 2)

    def test_exists_is_not_success_without_exact_md5(self):
        for digest in [None, "0" * 32]:
            with self.subTest(digest=digest):
                self.client.get_item.side_effect = [attachment(), attachment(digest)]
                self.client._json_request.side_effect = [({"exists": 1}, {}, 200)]
                with self.assertRaises(zotero.ZoteroAttachmentError) as caught:
                    self.client.upload_attachment("PARENT", self.pdf, "CHILD")
                self.assertEqual(caught.exception.attachment_key, "CHILD")
                self.client._raw_request.assert_not_called()

    def test_exists_with_exact_readback_is_success(self):
        self.client._json_request.side_effect = [({"exists": 1}, {}, 200)]
        self.assertEqual(self.client.upload_attachment("PARENT", self.pdf, "CHILD"), "CHILD")
        self.assertEqual(self.client.get_item.call_count, 2)
        self.client.create_item.assert_not_called()
        self.client._raw_request.assert_not_called()

    def test_wrong_child_relationship_or_conflicting_stored_file_is_not_written(self):
        for item in [attachment(parent="OTHER"), attachment(key="OTHER"),
                     attachment(linkMode="linked_file"), attachment(itemType="note"),
                     attachment("0" * 32)]:
            with self.subTest(item=item):
                self.client.get_item.side_effect = [item]
                with self.assertRaises(zotero.ZoteroAttachmentError):
                    self.client.upload_attachment("PARENT", self.pdf, "CHILD")
                self.client._json_request.assert_not_called()
                self.client.create_item.assert_not_called()

    def test_missing_or_wrong_final_md5_never_counts_as_success(self):
        for digest in [None, "0" * 32]:
            with self.subTest(digest=digest):
                self.client.get_item.side_effect = [attachment(), attachment(digest)]
                self.client._json_request.side_effect = [(
                    {"uploadKey": "x", "url": "http://localhost:23119/api/upload"}, {}, 200),
                    (None, {}, 204)]
                with self.assertRaises(zotero.ZoteroAttachmentError):
                    self.client.upload_attachment("PARENT", self.pdf, "CHILD")

    def test_lost_registration_response_recovers_by_reading_original_key(self):
        self.client.get_item.side_effect = [attachment()]
        self.client._json_request.side_effect = [(
            {"uploadKey": "x", "url": "http://localhost:23119/api/upload"}, {}, 200),
            zotero.ZoteroError("reply lost after storage registration")]
        with self.assertRaises(zotero.ZoteroAttachmentError) as caught:
            self.client.upload_attachment("PARENT", self.pdf, "CHILD")
        self.assertEqual(caught.exception.attachment_key, "CHILD")
        self.client.get_item.side_effect = [attachment(self.md5)]
        self.client._json_request.reset_mock()
        self.client._raw_request.reset_mock()
        self.assertEqual(self.client.upload_attachment("PARENT", self.pdf, "CHILD"), "CHILD")
        self.client.create_item.assert_not_called()
        self.client._json_request.assert_not_called()
        self.client._raw_request.assert_not_called()

    def test_child_key_checkpoint_precedes_upload_and_readback_failure(self):
        checkpoint = Mock()
        self.client.get_item.side_effect = zotero.ZoteroError("readback unavailable")
        with self.assertRaises(zotero.ZoteroAttachmentError) as caught:
            self.client.upload_attachment("PARENT", self.pdf, checkpoint=checkpoint)
        checkpoint.assert_called_once_with("CHILD")
        self.assertEqual(caught.exception.attachment_key, "CHILD")
        self.client._json_request.assert_not_called()

    def test_changed_server_reply_is_rejected(self):
        response = BytesIO(b"{}")
        response.status = 200
        response.headers = {"Zotero-Server-ID": "different-instance"}
        client = zotero.ZoteroLocalClient(opener=Mock())
        client.server_id = "bound-instance"
        client.opener.open.return_value = response
        with self.assertRaises(zotero.ZoteroError):
            client.get_item("PARENT")
        self.assertEqual(client.server_id, "bound-instance")


class ImportReadbackTests(unittest.TestCase):
    def setUp(self):
        self.record = paper()
        self.client = Mock()
        self.client.server_id = "instance"
        self.client.get_items.return_value = []
        self.client.get_collections.return_value = []
        self.client.get_template.return_value = {"itemType": "journalArticle", "title": "", "DOI": ""}
        self.client.create_item.return_value = "PARENT"
        self.parent = {"key": "PARENT", "data": {"itemType": "journalArticle",
                       "title": self.record.title, "DOI": self.record.doi}}
        self.client.get_item.return_value = self.parent

    def test_parent_readback_failure_preserves_key_and_resumes_without_create(self):
        self.client.get_item.side_effect = zotero.ZoteroError("read unavailable")
        result = zotero.import_records(self.client, [self.record], query="synthetic")
        self.assertEqual(result, {"parent_readback_failed": 1})
        self.assertEqual(self.record.zotero["item_key"], "PARENT")
        self.client.get_item.side_effect = None
        result = zotero.import_records(self.client, [self.record], query="synthetic")
        self.assertEqual(result, {"imported_without_pdf": 1})
        self.client.create_item.assert_called_once()
        self.assertEqual(self.record.zotero["server_id"], "instance")

    def test_parent_key_type_doi_and_title_mismatches_hold_import(self):
        for change in [{"key": "OTHER"}, {"itemType": "attachment"},
                       {"DOI": "10.1234/conflict"}, {"DOI": "", "title": "Unrelated study"}]:
            with self.subTest(change=change):
                item = copy.deepcopy(self.parent)
                if "key" in change:
                    item["key"] = change["key"]
                else:
                    item["data"].update(change)
                self.client.get_item.return_value = item
                result = zotero.import_records(self.client, [paper()], query="synthetic")
                self.assertEqual(result, {"parent_readback_failed": 1})
                self.client.upload_attachment.assert_not_called()

    def test_resume_does_not_write_to_another_server(self):
        self.record.zotero = {"status": "parent_created_attachment_failed", "item_key": "PARENT",
                              "attachment_key": "CHILD", "server_id": "previous-instance"}
        with self.assertRaises(zotero.ZoteroError):
            zotero.import_records(self.client, [self.record], query="synthetic",
                                  collection_name="New", create_collection=True)
        self.client.create_item.assert_not_called()
        self.client.create_collection.assert_not_called()
        self.client.upload_attachment.assert_not_called()

    def test_partial_parent_and_attachment_states_keep_keys(self):
        for status in ["parent_created", "parent_readback_failed", "parent_created_attachment_failed",
                       "parent_created_attachment_unverified"]:
            for pdf in ["", "synthetic.pdf"]:
                with self.subTest(status=status, pdf=pdf):
                    row = paper()
                    row.local_pdf = pdf
                    row.zotero = {"status": status, "item_key": "PARENT", "attachment_key": "CHILD"}
                    result = zotero.plan_import([row], [])
                    self.assertEqual(result, {"resume_attachment" if pdf else "resume_parent": 1})
                    self.assertEqual(row.zotero["attachment_key"], "CHILD")


class CollectionAuthorizationTests(unittest.TestCase):
    def test_only_task_flag_authorizes_collection_not_old_global_config(self):
        for flag in [False, True]:
            with self.subTest(flag=flag):
                args = Namespace(confirm_write=True, config=None, manifest="synthetic.json",
                                 pdf_only=False, collection="New", create_collection=flag)
                config = copy.deepcopy(pipeline.DEFAULT_CONFIG)
                config["zotero"]["create_collection"] = True
                with patch.object(cli, "load_config", return_value=config), \
                     patch.object(cli, "_client"), \
                     patch.object(cli, "load_manifest", return_value=({"query": "synthetic"}, [paper()])), \
                     patch.object(cli, "save_manifest"), patch.object(cli, "write_reports"), \
                     patch.object(cli, "import_records", return_value={}) as importer, \
                     patch("builtins.print"):
                    cli.command_zotero_import(args)
                self.assertEqual(importer.call_args.kwargs["create_collection"], flag)


if __name__ == "__main__":
    unittest.main(verbosity=1)
