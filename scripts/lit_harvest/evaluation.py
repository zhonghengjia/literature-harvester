"""Evaluate a frozen run against independently fixed work-level judgments."""
from __future__ import annotations

import json
from pathlib import Path
from .downloader import sha256_file
from .pipeline import load_manifest
from .query_plan import candidate_id
from .models import normalize_work_alias
from .settings import positive


def evaluate(benchmark_path, manifest_path, *, k=25):
    positive(k, "k")
    benchmark_path, manifest_path = Path(benchmark_path).resolve(), Path(manifest_path).resolve()
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    if (benchmark.get("schema_version") != 1 or not benchmark.get("benchmark_id") or
            not benchmark.get("frozen_at") or not isinstance(benchmark.get("judgments"), list)):
        raise ValueError("Benchmark requires schema_version=1, benchmark_id, frozen_at and judgments")
    aliases, judgments = {}, benchmark["judgments"]
    for index, item in enumerate(judgments):
        ids = item.get("ids")
        if (not isinstance(ids, list) or not ids or type(item.get("relevant")) is not bool or
                not isinstance(item.get("basis"), str) or not item["basis"].strip()):
            raise ValueError("Every work judgment requires ids, boolean relevant and an independent basis")
        for ident in ids:
            if (not isinstance(ident, str) or not ident.strip() or normalize_work_alias(ident) in aliases):
                raise ValueError("Judgment identifiers must be nonempty and unique, including aliases")
            aliases[normalize_work_alias(ident)] = index
    manifest, records = load_manifest(manifest_path)
    relevant_total = sum(j["relevant"] for j in judgments)
    seen = set()
    ranked = []
    for record in records:
        ident = record.extra.get("candidate_id") or candidate_id(record)
        keys = {normalize_work_alias(i) for i in [*record.identity_keys, ident, candidate_id(record)]}
        matches = {aliases[i] for i in keys if i in aliases}
        if len(matches) > 1:
            raise ValueError("A retrieved record matches conflicting benchmark works")
        match = next(iter(matches), None)
        identity = ("judged", match) if match is not None else ("unknown", ident)
        if identity in seen:
            continue
        seen.add(identity)
        ranked.append({"candidate_id": ident, "judgment_index": match,
            "relevant": judgments[match]["relevant"] if match is not None else None})
    top = ranked[:k]
    relevant = sum(r["relevant"] is True for r in top)
    unknown = sum(r["relevant"] is None for r in top)
    return {"schema_version": 1, "benchmark_id": benchmark["benchmark_id"],
        "benchmark_sha256": sha256_file(benchmark_path), "manifest_sha256": sha256_file(manifest_path),
        "judgments_independently_verified": False, "benchmark_kind": benchmark.get("kind", "unspecified"),
        "ranking_population": "manifest record order; not an exhaustive literature universe",
        "k": k, "returned_at_k": len(top), "known_relevant_total": relevant_total,
        "relevant_at_k": relevant, "unknown_at_k": unknown,
        "precision_at_k": relevant / k if not unknown else None,
        "precision_lower_bound_at_k": relevant / k,
        "recall_of_judged_relevant_at_k": relevant / relevant_total if relevant_total else None,
        "source_status": manifest.get("source_status", {}), "ranked_at_k": top,
        "download_success_not_retrieval_recall": True, "ai_reading_not_evaluated": True}
