"""Non-destructive work/version families, separate from record and file identity.

``families`` is an optional metadata aggregation view, not a replacement record
population. ``confirmed_layer`` contains only accepted provider version edges;
``families`` additionally permits explicitly labelled high-confidence metadata
groups. Neither layer licenses reuse of a DOI, candidate ID, PDF or screening
decision. All inputs survive in ``members`` and one family in each partition.

Provider declarations are accepted from the version_relations adapter contract,
not from arbitrary ``confirmed`` labels. No host-adjudication API is implemented.
The function is pure, deterministic, local-only and does not access source files.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from .models import PaperRecord, normalize_doi, normalize_pmid, normalize_pmcid, normalize_arxiv_id, normalize_title, normalize_work_alias
from .query_plan import candidate_id


SCHEMA_VERSION = 1
ALGORITHM_VERSION = "work-families-0.8.0-v1"
VERSION_RELATIONS = frozenset({"is-preprint-of", "has-preprint", "is-version-of", "has-version", "is-manuscript-of", "has-manuscript"})
NON_VERSION_RELATIONS = frozenset({
    "is-translation-of", "has-translation", "is-comment-on", "has-comment",
    "is-reply-to", "has-reply", "is-review-of", "has-review", "is-correction-of",
    "has-correction", "is-retraction-of", "has-retraction", "updates", "is-update-of",
    "is-supplement-to", "is-supplemented-by", "is-part-of", "has-part",
})
SOURCE_HOSTS = {"crossref": {"api.crossref.org", "www.crossref.org", "crossref.org"},
                "europe_pmc": {"www.ebi.ac.uk", "www.europepmc.org", "europepmc.org"}}
RESTRICTED_TYPES = frozenset({"comment", "letter", "reply", "author reply", "editorial", "correction", "erratum", "retraction", "retraction of publication", "retracted publication", "published erratum", "translation", "preprint withdrawal", "preprint removal"})
STOP_WORDS = frozenset("a an and are as at be been by for from had has have in is it its of on or our that the their these this to was were which with we study studies background objective objectives methods results conclusion conclusions abstract".split())


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _relation_type(value: Any) -> str:
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", "-", str(value or ""))
    return re.sub(r"[-_\s]+", "-", text).strip("-").casefold()


def _alias(kind: Any, value: Any) -> str:
    kind = str(kind or "").casefold().replace("-", "_")
    value = str(value or "").strip()
    if kind == "doi":
        value = normalize_doi(value)
    elif kind == "pmid":
        value = normalize_pmid(value)
    elif kind == "pmcid":
        value = normalize_pmcid(value)
    elif kind == "arxiv":
        value = normalize_arxiv_id(value)
    elif kind == "europe_pmc":
        return "europe_pmc:" + value.casefold() if re.fullmatch(r"[A-Za-z]+:[A-Za-z0-9]+", value) else ""
    elif kind not in {"openalex", "s2", "semantic_scholar"}:
        return ""
    return normalize_work_alias(f"{kind}:{value}") if value else ""


def _source_alias(value: Any) -> str:
    text = str(value or "")
    kind, sep, tail = text.partition(":")
    return _alias(kind, tail) if sep else ""


def _url(value: str):
    try:
        return urlparse(value)
    except ValueError:
        return urlparse("")


def _role(record: PaperRecord) -> tuple[str, list[str]]:
    raw_types = record.extra.get("publication_types") or []
    if not isinstance(raw_types, (list, tuple)):
        raw_types = [raw_types]
    types = {normalize_title(str(t)) for t in raw_types}
    types.add(normalize_title(record.item_type))
    restricted = sorted(types & RESTRICTED_TYPES)
    title = record.normalized_title
    if re.match(r"^(?:correction|corrigendum|erratum|retraction|retracted|reply|comment on|response to|author s reply|translation of)(?:\s|$)", title):
        restricted.append("notice_or_reply_title")
    if restricted:
        return "separate_related_work", sorted(set(restricted))
    if "preprint" in types or normalize_title(record.item_type) in {"preprint", "posted content"}:
        return "preprint", []
    if normalize_title(record.item_type) in {"journalarticle", "journal article", "research article"} or "journal article" in types:
        return "journal", []
    return "unknown", []


@dataclass(frozen=True)
class _Member:
    cid: str
    record: PaperRecord
    aliases: frozenset[str]
    role: str
    restrictions: tuple[str, ...]
    metadata_sha256: str


def _member(value: PaperRecord | Mapping[str, Any]) -> _Member:
    # Round-trip through a fresh object: PaperRecord.__post_init__ and downstream
    # code cannot mutate the caller's nested extra/lists.
    data = value.to_dict() if isinstance(value, PaperRecord) else dict(value)
    data = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
    record = PaperRecord.from_dict(data)
    stored = record.extra.get("candidate_id")
    if stored is not None and (not isinstance(stored, str) or not stored.strip()):
        raise ValueError("extra.candidate_id must be a nonempty string when supplied")
    cid = stored if stored is not None else candidate_id(record)
    aliases = {normalize_work_alias(k) for k in record.identity_keys if not k.startswith("title:")}
    parsed = _url(record.url)
    if (parsed.hostname or "").casefold() in SOURCE_HOSTS["europe_pmc"]:
        match = re.fullmatch(r"/article/([A-Za-z]+)/([A-Za-z0-9]+)/?", parsed.path)
        if match:
            aliases.add(_alias("europe_pmc", ":".join(match.groups())))
    extra_aliases = record.extra.get("source_identifiers") or []
    if isinstance(extra_aliases, (list, tuple)):
        aliases.update(a for v in extra_aliases if (a := _source_alias(v)))
    role, restrictions = _role(record)
    return _Member(cid, record, frozenset(aliases), role, tuple(restrictions), _digest(data))


def _author_keys(name: str) -> set[tuple[str, str]]:
    # Both common bibliography orders are considered, without declaring a person
    # identity. Full initial strings only need a compatible first initial.
    words = normalize_title(name).split()
    if len(words) < 2:
        return set()
    if len(words[-1]) <= 3:
        return {(" ".join(words[:-1]), words[-1][0])}
    if len(words[0]) <= 3:
        return {(" ".join(words[1:]), words[0][0])}
    return {(words[-1], words[0][0]), (words[0], words[-1][0])}


def _authors(a: _Member, b: _Member) -> dict[str, Any]:
    left = [_author_keys(v) for v in a.record.authors]
    right = [_author_keys(v) for v in b.record.authors]
    # Bipartite matching prevents one repeated surname from matching every author.
    matched: dict[int, int] = {}
    def visit(i: int, seen: set[int]) -> bool:
        for j, keys in enumerate(right):
            if j in seen or not left[i].intersection(keys):
                continue
            seen.add(j)
            if j not in matched or visit(matched[j], seen):
                matched[j] = i
                return True
        return False
    overlap = sum(visit(i, set()) for i in range(len(left)))
    minimum = min(len(left), len(right))
    first = bool(left and right and left[0].intersection(right[0]))
    compatible = bool(minimum and overlap / minimum >= .6 and (overlap >= 2 or minimum == 1 and first))
    return {"matched_authors": overlap, "shorter_author_count": minimum, "first_author_compatible": first,
            "compatible": compatible, "known_disjoint": bool(left and right and not overlap)}


def _shingles(words: list[str], length: int) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + length]) for i in range(len(words) - length + 1)}


def _abstracts(a: _Member, b: _Member) -> dict[str, Any]:
    aa, bb = (normalize_title(m.record.abstract).split() for m in (a, b))
    sa, sb = set(aa), set(bb)
    five_a, five_b = _shingles(aa, 5), _shingles(bb, 5)
    shared = five_a & five_b
    dice = 2 * len(shared) / (len(five_a) + len(five_b)) if five_a or five_b else 0.0
    overlap = len(sa & sb) / min(len(sa), len(sb)) if sa and sb else 0.0
    distinctive = {w for sh in shared for w in sh if w not in STOP_WORDS and (len(w) >= 5 or w.isdigit())}
    numbers_a = {w for w in sa if w.isdigit()}
    numbers_b = {w for w in sb if w.isdigit()}
    numeric_agreement = len(numbers_a & numbers_b) / len(numbers_a | numbers_b) if numbers_a or numbers_b else None
    def spread(words: list[str]) -> list[float]:
        regions = [set() for _ in range(3)]
        for i in range(max(0, len(words) - 4)):
            regions[min(2, i * 3 // max(1, len(words) - 4))].add(tuple(words[i:i + 5]))
        return [round(len(region & shared) / len(region), 6) if region else 0.0 for region in regions]
    spread_a, spread_b = spread(aa), spread(bb)
    # Long common prose must be spread across the abstract, not just one shared
    # introduction. This is conservative lexical identity evidence, not semantics.
    strong = (min(len(aa), len(bb)) >= 90 and min(len(sa), len(sb)) >= 50 and
              len(shared) >= 25 and len(distinctive) >= 25 and overlap >= .75 and dice >= .45 and
              min(spread_a + spread_b) >= .2 and
              (numeric_agreement is None or numeric_agreement >= .5))
    return {"word_counts": [len(aa), len(bb)], "shared_fivegrams": len(shared),
            "fivegram_dice": round(dice, 6), "token_containment": round(overlap, 6),
            "distinctive_shared_tokens": len(distinctive), "numeric_token_agreement": numeric_agreement,
            "shared_fivegram_coverage_by_third": [spread_a, spread_b],
            "strong": strong}


def _metadata(a: _Member, b: _Member) -> dict[str, Any]:
    title_equal = bool(a.record.normalized_title and a.record.normalized_title == b.record.normalized_title)
    title_tokens = set(a.record.normalized_title.split()) - STOP_WORDS
    title_specific = len(title_tokens) >= 6 and len(a.record.normalized_title) >= 45
    authors = _authors(a, b)
    abstracts = _abstracts(a, b)
    year_ok = bool(a.record.year is not None and b.record.year is not None and abs(a.record.year - b.record.year) <= 4)
    roles_ok = {a.role, b.role} == {"preprint", "journal"}
    chronology_ok = year_ok
    if roles_ok and year_ok:
        preprint, journal = (a, b) if a.role == "preprint" else (b, a)
        chronology_ok = preprint.record.year <= journal.record.year + 1
    reasons = []
    for ok, reason in ((not a.restrictions and not b.restrictions, "separate_related_work"),
                       (title_equal, "title_not_equal"), (title_specific, "generic_title"),
                       (authors["compatible"], "insufficient_author_evidence"),
                       (chronology_ok, "incompatible_or_missing_years"),
                       (roles_ok, "not_preprint_journal_pair"), (abstracts["strong"], "insufficient_distinctive_abstract")):
        if not ok:
            reasons.append(reason)
    return {"title_equal": title_equal, "title_specific": title_specific, "authors": authors,
            "abstracts": abstracts, "chronology_compatible": chronology_ok, "roles": [a.role, b.role],
            "reasons": reasons, "high_confidence": not reasons}


def _components(ids: list[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    neighbors: dict[str, set[str]] = {cid: set() for cid in ids}
    for a, b in edges:
        neighbors[a].add(b)
        neighbors[b].add(a)
    remaining = set(ids)
    result = []
    while remaining:
        todo = [min(remaining)]
        group: set[str] = set()
        while todo:
            item = todo.pop()
            if item in group:
                continue
            group.add(item)
            todo.extend(neighbors[item] - group)
        remaining -= group
        result.append(sorted(group))
    return result


def _conflicts(group: list[str], members: dict[str, _Member], negative: set[frozenset[str]],
               explicit: set[frozenset[str]], metadata_layer: bool) -> list[dict[str, Any]]:
    conflicts = []
    for i, cid in enumerate(group):
        a = members[cid]
        for other in group[i + 1:]:
            b = members[other]
            pair = frozenset((cid, other))
            reason = ""
            if pair in negative:
                reason = "explicit_non_version_relation"
            elif a.restrictions or b.restrictions:
                reason = "separate_related_work"
            elif pair not in explicit:
                evidence = _metadata(a, b)
                if a.role == b.role == "journal" and a.record.doi and b.record.doi and a.record.doi != b.record.doi:
                    reason = "multiple_unlinked_journal_versions"
                elif evidence["authors"]["known_disjoint"] and not evidence["title_equal"]:
                    reason = "incompatible_bridge_metadata"
                elif metadata_layer and not (evidence["title_equal"] and evidence["authors"]["compatible"] and
                                             evidence["abstracts"]["strong"] and evidence["chronology_compatible"]):
                    reason = "metadata_bridge_not_pairwise_supported"
            if reason:
                conflicts.append({"candidate_ids": sorted(pair), "reason": reason})
    return conflicts


def _family(group: list[str], members: dict[str, _Member], relation_ids: list[str], classification: str) -> dict[str, Any]:
    def representative_key(cid: str) -> tuple[Any, ...]:
        member = members[cid]
        r = member.record
        # A display preference only, never a citation/file replacement rule.
        return ({"journal": 0, "preprint": 1, "unknown": 2, "separate_related_work": 3}[member.role],
                -sum(bool(v) for v in (r.doi, r.pmid, r.pmcid, r.arxiv_id)), -len(r.abstract), cid)
    return {"family_id": "wf_" + _digest(group)[:24], "member_candidate_ids": group,
            "representative_candidate_id": min(group, key=representative_key), "classification": classification,
            "relation_ids": sorted(relation_ids), "member_count": len(group)}


def build_work_families(records: Iterable[PaperRecord | Mapping[str, Any]]) -> dict[str, Any]:
    """Return schema-v1 inventory without changing records, screening or files.

    An explicit edge requires the recognized provider, bound subject identifier,
    public provider evidence URL and 64-hex source metadata digest. That digest
    binds an adapter observation; this function does not independently refetch or
    authenticate a provider. Unknown/unbound/missing-target edges remain review.
    Any conflicting connected component is quarantined in its entirety rather
    than accepting an input-order-dependent spanning tree.
    """
    member_list = [_member(r) for r in records]
    members = {m.cid: m for m in member_list}
    if len(members) != len(member_list):
        raise ValueError("Duplicate candidate IDs: no record may be silently collapsed in a family inventory")
    ids = sorted(members)
    alias_map: dict[str, list[str]] = defaultdict(list)
    for cid in ids:
        for alias in sorted(members[cid].aliases):
            alias_map[alias].append(cid)
    relations_by_id: dict[str, dict[str, Any]] = {}
    negative: set[frozenset[str]] = set()
    for cid in ids:
        member = members[cid]
        rows = member.record.extra.get("version_relations") or []
        if not isinstance(rows, (list, tuple)):
            rows = [rows]
        for raw in rows:
            if not isinstance(raw, Mapping):
                raw = {"relation": "malformed", "raw_relation": raw}
            relation = _relation_type(raw.get("relation"))
            target = _alias(raw.get("target_id_type"), raw.get("target_id"))
            targets = alias_map.get(target, [])
            source = str(raw.get("source") or "").casefold()
            source_id = _source_alias(raw.get("source_record_id"))
            evidence_url = str(raw.get("evidence_url") or "")
            parsed_url = _url(evidence_url)
            metadata_hash = str(raw.get("source_metadata_sha256") or "").casefold()
            bound = bool(source in SOURCE_HOSTS and source_id in member.aliases and
                         parsed_url.scheme == "https" and (parsed_url.hostname or "").casefold() in SOURCE_HOSTS[source] and
                         not parsed_url.username and not parsed_url.password and not parsed_url.query and
                         re.fullmatch(r"[0-9a-f]{64}", metadata_hash))
            # EPMC query endpoints have public query parameters. Never copy them
            # into output; the adapter hash + current source ID retain provenance.
            if source == "europe_pmc" and parsed_url.query and not parsed_url.username and not parsed_url.password:
                bound = bool(source_id in member.aliases and parsed_url.scheme == "https" and
                             (parsed_url.hostname or "").casefold() in SOURCE_HOSTS[source] and
                             re.fullmatch(r"[0-9a-f]{64}", metadata_hash))
            # Only a credential-free public provider endpoint is emitted. Input
            # provenance mistakes must not copy credentials/query secrets out.
            evidence_url = (parsed_url._replace(query="", fragment="").geturl()
                            if source in SOURCE_HOSTS and not parsed_url.username and not parsed_url.password and
                            (parsed_url.hostname or "").casefold() in SOURCE_HOSTS[source] else "")
            metadata_hash = metadata_hash if re.fullmatch(r"[0-9a-f]{64}", metadata_hash) else ""
            edge = {"subject_candidate_id": cid, "object_candidate_id": targets[0] if len(targets) == 1 else None,
                    "target_alias": target, "relation": relation, "source": source,
                    "source_record_id": source_id, "evidence_url": evidence_url,
                    "source_metadata_sha256": metadata_hash,
                    "raw_relation_sha256": _digest(raw.get("raw_relation", {})), "basis": "provider_metadata"}
            reason = ""
            if len(targets) != 1:
                reason = "ambiguous_target" if targets else "target_not_in_inventory"
            elif targets[0] == cid:
                reason = "self_relation"
            elif relation not in VERSION_RELATIONS:
                reason = "non_version_relation" if relation in NON_VERSION_RELATIONS else "unsupported_relation_type"
            elif not bound:
                reason = "unbound_or_untrusted_provider_provenance"
            elif member.restrictions or members[targets[0]].restrictions:
                reason = "separate_related_work"
            else:
                target_member = members[targets[0]]
                subject_role, object_role = member.role, target_member.role
                if relation == "has-preprint" and (subject_role == "preprint" or object_role == "journal") or relation == "is-preprint-of" and (subject_role == "journal" or object_role == "preprint"):
                    reason = "contradictory_preprint_direction"
            edge.update(status="proposed" if not reason else "review", reason=reason)
            edge["relation_id"] = "wr_" + _digest(edge)[:24]
            relations_by_id[edge["relation_id"]] = edge
            if bound and len(targets) == 1 and targets[0] != cid and relation in NON_VERSION_RELATIONS:
                negative.add(frozenset((cid, targets[0])))
    relations = list(relations_by_id.values())
    proposed = [e for e in relations if e["status"] == "proposed"]
    explicit_pairs = {frozenset((e["subject_candidate_id"], e["object_candidate_id"])) for e in proposed}
    review = []
    provider_conflicted_ids: set[str] = set()
    def quarantine(edges: list[dict[str, Any]], metadata_layer: bool) -> None:
        pairs = [(e["subject_candidate_id"], e["object_candidate_id"]) for e in edges]
        for group in _components(ids, pairs):
            if len(group) < 2:
                continue
            conflicts = _conflicts(group, members, negative, explicit_pairs, metadata_layer)
            if conflicts:
                group_set = set(group)
                if not metadata_layer:
                    provider_conflicted_ids.update(group_set)
                for edge in edges:
                    if edge["subject_candidate_id"] in group_set and edge["object_candidate_id"] in group_set:
                        edge.update(status="blocked", reason="transitive_component_conflict")
                review.append({"candidate_ids": group, "reason": "transitive_component_conflict", "conflicts": conflicts})
    quarantine(proposed, False)
    for edge in proposed:
        if edge["status"] == "proposed":
            edge.update(status="accepted", reason="declared_version_relation")
    accepted = [e for e in relations if e["status"] == "accepted"]
    confirmed_components = _components(ids, [(e["subject_candidate_id"], e["object_candidate_id"]) for e in accepted])
    confirmed_group_for = {cid: tuple(group) for group in confirmed_components for cid in group}
    # Title is a candidate generator, never a merge key. Only the fully checked
    # evidence below can create the separate metadata layer.
    titles: dict[str, list[str]] = defaultdict(list)
    for cid in ids:
        if members[cid].record.normalized_title:
            titles[members[cid].record.normalized_title].append(cid)
    metadata_edges = []
    for group in titles.values():
        for i, cid in enumerate(group):
            for other in group[i + 1:]:
                if confirmed_group_for[cid] == confirmed_group_for[other]:
                    continue
                evidence = _metadata(members[cid], members[other])
                conflicted = cid in provider_conflicted_ids or other in provider_conflicted_ids
                if evidence["high_confidence"] and frozenset((cid, other)) not in negative and not conflicted:
                    edge = {"subject_candidate_id": cid, "object_candidate_id": other,
                            "relation": "possible-version-of", "source": "local_metadata", "basis": "high_confidence_metadata",
                            "status": "proposed", "reason": "", "evidence": evidence,
                            "source_record_sha256": [members[cid].metadata_sha256, members[other].metadata_sha256]}
                    edge["relation_id"] = "wr_" + _digest(edge)[:24]
                    metadata_edges.append(edge)
                else:
                    review.append({"candidate_ids": [cid, other], "reason": "conflicting_provider_component" if conflicted else "same_title_insufficient_version_evidence",
                                   "evidence": evidence})
    # A metadata bridge may not weaken a confirmed component's protections.
    quarantine(accepted + metadata_edges, True)
    for edge in metadata_edges:
        if edge["status"] == "proposed":
            edge.update(status="accepted", reason="high_confidence_metadata_not_external_confirmation")
    relations.extend(metadata_edges)
    # Quarantining a metadata bridge must not revoke already confirmed edges.
    for edge in accepted:
        if edge["status"] == "blocked":
            edge.update(status="accepted", reason="declared_version_relation")
    combined = [e for e in relations if e["status"] == "accepted"]
    effective_components = _components(ids, [(e["subject_candidate_id"], e["object_candidate_id"]) for e in combined])
    def inventory(groups: list[list[str]], layer_edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for group in groups:
            group_set = set(group)
            used = [e for e in layer_edges if e["subject_candidate_id"] in group_set and e["object_candidate_id"] in group_set]
            classification = ("high_confidence_metadata" if any(e["basis"] == "high_confidence_metadata" for e in used) else
                              "confirmed_relation" if len(group) > 1 else "singleton")
            result.append(_family(group, members, [e["relation_id"] for e in used], classification))
        return sorted(result, key=lambda f: f["family_id"])
    confirmed_inventory = inventory(confirmed_components, accepted)
    families = inventory(effective_components, combined)
    for edge in relations:
        if edge["status"] != "accepted" and edge["reason"] != "self_relation":
            review.append({"candidate_ids": sorted({v for v in (edge["subject_candidate_id"], edge["object_candidate_id"]) if v}),
                           "reason": edge["reason"], "relation_ids": [edge["relation_id"]]})
    review_map = {_digest(item): item for item in review}
    return {"schema_version": SCHEMA_VERSION, "algorithm_version": ALGORITHM_VERSION,
            "counting_unit": "bibliographic_work_version_family_not_independent_study",
            "record_population_unchanged": True, "pdf_identity_shared": False,
            "members": [{"candidate_id": cid, "candidate_id_origin": "stored" if "candidate_id" in members[cid].record.extra else "derived",
                         "source_record_sha256": members[cid].metadata_sha256, "identifiers": sorted(members[cid].aliases),
                         "publication_role": members[cid].role, "restrictions": list(members[cid].restrictions)} for cid in ids],
            "confirmed_layer": confirmed_inventory, "families": families,
            "relations": sorted(relations, key=lambda e: e["relation_id"]),
            "review_candidates": [review_map[k] for k in sorted(review_map)],
            "counts": {"record_count": len(ids), "confirmed_layer_family_count": len(confirmed_inventory),
                       "work_family_count": len(families),
                       "confirmed_relation_families": sum(f["classification"] == "confirmed_relation" for f in confirmed_inventory),
                       "high_confidence_metadata_families": sum(f["classification"] == "high_confidence_metadata" for f in families),
                       "singleton_families": sum(f["classification"] == "singleton" for f in families),
                       "accepted_provider_relations": sum(e["status"] == "accepted" and e["basis"] == "provider_metadata" for e in relations),
                       "accepted_metadata_relations": sum(e["status"] == "accepted" and e["basis"] == "high_confidence_metadata" for e in relations),
                       "blocked_relations": sum(e["status"] == "blocked" for e in relations),
                       "review_candidates": len(review_map)}}
