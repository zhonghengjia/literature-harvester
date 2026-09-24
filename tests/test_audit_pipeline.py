"""Offline regressions for frozen-run provenance and truthful diagnostics."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from lit_harvest import pipeline
from lit_harvest.coverage import build_coverage
from lit_harvest.models import PaperRecord

spec = importlib.util.spec_from_file_location("audit_pipeline_cli", ROOT / "scripts/literature_harvester.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class PipelineAuditChecks(unittest.TestCase):
    def test_resume_records_effective_sources_without_changing_frozen_population(self):
        config = copy.deepcopy(pipeline.DEFAULT_CONFIG)
        config["sources"] = {"pubmed": False, "unpaywall": True}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "original.json"
            record = PaperRecord("Frozen synthetic study", doi="10.9999/frozen")
            original = pipeline.new_manifest("frozen", [record], {"pubmed": {"status": "ok"}},
                {"enabled_sources": ["pubmed"], "target_pdfs": 1, "required_terms": ["frozen"]})
            source.write_text(json.dumps(original), encoding="utf-8")
            before = source.read_bytes()
            with patch.object(pipeline, "retrieve_population") as retrieve, patch.object(
                    pipeline, "search_all", side_effect=AssertionError("Frozen resume cannot search")):
                _, manifest, records = pipeline.resume_pipeline(source, config, str(root / "resumed"), env={})
            self.assertEqual(manifest["settings"]["enabled_sources"], ["unpaywall"])
            self.assertEqual(manifest["settings"]["required_terms"], ["frozen"])
            self.assertEqual(manifest["parent_run_id"], original["run_id"])
            self.assertEqual([r.doi for r in records], [record.doi])
            self.assertEqual(source.read_bytes(), before)
            self.assertIs(retrieve.call_args.args[3], config)

    def test_claimed_but_unverified_pdf_remains_in_recovery_diagnostics(self):
        unverified = PaperRecord("Legacy unverified synthetic article", doi="10.9999/legacy")
        unverified.download_status = "downloaded"
        pending = PaperRecord("Not attempted synthetic article", doi="10.9999/pending")
        report = build_coverage({}, [unverified, pending])
        self.assertEqual(report["pdf_count"], 0)
        self.assertEqual(report["unverified_legacy_pdf_records"], 1)
        self.assertEqual(sum(report["recoverable"].values()), 1)
        self.assertEqual(report["pending_records"], 1)

    def test_zero_result_cap_is_rejected_before_search(self):
        arguments = cli.build_parser().parse_args(["run", "--query", "synthetic", "--max-results", "0"])
        with patch.object(cli, "load_config", return_value=copy.deepcopy(pipeline.DEFAULT_CONFIG)), patch.object(
                cli, "run_pipeline", side_effect=AssertionError("Invalid cap must not search")):
            with self.assertRaisesRegex(ValueError, "max-results"):
                cli.command_run(arguments)


if __name__ == "__main__":
    unittest.main()
