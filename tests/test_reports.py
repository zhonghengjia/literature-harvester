from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lit_harvest.models import PaperRecord  # noqa: E402
from lit_harvest.reports import write_failure_report  # noqa: E402


class ReportTests(unittest.TestCase):
    def test_failure_report_contains_actionable_legal_channels(self) -> None:
        record = PaperRecord(
            title="Paywalled clinical paper",
            doi="10.1234/example",
            pmid="12345678",
            download_status="no_oa_version",
            failure_reason="No verified OA location",
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_failure_report(run_dir, [record])
            text = (run_dir / "failed_downloads.md").read_text(encoding="utf-8")
        self.assertIn("pubscholar.cn", text)
        self.assertIn("opensign.lib.tsinghua.edu.cn", text)
        self.assertIn("interlibrary-loan", text)
        self.assertIn("Author-request template", text)


if __name__ == "__main__":
    unittest.main()
