"""Validated, bounded query plans and deterministic discovery provenance."""
from __future__ import annotations

import hashlib
import json
import re
from .models import normalize_title

SOURCES = {"pubmed", "europe_pmc", "crossref", "openalex", "semantic_scholar", "arxiv"}
NATIVE = {"pubmed", "europe_pmc", "arxiv"}


def _text(value, name, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid {name}")
    return value.strip()


def _keys(value, allowed, name):
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"Unexpected fields in {name}")


def validate_plan(plan):
    _keys(plan, {"schema_version", "question", "scope_note", "concepts", "routes"}, "query plan")
    if type(plan.get("schema_version")) is not int or plan["schema_version"] != 1:
        raise ValueError("Query plan schema_version must be 1")
    question = _text(plan.get("question"), "question")
    concepts = []
    if not isinstance(plan.get("concepts", []), list) or len(plan.get("concepts", [])) > 12:
        raise ValueError("At most 12 concepts are allowed")
    for item in plan.get("concepts", []):
        _keys(item, {"id", "terms"}, "concept")
        name = _text(item.get("id"), "concept id", 64)
        terms = item.get("terms")
        if not isinstance(terms, list) or not 1 <= len(terms) <= 20:
            raise ValueError("Each concept requires 1..20 terms")
        terms = list(dict.fromkeys(_text(term, "concept term", 200) for term in terms))
        if any('"' in term or '[' in term or ']' in term for term in terms):
            raise ValueError("Concept terms are literal phrases, not query expressions")
        concepts.append({"id": name, "terms": terms})
    if len({c["id"] for c in concepts}) != len(concepts):
        raise ValueError("Concept ids must be unique")
    by_id = {c["id"]: c for c in concepts}
    routes = plan.get("routes")
    if not isinstance(routes, list) or not 1 <= len(routes) <= 18:
        raise ValueError("A plan requires 1..18 routes")
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
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Each route limit must be 1..1000")
        ids = item.get("concept_ids", [])
        if not isinstance(ids, list) or any(not isinstance(i, str) or i not in by_id for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Route concept_ids must refer to unique declared concepts")
        if item.get("query") and ids:
            raise ValueError("Use an explicit query or concept_ids, not both")
        if ids:
            if source not in NATIVE:
                raise ValueError("Concept compilation supports PubMed, Europe PMC and arXiv; provide text queries for other sources")
            def phrase(term):
                if source == "pubmed":
                    return f'"{term}"[Title/Abstract]'
                return f'{"TITLE_ABS" if source == "europe_pmc" else "all"}:"{term}"'
            query = " AND ".join("(" + " OR ".join(phrase(t) for t in by_id[i]["terms"]) + ")" for i in ids)
            syntax = "native"
        else:
            query = _text(item.get("query"), "query")
        if syntax == "native" and source not in NATIVE:
            raise ValueError("Native syntax is not supported for this source")
        compiled.append({"id": route_id, "source": source, "query": query, "syntax": syntax, "limit": limit})
    if len({r["id"] for r in compiled}) != len(compiled) or sum(r["limit"] for r in compiled) > 10000:
        raise ValueError("Route ids must be unique and total candidate budget <=10000")
    normalized = {"schema_version": 1, "question": question, "concepts": concepts, "routes": compiled}
    if plan.get("scope_note"):
        normalized["scope_note"] = _text(plan["scope_note"], "scope note")
    return normalized


def digest_json(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def candidate_id(record):
    # Include all strong IDs: records held apart for an ID conflict must not share a queue ID.
    return digest_json({"ids": record.identity_keys, "year": record.year, "authors": record.authors})[:24]


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
