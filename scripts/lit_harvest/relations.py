"""Explicit one-hop citation discovery with immutable parent and page checkpoints."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import quote
from .downloader import sha256_file, user_agent
from .models import PaperRecord, normalize_work_alias
from .pipeline import load_manifest, new_manifest, save_manifest, create_run_dir, merge_records, effective_contact_email
from .query_plan import candidate_id
from .settings import make_client, positive, section
from .sources import openalex_record


def expand(manifest_path, output_dir, config, *, seeds, direction="both", max_pages=5,
           max_results=500, resume_state=None, client=None, env=None):
    positive(max_pages, "max_pages")
    positive(max_results, "max_results")
    if direction not in {"both", "referenced", "citing"} or not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("Use unique explicit seeds and a supported direction")
    parent_path = Path(manifest_path).resolve()
    parent, population = load_manifest(parent_path)
    parent_hash = sha256_file(parent_path)
    selected = []
    for seed in seeds:
        matches = [r for r in population if normalize_work_alias(seed) in r.identity_keys
                   or seed in {candidate_id(r), r.extra.get("candidate_id")}]
        if len(matches) != 1:
            raise ValueError("Each seed must identify exactly one parent record")
        selected.append(matches[0])
    if len({candidate_id(r) for r in selected}) != len(selected):
        raise ValueError("Seed aliases must not select the same work twice")
    contract = {"parent_manifest_sha256": parent_hash, "seeds": seeds, "direction": direction}
    state = {"schema_version": 1, "contract": contract, "records": [], "edges": [], "routes": {}}
    if resume_state:
        state = json.loads(Path(resume_state).read_text(encoding="utf-8"))
        if state.get("schema_version") != 1 or state.get("contract") != contract:
            raise ValueError("Expansion continuation must use the same frozen parent, seeds and direction")
    directory = create_run_dir("citation-expansion", config, output_dir)
    env = os.environ if env is None else env
    client = client or make_client(config, user_agent=user_agent(effective_contact_email(config, env)))
    params = {"api_key": env.get("OPENALEX_API_KEY", ""), "mailto": effective_contact_email(config, env)}
    directions = ["referenced", "citing"] if direction == "both" else [direction]
    records = [PaperRecord.from_dict(r) for r in state["records"]]

    def checkpoint():
        state["records"] = [r.to_dict() for r in records]
        save_manifest(directory / "expansion-state.json", state)

    for seed, record in zip(seeds, selected):
        for side in directions:
            key = seed + "/" + side
            route = state["routes"].setdefault(key, {"cursor": "*", "seen_cursors": [], "count": 0, "status": "pending"})
            if route["status"] == "complete":
                continue
            if not section(config, "sources")["openalex"]:
                route["status"] = "disabled"
                checkpoint()
                continue
            try:
                ident = route.get("openalex_id") or record.openalex_id.rsplit("/", 1)[-1]
                if not re.fullmatch(r"W\d+", ident, re.I):
                    if not record.doi:
                        raise ValueError("Seed requires an OpenAlex ID or DOI; no title guessing")
                    entity = client.get_json("https://api.openalex.org/works/https://doi.org/" + quote(record.doi, safe="/"), params)
                    ident = str(entity.get("id", "")).rsplit("/", 1)[-1]
                    if not re.fullmatch(r"W\d+", ident, re.I):
                        raise ValueError("Seed lookup did not resolve a work")
                route["openalex_id"] = ident.upper()
                received = 0
                for _ in range(max_pages):
                    if received >= max_results:
                        break
                    cursor = route["cursor"]
                    response = client.get_json("https://api.openalex.org/works", {**params,
                        "filter": ("cited_by:" if side == "referenced" else "cites:") + ident,
                        "cursor": cursor, "per_page": min(100, max_results-received)})
                    if not isinstance(response, dict) or not isinstance(response.get("results"), list) or not isinstance(response.get("meta"), dict):
                        raise ValueError("Malformed citation page")
                    items = response["results"]
                    next_cursor = response["meta"].get("next_cursor")
                    if len(items) > min(100, max_results-received) or (next_cursor is not None and not isinstance(next_cursor, str)):
                        raise ValueError("Invalid citation page size or cursor")
                    if next_cursor and (next_cursor == cursor or next_cursor in route["seen_cursors"]):
                        raise ValueError("Citation cursor repeated")
                    additions, edges = [], []
                    for item in items:
                        candidate = openalex_record(item)
                        if candidate is None:
                            raise ValueError("Citation record has no usable title")
                        cid = candidate_id(candidate)
                        edge = {"seed": seed, "candidate_id_at_discovery": cid, "candidate_ids": candidate.identity_keys,
                                "direction": side, "source": "openalex"}
                        if edge not in state["edges"] and edge not in edges:
                            edges.append(edge)
                        candidate.extra["citation_parent"] = candidate_id(record)
                        additions.append(candidate)
                    # Use the established merge policy, never a new title/fuzzy matcher.
                    records = merge_records(records + additions)
                    state["edges"].extend(edges)
                    received += len(items)
                    route["count"] += len(items)
                    route["seen_cursors"].append(cursor)
                    route["cursor"] = next_cursor
                    route["status"] = "complete" if not next_cursor else "partial_budget"
                    route.pop("error_type", None)
                    checkpoint()
                    if not next_cursor:
                        break
            except Exception as exc:
                route.update(status="error", error_type=getattr(exc, "code", type(exc).__name__))
                checkpoint()
    checkpoint()
    for record in records:
        record.extra["candidate_id"] = candidate_id(record)
    result = new_manifest("Citation expansion of " + parent.get("query", ""), records, state["routes"],
                          {"one_hop": True, "max_pages_per_route": max_pages, "max_results_per_route": max_results})
    result["lineage"] = {**contract, "parent_run_id": parent.get("run_id"), "parent_manifest": str(parent_path),
        "expansion_state": str(directory / "expansion-state.json"), "state_sha256": sha256_file(directory / "expansion-state.json"),
        "reference_list_exhaustive": False, "new_seeds_require_explicit_new_run": True}
    save_manifest(directory / "manifest.json", result)
    return directory, result
