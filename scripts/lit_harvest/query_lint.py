"""Conservative syntax diagnostics, not a clinical search-validity certificate."""
import re

VERSION = "query-lint/2"
FIELDS = {
    "pubmed": {"ti", "ab", "tiab", "title", "abstract", "title/abstract", "mh", "mesh", "mesh terms", "majr", "mesh major topic", "pt", "publication type", "au", "author", "ad", "affiliation", "dp", "date - publication", "pdat", "edat", "uid", "pmid", "all fields", "tw", "text word", "la", "language", "jour", "journal", "issn", "sb", "filter"},
    "europe_pmc": {"TITLE", "ABSTRACT", "TITLE_ABS", "AUTH_FIRST", "AUTH_LAST", "AUTH_MANUSCRIPT", "AUTHORID", "AUTH_ORCID", "AUTHOR", "JOURNAL", "EXT_ID", "SRC", "DOI", "PMCID", "PUB_YEAR", "FIRST_PDATE", "CREATION_DATE", "MESH", "PUB_TYPE", "OPEN_ACCESS", "HAS_FT", "IN_PMC"},
    "arxiv": {"ti", "au", "abs", "co", "jr", "cat", "rn", "id", "all", "submittedDate", "lastUpdatedDate"},
}


def inspect_query(query, source, syntax="native"):
    errors, warnings = [], []
    result = {"checker": VERSION, "source": source, "query": query, "errors": errors,
              "warnings": warnings, "scientific_completeness_verified": False}
    if syntax != "native":
        for match in re.finditer(r"\[[^\[\]]+\]|\b(?:TITLE|ABSTRACT|TITLE_ABS|MESH|PUB_YEAR|ti|abs|cat):", query):
            warnings.append({"code": "native_syntax_in_text_route", "offset": match.start(),
                             "detail": "Database field syntax may not transfer to this text route; query is unchanged"})
        result["status"] = "text_not_boolean_validated"
        return result
    stack, quote, escaped = [], None, False
    visible = list(query)
    for i, ch in enumerate(query):
        if escaped:
            visible[i] = " "
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            quote = None if quote is not None else i
            visible[i] = " "
            continue
        if quote is not None:
            visible[i] = " "
            continue
        if ch in "([":
            stack.append((ch, i))
        elif ch in ")]":
            if not stack or stack[-1][0] != (")" == ch and "(" or "["):
                errors.append({"code": "unmatched_closing_delimiter", "offset": i})
            else:
                stack.pop()
    if quote is not None:
        errors.append({"code": "unclosed_quote", "offset": quote})
    errors.extend({"code": "unclosed_delimiter", "offset": i} for _, i in stack)
    plain = "".join(visible)
    if re.search(r"\bAND\b", plain) and re.search(r"\bOR\b", plain) and "(" not in plain:
        warnings.append({"code": "mixed_boolean_precedence_requires_review", "offset": 0})
    # Unknown fields may be legitimate provider extensions: warn, never rewrite.
    pattern = r"\[([^\[\]]+)\]" if source == "pubmed" else r"\b([A-Za-z_]+):"
    for match in re.finditer(pattern, plain):
        field = match.group(1).strip()
        key = field.casefold().split(":")[0] if source == "pubmed" else field
        if key not in FIELDS.get(source, set()):
            warnings.append({"code": "field_requires_provider_verification", "field": field, "offset": match.start()})
    for match in re.finditer(r"\b(AND|OR|NOT|ANDNOT)\s*$", plain):
        # Quoted trailing terms were masked; examine original suffix before flagging.
        if not query[match.start()+len(match.group(1)):].strip():
            errors.append({"code": "trailing_boolean_operator", "offset": match.start()})
    result["status"] = "error" if errors else "warnings" if warnings else "checked_subset"
    return result


def inspect_plan(plan):
    return {"schema_version": 1, "checker": VERSION, "routes": [
        {"route_id": r["id"], **inspect_query(r["query"], r["source"], r["syntax"])} for r in plan["routes"]],
        "mesh_evidence": [{"concept_id": c["id"], **h, "verified_by_checker": False}
                          for c in plan["concepts"] for h in c.get("mesh_terms", [])],
        "scientific_completeness_verified": False}
