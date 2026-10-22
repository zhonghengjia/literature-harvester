"""Validated, bounded query plans and deterministic discovery provenance."""
from __future__ import annotations

import hashlib
import json
import re
from .models import normalize_title
from .settings import DEFAULT_CONFIG, positive

SOURCES = {"pubmed", "europe_pmc", "crossref", "openalex", "semantic_scholar", "arxiv"}
NATIVE = {"pubmed", "europe_pmc", "arxiv"}


def _text(value, name, limit=50000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 and c not in '\r\n\t' for c in value):
        raise ValueError(f"Invalid {name}")
    return " ".join(value.split())


def _keys(value, allowed, name):
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"Unexpected fields in {name}")


def validate_plan(plan, limits=None):
    _keys(plan, {"schema_version", "question", "scope_note", "concepts", "routes", "limits", "query_validation", "coverage"}, "query plan")
    supplied = plan.get("limits", {})
    _keys(supplied, set(DEFAULT_CONFIG["plan_limits"]), "plan limits")
    bounds = {**DEFAULT_CONFIG["plan_limits"], **(limits or {}), **supplied}
    for key, value in bounds.items():
        positive(value, key)
    if type(plan.get("schema_version")) is not int or plan["schema_version"] != 1:
        raise ValueError("Query plan schema_version must be 1")
    question = _text(plan.get("question"), "question", bounds["text_chars"])
    concepts = []
    if not isinstance(plan.get("concepts", []), list) or len(plan.get("concepts", [])) > bounds["concepts"]:
        raise ValueError("Concept count exceeds configured plan budget")
    for item in plan.get("concepts", []):
        _keys(item, {"id", "terms", "mesh_terms"}, "concept")
        name = _text(item.get("id"), "concept id", 64)
        terms = item.get("terms")
        if not isinstance(terms, list) or not 1 <= len(terms) <= bounds["terms"]:
            raise ValueError("Concept terms exceed configured plan budget")
        terms = list(dict.fromkeys(_text(term, "concept term", bounds["term_chars"]) for term in terms))
        if any('"' in term or '[' in term or ']' in term for term in terms):
            raise ValueError("Concept terms are literal phrases, not query expressions")
        concept = {"id": name, "terms": terms}
        mesh = item.get("mesh_terms", [])
        if not isinstance(mesh, list) or len(mesh) > bounds["terms"]:
            raise ValueError("Invalid MeSH terms")
        if mesh:
            concept["mesh_terms"] = []
            for heading in mesh:
                _keys(heading, {"term", "evidence_url"}, "verified MeSH heading")
                term = _text(heading.get("term"), "MeSH heading", bounds["term_chars"])
                evidence = _text(heading.get("evidence_url"), "MeSH verification source")
                if any(ch in term for ch in '"[]'):
                    raise ValueError("MeSH heading must be literal")
                from urllib.parse import urlparse
                if urlparse(evidence).scheme != "https" or urlparse(evidence).hostname not in {"meshb.nlm.nih.gov", "id.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}:
                    raise ValueError("MeSH headings require an explicit NLM verification source")
                concept["mesh_terms"].append({"term": term, "evidence_url": evidence})
        concepts.append(concept)
    if len({c["id"] for c in concepts}) != len(concepts):
        raise ValueError("Concept ids must be unique")
    by_id = {c["id"]: c for c in concepts}
    routes = plan.get("routes")
    if not isinstance(routes, list) or not 1 <= len(routes) <= bounds["routes"]:
        raise ValueError("Route count exceeds configured plan budget")
    compiled = []
    for item in routes:
        _keys(item, {"id", "source", "query", "syntax", "concept_ids", "limit"}, "route")
        route_id = _text(item.get("id"), "route id", 64)
        source = item.get("source")
        syntax = item.get("syntax", "text")
        limit = item.get("limit", 100)
        if not isinstance(source, str) or source not in SOURCES or not isinstance(syntax, str) or syntax not in {"text", "native"}:
            raise ValueError("Unknown source or query syntax")
        if route_id in {"selection", "openalex_content"}:
            raise ValueError("Route id conflicts with a reserved status name")
        if type(limit) is not int or not 1 <= limit <= bounds["route_records"]:
            raise ValueError("Route record limit exceeds configured plan budget")
        ids = item.get("concept_ids", [])
        if not isinstance(ids, list) or any(not isinstance(i, str) or i not in by_id for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Route concept_ids must refer to unique declared concepts")
        if ids:
            if source not in NATIVE:
                raise ValueError("Concept compilation supports PubMed, Europe PMC and arXiv; provide text queries for other sources")
            def phrase(term):
                if source == "pubmed":
                    return f'"{term}"[Title/Abstract]'
                return f'{"TITLE_ABS" if source == "europe_pmc" else "all"}:"{term}"'
            groups = []
            for ident in ids:
                alternatives = [phrase(t) for t in by_id[ident]["terms"]]
                if source == "pubmed":
                    alternatives += [f'"{h["term"]}"[MeSH Terms]' for h in by_id[ident].get("mesh_terms", [])]
                groups.append("(" + " OR ".join(alternatives) + ")")
            query = " AND ".join(groups)
            # Normalized plans retain their concept subset. A supplied compiled
            # query is allowed only if it is exactly that same compilation.
            if item.get("query") and (_text(item["query"], "query", bounds["text_chars"]) != query
                                      or syntax != "native"):
                raise ValueError("A route with query and concept_ids must match the exact native concept compilation")
            syntax = "native"
        else:
            query = _text(item.get("query"), "query", bounds["text_chars"])
        if syntax == "native" and source not in NATIVE:
            raise ValueError("Native syntax is not supported for this source")
        compiled.append({"id": route_id, "source": source, "query": query, "syntax": syntax, "limit": limit,
                         **({"concept_ids": list(ids)} if ids else {})})
    if len({r["id"] for r in compiled}) != len(compiled) or sum(r["limit"] for r in compiled) > bounds["total_records"]:
        raise ValueError("Route ids must be unique and total candidates within configured plan budget")
    normalized = {"schema_version": 1, "question": question, "concepts": concepts, "routes": compiled}
    normalized["limits"] = bounds
    if plan.get("scope_note"):
        normalized["scope_note"] = _text(plan["scope_note"], "scope note", bounds["text_chars"])
    from .query_lint import inspect_plan
    normalized["query_validation"] = inspect_plan(normalized)
    bad = [r for r in normalized["query_validation"]["routes"] if r["errors"]]
    if bad:
        raise ValueError("Query syntax error in route " + bad[0]["route_id"] + ": " + str(bad[0]["errors"]))
    if "coverage" in plan:
        from .coverage_plan import validate_coverage_marker
        normalized["coverage"] = validate_coverage_marker(plan["coverage"], normalized, limits)
    return normalized


def digest_json(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def candidate_id(record):
    # Include all strong IDs: records held apart for an ID conflict must not share a queue ID.
    identity = {"ids": record.identity_keys, "year": record.year, "authors": record.authors}
    if not any(not k.startswith("title:") for k in record.identity_keys):
        identity["source_url"] = record.url
    return digest_json(identity)[:24]


def concept_evidence(record, concepts):
    matches = []
    fields = {"title": normalize_title(record.title), "abstract": normalize_title(record.abstract)}
    for concept in concepts:
        for term in concept["terms"]:
            normalized = normalize_title(term)
            if not normalized:
                continue
            for field, text in fields.items():
                if re.search(r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", text):
                    matches.append({"concept_id": concept["id"], "term": term, "field": field})
    return matches


class SelectedRecords(list):
    """Legacy list contract plus the complete plan-mode pool, never a hidden download population."""
    def __init__(self, selected, candidates):
        super().__init__(selected)
        self.candidates = candidates
