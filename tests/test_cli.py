from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("literature_harvester_cli", SCRIPTS / "literature_harvester.py")
assert SPEC and SPEC.loader
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)

from lit_harvest.models import PaperRecord  # noqa: E402


class CliTests(unittest.TestCase):
    def test_pdf_only_selects_existing_successful_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf = Path(temp_dir) / "paper.pdf"
            pdf.touch()
            selected = PaperRecord(
                title="Selected",
                download_status="downloaded",
                local_pdf=str(pdf),
            )
            missing = PaperRecord(
                title="Missing",
                download_status="downloaded",
                local_pdf=str(Path(temp_dir) / "missing.pdf"),
            )
            failed = PaperRecord(
                title="Failed",
                download_status="no_oa_version",
                local_pdf=str(pdf),
            )
            with patch.object(CLI, "validate_pdf", return_value=None):
                result = CLI._select_zotero_records([selected, missing, failed], True)
        self.assertEqual(result, [selected])

    def test_zotero_commands_accept_pdf_only(self) -> None:
        parser = CLI.build_parser()
        planned = parser.parse_args(["zotero-plan", "--manifest", "run.json", "--pdf-only"])
        imported = parser.parse_args(
            ["zotero-import", "--manifest", "run.json", "--pdf-only", "--confirm-write"]
        )
        self.assertTrue(planned.pdf_only)
        self.assertTrue(imported.pdf_only)


if __name__ == "__main__":
    unittest.main()
