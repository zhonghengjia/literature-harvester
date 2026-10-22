"""Reviewable same-budget strategy plans; never silently rewrite an input plan."""
from __future__ import annotations

from copy import deepcopy
import re

from .query_plan import digest_json, validate_plan


def build_coverage_plan(plan, per_source_budget=100):
    """Return an envelope with original_plan and executable query_plan.

    Each original precision route is paired with a native broad route. Budgets
    count provider result slots, not unique papers or HTTP requests. Quota is not
    redistributed after search, so a source that exhausts returns fewer results.
    Hand-written queries are accepted only when identical to the declared
    concepts' standard precision compilation: extra constraints cannot be lost.
    """
    if type(per_source_budget) is not int or per_source_budget < 2:
        raise ValueError("per_source_budget must be an integer >= 2")
    original = deepcopy(plan)
    normalized = validate_plan(plan)
    if not normalized["concepts"]:
        raise ValueError("Coverage planning requires explicit concepts and synonyms")
    by_id = {c["id"]: c for c in normalized["concepts"]}
    pairs = []
    for raw, route in zip(plan["routes"], normalized["routes"]):
        source = route["source"]
        if source not in {"pubmed", "europe_pmc"}:
            raise ValueError("Coverage planning supports only PubMed and Europe PMC")
        ids = raw.get("concept_ids") or list(by_id)
        probe = {"schema_version": 1, "question": normalized["question"], "concepts": normalized["concepts"],
                 "routes": [{"id": "precision", "source": source, "concept_ids": ids, "limit": 1}]}
        expected = validate_plan(probe)["routes"][0]["query"]
        if not raw.get("concept_ids") and route["query"] != expected:
            raise ValueError(f"Route {route['id']} needs explicit concept_ids; arbitrary queries are not reverse-parsed")
        groups = []
        for ident in ids:
            terms = by_id[ident]["terms"]
            if source == "pubmed":
                # Bare literal tokens permit PubMed Automatic Term Mapping;
                # syntax/control characters and Boolean tokens are not literals.
                if any(not re.fullmatch(r"[\w\s-]+", t, re.UNICODE) or
                       re.search(r"\b(?:AND|OR|NOT)\b", t, re.I) for t in terms):
                    raise ValueError("PubMed ATM terms must be plain words/hyphens, without Boolean operators")
                alternatives = ["(" + term + ")" if " " in term else term for term in terms]
                alternatives += [f'"{h["term"]}"[MeSH Terms]' for h in by_id[ident].get("mesh_terms", [])]
            else:
                alternatives = ['"' + term + '"' for term in terms]
            groups.append("(" + " OR ".join(alternatives) + ")")
        pairs.append((route, ids, " AND ".join(groups)))
    routes, ledger, budgets = [], [], {}
    for source in dict.fromkeys(route["source"] for route, _, _ in pairs):
        selected = [pair for pair in pairs if pair[0]["source"] == source]
        slots = 2 * len(selected)
        if per_source_budget < slots:
            raise ValueError("Budget is too small to give every complementary route a positive quota")
        base, remainder = divmod(per_source_budget, slots)
        index = 0
        for route, ids, broad in selected:
            for strategy, query in (("precision", route["query"]), ("native_broad", broad)):
                # Generated IDs avoid collisions/truncation of caller route IDs.
                name = f"coverage_{len(routes)+1}_{source}_{strategy}"
                quota = base + (index < remainder)
                index += 1
                routes.append({"id": name, "source": source, "query": query, "syntax": "native", "limit": quota})
                ledger.append({"route_id": name, "original_route_id": route["id"], "concept_ids": list(ids),
                               "strategy": strategy, "limit": quota,
                               "semantics": ("PubMed unfielded ATM" if source == "pubmed" else
                                             "Europe PMC unfielded phrase search; not PubMed ATM") if strategy == "native_broad"
                                            else "Original title/abstract precision route"})
        budgets[source] = {"allocated_records": per_source_budget, "route_count": slots}
    executable = {"schema_version": 1, "question": normalized["question"], "concepts": normalized["concepts"],
                  "routes": routes, "limits": normalized["limits"]}
    if normalized.get("scope_note"):
        executable["scope_note"] = normalized["scope_note"]
    return {"schema_version": 1, "experiment_type": "same_budget_strategy_experiment",
            "original_plan": original, "original_plan_sha256": digest_json(original),
            "query_plan": validate_plan(executable), "budgets": budgets, "route_strategies": ledger,
            "limitations": ["Result-slot quotas are not a recall guarantee or an HTTP-request budget",
                            "No population, date or open-access filters were added",
                            "Source-native broad search is not semantically identical across providers"]}
