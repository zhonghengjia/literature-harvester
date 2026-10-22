"""Preserved-original additive discovery and explicit fixed-budget experiments."""
from __future__ import annotations

from copy import deepcopy
import re

from .query_plan import digest_json, validate_plan


def _build_fixed(plan, per_source_budget=100, limits=None):
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
    normalized = validate_plan(plan, limits)
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


def _bounded(value, depth=0):
    if depth > 16:
        raise ValueError("Coverage provenance nesting exceeds 16 levels")
    if isinstance(value, dict):
        for item in value.values():
            _bounded(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1)


def _projection(plan):
    return {k: v for k, v in plan.items() if k not in {"coverage", "query_validation"}}


def _native_broad(concepts, source):
    from .sources import detect_identifier
    groups = []
    for concept in concepts:
        terms = concept["terms"]
        if any(detect_identifier(t)[0] != "text" for t in terms):
            raise ValueError("known_identifier_concept")
        if source == "pubmed":
            if any(not re.fullmatch(r"[\w\s-]+", t, re.UNICODE) or
                   re.search(r"\b(?:AND|OR|NOT)\b", t, re.I) for t in terms):
                raise ValueError("unsafe_atm_literal")
            alternatives = [f"({t})" if " " in t else t for t in terms]
            alternatives += [f'"{h["term"]}"[MeSH Terms]' for h in concept.get("mesh_terms", [])]
        else:
            alternatives = [f'"{t}"' for t in terms]
        groups.append("(" + " OR ".join(alternatives) + ")")
    return " AND ".join(groups)


def _build_additive(plan, additional_per_source, limits):
    normalized = validate_plan(plan, limits)
    by_id = {c["id"]: c for c in normalized["concepts"]}
    ledger, skipped, candidates = [], [], {}
    existing = {(r["source"], r["syntax"], r["query"]): r["id"] for r in normalized["routes"]}
    for raw, route in zip(plan["routes"], normalized["routes"]):
        entry = {"route_id": route["id"], "original_route_id": route["id"], "strategy": "original",
                 "source": route["source"], "limit": route["limit"]}
        ledger.append(entry)
        source = route["source"]
        reason = ""
        if source not in {"pubmed", "europe_pmc"}:
            reason = "unsupported_source_preserved"
        elif not by_id:
            from .sources import detect_identifier
            reason = "known_identifier_preserved" if detect_identifier(route["query"])[0] != "text" else "no_declared_concepts"
        ids = raw.get("concept_ids") or list(by_id)
        if not reason:
            probe = {"schema_version": 1, "question": normalized["question"], "concepts": normalized["concepts"],
                     "routes": [{"id": "probe", "source": source, "concept_ids": ids, "limit": 1}]}
            expected = validate_plan(probe)["routes"][0]["query"]
            if route["query"] != expected:
                reason = "handwritten_query_or_restrictions_preserved"
        if not reason:
            try:
                broad = _native_broad([by_id[i] for i in ids], source)
            except ValueError as exc:
                reason = str(exc)
        if reason:
            skipped.append({"route_id": route["id"], "source": source, "reason": reason})
            entry["complementary_status"] = "skipped"
            entry["reason"] = reason
            continue
        fingerprint = (source, "native", broad)
        if fingerprint in existing:
            entry.update(complementary_status="already_present", complementary_route_id=existing[fingerprint])
            skipped.append({"route_id": route["id"], "source": source, "reason": "identical_complementary_query_already_present",
                            "existing_route_id": existing[fingerprint]})
            continue
        candidates.setdefault(fingerprint, []).append((route["id"], ids))
        entry["complementary_status"] = "planned"
    routes = deepcopy(normalized["routes"])
    used = {r["id"] for r in routes}
    for source in ("pubmed", "europe_pmc"):
        selected = [(fp, origins) for fp, origins in candidates.items() if fp[0] == source]
        if not selected:
            continue
        if additional_per_source < len(selected):
            raise ValueError("Additional source budget cannot allocate one slot to each complementary route; original routes unchanged")
        base, remainder = divmod(additional_per_source, len(selected))
        for index, (fingerprint, origins) in enumerate(selected):
            ident = "add_" + source + "_" + digest_json([fingerprint, origins])[:16]
            if ident in used:
                raise ValueError("Generated complementary route ID conflicts with an original route ID")
            used.add(ident)
            quota = base + (index < remainder)
            routes.append({"id": ident, "source": source, "query": fingerprint[2], "syntax": "native", "limit": quota})
            ledger.append({"route_id": ident, "original_route_ids": [o[0] for o in origins],
                           "concept_ids": origins[0][1], "strategy": "native_broad", "source": source, "limit": quota})
    executable = {**_projection(normalized), "routes": routes}
    budgets = {}
    for source in dict.fromkeys(r["source"] for r in routes):
        original = sum(r["limit"] for r in normalized["routes"] if r["source"] == source)
        total = sum(r["limit"] for r in routes if r["source"] == source)
        budgets[source] = {"original_records": original, "additional_records": total-original, "allocated_records": total}
    totals = "; ".join(f"{s}: original {v['original_records']} + additional {v['additional_records']} = {v['allocated_records']}" for s, v in budgets.items())
    executable["scope_note"] = ((normalized.get("scope_note", "") + "\n") if normalized.get("scope_note") else "") + "Additive result-slot budgets: " + totals
    # Fail before requests; never trim original routes to fit total/route caps.
    try:
        checked = validate_plan(executable, limits)
    except ValueError as exc:
        raise ValueError("Additive plan exceeds or violates configured limits; original routes were not reduced: " + str(exc)) from exc
    return {"schema_version": 1, "experiment_type": "preserved_original_plus_complementary",
            "original_plan": deepcopy(plan), "original_plan_sha256": digest_json(plan),
            "query_plan": checked, "route_strategies": ledger, "skipped_routes": skipped, "budgets": budgets,
            "limitations": ["Additional result slots, not a fixed-total-budget experiment or recall guarantee",
                            "Original scope text retained in original_plan; original route IDs/queries/quotas preserved",
                            "No population/date/OA constraints added or removed; ineligible routes remain unchanged"]}


