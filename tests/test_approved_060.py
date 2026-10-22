"""Approved scope regression evidence; synthetic fixtures, no live requests."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lit_harvest import pipeline, relations, content_store, assets
from lit_harvest.article_document import pdf_document
from lit_harvest.downloader import sha256_file
from lit_harvest.evaluation import evaluate
from lit_harvest.models import PaperRecord
from lit_harvest.query_lint import inspect_query
from lit_harvest.query_plan import validate_plan, candidate_id
from test_reading_upgrade import make_pdf, record, plan
from literature_harvester import main


class FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []
    def get_json(self, url, params):
        self.calls.append((url, params.copy()))
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def page(number=2, cursor=None):
    return {"results": [{"id": f"https://openalex.org/W{number}", "title": f"Synthetic study {number}",
                         "doi": f"https://doi.org/10.1234/r{number}"}], "meta": {"next_cursor": cursor}}


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = copy.deepcopy(pipeline.DEFAULT_CONFIG)
        self.config["content"]["ocr"] = "off"
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("Offline test")))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("Offline test")))
        self.seed = record(openalex_id="W1")
        self.parent = self.root / "parent.json"
        pipeline.save_manifest(self.parent, pipeline.new_manifest("synthetic", [self.seed], {}, {}))
        self.parent_hash = sha256_file(self.parent)

    def expand(self, pages, folder="child", **kwargs):
        client = FakeClient(pages)
        directory, manifest = relations.expand(self.parent, self.root / folder, self.config,
            seeds=[candidate_id(self.seed)], client=client, env={}, **kwargs)
        self.assertEqual(sha256_file(self.parent), self.parent_hash)
        return directory, manifest, client

    def document(self, blank=False):
        rec = make_pdf(self.root / "source.pdf", record(), blank_second=blank)
        doc = pdf_document(Path(rec.local_pdf), rec, max_pages=1, ocr="off")
        doc["source"] = {"artifact": rec.local_pdf}
        folder = self.root / "original"
        folder.mkdir()
        path = content_store.write_document(folder, doc)
        return rec, path, doc

    def test_query_errors_have_offsets(self):
        out = inspect_query('("x"[tiab] AND', "pubmed")
        self.assertEqual({e["code"] for e in out["errors"]}, {"unclosed_delimiter", "trailing_boolean_operator"})
        self.assertEqual(out["errors"][0]["offset"], 0)

    def test_query_dialects_quotes_and_warnings_preserve_text(self):
        query = '"salt (and water)"[tiab] OR sodium[FutureField]'
        out = inspect_query(query, "pubmed")
        self.assertEqual(out["query"], query)
        self.assertEqual(out["errors"], [])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertFalse(out["scientific_completeness_verified"])
        self.assertEqual(inspect_query('ti:"and" AND cat:cs.AI', "arxiv")["status"], "checked_subset")

    def test_query_plan_recomputes_audit(self):
        value = validate_plan(plan())
        value["query_validation"] = {"scientific_completeness_verified": True}
        self.assertFalse(validate_plan(value)["query_validation"]["scientific_completeness_verified"])

    def test_query_rejects_malformed_native_route(self):
        value = plan()
        value["routes"] = [{"id": "p", "source": "pubmed", "query": '"unclosed', "syntax": "native", "limit": 2}]
        with self.assertRaisesRegex(ValueError, "syntax error"):
            validate_plan(value)

    def test_both_citation_directions_dedupe(self):
        directory, manifest, client = self.expand([page(), page()])
        self.assertEqual(len(manifest["records"]), 1)
        self.assertEqual([c[1]["filter"] for c in client.calls], ["cited_by:W1", "cites:W1"])
        state = json.loads((directory / "expansion-state.json").read_text())
        self.assertEqual(len(state["edges"]), 2)
        self.assertFalse(manifest["lineage"]["reference_list_exhaustive"])

    def test_citation_resume_uses_saved_cursor(self):
        directory, manifest, _ = self.expand([page(cursor="next")], direction="citing", max_pages=1)
        self.assertEqual(next(iter(manifest["source_status"].values()))["status"], "partial_budget")
        _, resumed, client = self.expand([page(3)], "next", direction="citing", resume_state=directory / "expansion-state.json")
        self.assertEqual(client.calls[0][1]["cursor"], "next")
        self.assertEqual(len(resumed["records"]), 2)

    def test_citation_error_is_not_empty_success(self):
        _, manifest, _ = self.expand([ValueError("secret must not be logged")], direction="citing")
        status = next(iter(manifest["source_status"].values()))
        self.assertEqual(status["status"], "error")
        self.assertNotIn("secret", json.dumps(manifest))

    def test_empty_citation_page_is_explicit_complete(self):
        _, manifest, _ = self.expand([{"results": [], "meta": {"next_cursor": None}}], direction="referenced")
        self.assertEqual(len(manifest["records"]), 0)
        self.assertEqual(next(iter(manifest["source_status"].values()))["status"], "complete")

    def test_repeated_cursor_stops(self):
        _, manifest, _ = self.expand([page(cursor="*")], direction="citing")
        self.assertEqual(next(iter(manifest["source_status"].values()))["status"], "error")

    def test_changed_parent_rejects_continuation(self):
        directory, _, _ = self.expand([page(cursor="next")], direction="citing", max_pages=1)
        with self.parent.open("a") as stream:
            stream.write(" ")
        with self.assertRaisesRegex(ValueError, "same frozen parent"):
            relations.expand(self.parent, self.root / "bad", self.config, seeds=[candidate_id(self.seed)], direction="citing",
                resume_state=directory / "expansion-state.json", client=FakeClient([]), env={})

    def test_unknown_seed_cannot_trigger_search(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            relations.expand(self.parent, self.root / "bad", self.config, seeds=["unknown"], client=FakeClient([]), env={})

    def test_seed_aliases_cannot_repeat_same_work(self):
        with self.assertRaisesRegex(ValueError, "same work twice"):
            relations.expand(self.parent, self.root / "bad", self.config,
                seeds=[candidate_id(self.seed), "doi:"+self.seed.doi], client=FakeClient([]), env={})

    def test_bad_citation_page_has_no_partially_committed_edges(self):
        response = page()
        response["results"].append({"id": "https://openalex.org/W3"})
        directory, manifest, _ = self.expand([response], direction="citing")
        state = json.loads((directory / "expansion-state.json").read_text())
        self.assertEqual(state["edges"], [])
        self.assertEqual(manifest["records"], [])

    def test_one_hop_cycle_never_launches_new_seed_requests(self):
        _, manifest, client = self.expand([page(1), page(1)])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(manifest["records"]), 1)

    def test_cache_hit_and_option_change(self):
        rec = make_pdf(self.root / "s.pdf", record())
        folder = self.root / "cache"
        first = content_store.cached_pdf_document(Path(rec.local_pdf), rec, cache_dir=folder, max_pages=1, ocr="off")
        with patch.object(content_store, "pdf_document", side_effect=AssertionError("Must use cache")):
            second = content_store.cached_pdf_document(Path(rec.local_pdf), rec, cache_dir=folder, max_pages=1, ocr="off")
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        third = content_store.cached_pdf_document(Path(rec.local_pdf), rec, cache_dir=folder, max_pages=2, ocr="off")
        self.assertNotEqual(first["cache"]["key"], third["cache"]["key"])

    def test_cache_damage_and_source_change_rejected(self):
        rec = make_pdf(self.root / "s.pdf", record())
        folder = self.root / "cache"
        doc = content_store.cached_pdf_document(Path(rec.local_pdf), rec, cache_dir=folder, ocr="off")
        cache = folder / (doc["cache"]["key"] + ".json")
        cache.write_text('{}')
        with self.assertRaisesRegex(ValueError, "cache integrity"):
            content_store.cached_pdf_document(Path(rec.local_pdf), rec, cache_dir=folder, ocr="off")
        Path(rec.local_pdf).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum changed"):
            content_store.cached_pdf_document(Path(rec.local_pdf), rec, cache_dir=folder, ocr="off")

    def test_cache_engine_version_invalidates_key(self):
        rec = make_pdf(self.root / "s.pdf", record())
        with patch.object(content_store, "version", return_value="one"):
            first = content_store.cached_pdf_document(Path(rec.local_pdf), rec, cache_dir=self.root / "cache", ocr="off")
        with patch.object(content_store, "version", return_value="two"):
            second = content_store.cached_pdf_document(Path(rec.local_pdf), rec, cache_dir=self.root / "cache", ocr="off")
        self.assertNotEqual(first["cache"]["key"], second["cache"]["key"])

    def test_next_pages_preserves_prior_elements_and_source(self):
        _, path, old = self.document()
        digest = sha256_file(path)
        with patch.object(content_store, "pdf_document", wraps=pdf_document) as extractor:
            _, doc = content_store.next_pages(path, self.root / "next", self.config, max_pages=1)
        self.assertEqual(extractor.call_args.kwargs["page_numbers"], [2])
        self.assertEqual(doc["elements"][:len(old["elements"])], old["elements"])
        self.assertEqual([p["page"] for p in doc["coverage"]["pages"]], [1, 2])
        self.assertFalse(any(g["reason"] == "page_limit" for g in doc["gaps"]))
        self.assertEqual(doc["ai_reading_status"], "not_submitted")
        self.assertEqual(sha256_file(path), digest)

    def test_blank_continuation_page_is_recorded_not_lost(self):
        _, path, _ = self.document(blank=True)
        _, doc = content_store.next_pages(path, self.root / "next", self.config)
        self.assertEqual(doc["coverage"]["pages"][-1]["status"], "no_text")

    def test_find_returns_locator_not_reading_claim(self):
        _, path, _ = self.document()
        hit = content_store.find_content(path, "methods")
        self.assertEqual(hit["hits"][0]["page"], 1)
        self.assertFalse(hit["full_paper_read"])
        self.assertEqual(content_store.find_content(path, "methods", section_filter="Absent")["hits"], [])

    def test_source_change_blocks_lookup(self):
        rec, path, _ = self.document()
        Path(rec.local_pdf).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "source is missing or changed"):
            content_store.find_content(path, "methods")

    def asset_document(self):
        rec, path, doc = self.document()
        doc["source"].update(url="https://example.org/article/", candidate={"oa_verified": True})
        doc["elements"][0]["resource_references"] = ["a.png", "b.pdf"]
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path, doc

    def test_asset_resume_reuses_verified_and_fetches_remaining(self):
        path, doc = self.asset_document()
        self.config["content"]["asset_limit"] = 1
        with patch.object(assets, "_fetch_bounded", return_value=(b"\x89PNG\r\n\x1a\nsynthetic", "https://example.org/article/a.png", "")):
            assets.acquire_assets(doc, path.parent, object(), self.config)
        path.write_text(json.dumps(doc), encoding="utf-8")
        digest = sha256_file(path)
        with patch.object(assets, "_fetch_bounded", return_value=(b"%PDF-synthetic", "https://example.org/article/b.pdf", "")) as fetch:
            _, result = assets.resume_assets(path, self.root / "resume", self.config, client=object())
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(fetch.call_args.args[1].endswith("b.pdf"))
        self.assertEqual(result["asset_summary"]["reused_references"], 1)
        self.assertEqual(result["asset_summary"]["pending"], [])
        self.assertFalse(result["asset_summary"]["interpretation_certified"])
        self.assertEqual(sha256_file(path), digest)

    def test_corrupt_asset_not_counted_as_reused(self):
        path, doc = self.asset_document()
        with patch.object(assets, "_fetch_bounded", return_value=(b"%PDF-synthetic", "https://example.org/file", "")):
            assets.acquire_assets(doc, path.parent, object(), self.config)
        Path(doc["elements"][0]["assets"][0]["path"]).write_bytes(b"bad")
        path.write_text(json.dumps(doc), encoding="utf-8")
        _, result = assets.resume_assets(path, self.root / "resume", self.config, local_only=True, client=object())
        self.assertEqual(result["asset_summary"]["reused_references"], 0)
        self.assertEqual(len(result["asset_summary"]["pending"]), 2)

    def benchmark(self):
        path = self.root / "benchmark.json"
        path.write_text(json.dumps({"schema_version": 1, "benchmark_id": "synthetic", "kind": "synthetic",
            "frozen_at": "2026-10-03", "judgments": [
                {"ids": ["doi:10.1234/a", "openalex:W1"], "relevant": True, "basis": "synthetic label"},
                {"ids": ["doi:10.1234/b"], "relevant": True, "basis": "synthetic label"},
                {"ids": ["doi:10.1234/c"], "relevant": False, "basis": "synthetic label"}]}))
        return path

    def test_evaluation_fixed_denominators_and_unknowns(self):
        benchmark = self.benchmark()
        pipeline.save_manifest(self.parent, pipeline.new_manifest("synthetic", [record("c"), record("a")], {"p": {"status": "partial"}}, {}))
        result = evaluate(benchmark, self.parent, k=2)
        self.assertEqual(result["precision_at_k"], .5)
        self.assertEqual(result["recall_of_judged_relevant_at_k"], .5)
        self.assertEqual(result["source_status"]["p"]["status"], "partial")
        pipeline.save_manifest(self.parent, pipeline.new_manifest("synthetic", [record("unknown"), record("a")], {}, {}))
        result = evaluate(benchmark, self.parent, k=2)
        self.assertIsNone(result["precision_at_k"])
        self.assertEqual(result["precision_lower_bound_at_k"], .5)

    def test_evaluation_cannot_double_count_aliases(self):
        benchmark = self.benchmark()
        pipeline.save_manifest(self.parent, pipeline.new_manifest("synthetic", [self.seed, self.seed, record("b")], {}, {}))
        result = evaluate(benchmark, self.parent, k=2)
        self.assertEqual(result["precision_at_k"], 1)
        self.assertEqual(result["recall_of_judged_relevant_at_k"], 1)

    def test_conflicting_benchmark_aliases_rejected(self):
        benchmark = self.benchmark()
        data = json.loads(benchmark.read_text())
        data["judgments"][1]["ids"].append("doi:10.1234/a")
        benchmark.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "unique"):
            evaluate(benchmark, self.parent)

    def test_new_cli_commands_offline(self):
        from contextlib import redirect_stdout
        from io import StringIO
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(plan()))
        _, document, _ = self.document()
        commands = [
            ["query-check", "--plan", str(plan_path)],
            ["content-find", "--document", str(document), "--query", "methods"],
            ["content-next", "--document", str(document)],
            ["assets-resume", "--document", str(document), "--local-only"],
            ["evaluate", "--manifest", str(self.parent), "--benchmark", str(self.benchmark())],
        ]
        for i, command in enumerate(commands):
            with redirect_stdout(StringIO()):
                self.assertEqual(main(command + ["--output-dir", str(self.root / f"cli-{i}")]), 0)


if __name__ == "__main__":
    unittest.main()