def build_coverage_plan(plan, per_source_budget=100, *, mode="additive", additional_per_source=50, limits=None):
    """Build a bounded, reviewable envelope, preserving validated prior strategies.

    per_source_budget applies only to explicit fixed-budget mode; additive keeps
    every original quota and allocates additional_per_source across safe native
    complements per source. The embedded provenance is re-derived, not trusted.
    """
    from .settings import positive
    if mode not in {"additive", "fixed-budget"}:
        raise ValueError("Coverage mode must be additive or fixed-budget")
    if type(per_source_budget) is not int or per_source_budget < 2:
        raise ValueError("per_source_budget must be an integer >= 2")
    positive(additional_per_source, "additional_per_source")
    if isinstance(plan, dict) and "coverage" in plan:
        normalized = validate_plan(plan, limits)
        marker = normalized["coverage"]
        return {"schema_version": 1, "experiment_type": marker["experiment_type"],
                "original_plan": deepcopy(marker["original_plan"]),
                "original_plan_sha256": marker["original_plan_sha256"], "query_plan": normalized,
                "route_strategies": marker["route_strategies"], "skipped_routes": marker["skipped_routes"],
                "budgets": marker["budgets"], "already_expanded": True, "mode": marker["mode"]}
    _bounded(plan)
    result = (_build_additive(plan, additional_per_source, limits) if mode == "additive" else
              _build_fixed(plan, per_source_budget, limits))
    result.setdefault("skipped_routes", [])
    result["mode"] = mode
    marker = {"schema_version": 1, "mode": mode, "additional_per_source": additional_per_source,
              "per_source_budget": per_source_budget, "experiment_type": result["experiment_type"],
              "original_plan": deepcopy(plan), "original_plan_sha256": digest_json(plan),
              "executable_sha256": digest_json(_projection(result["query_plan"])),
              "route_strategies": deepcopy(result["route_strategies"]), "budgets": deepcopy(result["budgets"]),
              "skipped_routes": deepcopy(result["skipped_routes"])}
    _check_marker_size(marker)
    result["query_plan"]["coverage"] = marker
    return result


def _check_marker_size(marker):
    import json
    _bounded(marker)
    if len(json.dumps(marker, ensure_ascii=False).encode("utf-8")) > 2 * 1024**2:
        raise ValueError("Coverage provenance exceeds 2 MiB")


def validate_coverage_marker(marker, normalized, limits=None):
    """Called by the shared validator; reject edited claims/routes before I/O."""
    fields = {"schema_version", "mode", "additional_per_source", "per_source_budget", "experiment_type",
              "original_plan", "original_plan_sha256", "executable_sha256", "route_strategies", "budgets", "skipped_routes"}
    if not isinstance(marker, dict) or set(marker) != fields or type(marker.get("schema_version")) is not int or marker["schema_version"] != 1:
        raise ValueError("Malformed coverage provenance")
    _check_marker_size(marker)
    original = marker["original_plan"]
    if not isinstance(original, dict) or "coverage" in original or digest_json(original) != marker["original_plan_sha256"]:
        raise ValueError("Nested or changed original coverage plan")
    expected = build_coverage_plan(original, marker["per_source_budget"], mode=marker["mode"],
        additional_per_source=marker["additional_per_source"], limits=limits)
    if expected["query_plan"]["coverage"] != marker or digest_json(_projection(normalized)) != marker["executable_sha256"]:
        raise ValueError("Coverage provenance or executable routes changed; regenerate the plan explicitly")
    return deepcopy(marker)
