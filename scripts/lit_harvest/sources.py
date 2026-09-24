from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import html
import re
import threading
import time
from typing import Any
from urllib.parse import quote, urlencode, urlparse
from xml.etree import ElementTree

from .http import HttpClient, HttpError, is_public_https_url
from .models import PaperRecord, normalize_doi, normalize_title, normalize_pmcid, normalize_arxiv_id


PMID_RE = re.compile(r"^(?:pmid:\s*)?(\d{5,9})$", re.I)
PMCID_RE = re.compile(r"^(?:pmcid:\s*)?(PMC\d+)$", re.I)


# Local run bounds are separate from provider page bounds. OpenAlex's supported
# maximum is 100 (200 is deprecated); S2 relevance search exposes only 1,000 hits.
MAX_SEARCH_RESULTS = 10_000
PAGE_SIZES = {"pubmed": 500, "europe_pmc": 1000, "crossref": 1000,
              "openalex": 100, "semantic_scholar": 100, "arxiv": 1000}


class _ProviderPacer:
    """Serialize a provider's calls in this process, including response reads.

    The gap starts after completion, so slow requests never cause overlapping
    connections. Separate processes/machines must still coordinate their use.
    Transport-internal retries must independently respect provider intervals.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_completed: float | None = None

    @contextmanager
    def slot(self, interval: float):
        with self._lock:
            if self._last_completed is not None:
                delay = interval - (time.monotonic() - self._last_completed)
                if delay > 0:
                    time.sleep(delay)
            try:
                yield
            finally:
                self._last_completed = time.monotonic()


_PROVIDER_PACERS = {"pubmed": _ProviderPacer(), "arxiv": _ProviderPacer()}


class SearchResults(list):
    """A list-compatible result carrying partial-page failures to search_all."""

    def __init__(self, records=(), source_status: dict[str, Any] | None = None):
        super().__init__(records)
        self.source_status = source_status or {}


def _search_status(limit: int, source: str) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise ValueError(f"limit must be an integer in 1..{MAX_SEARCH_RESULTS}")
    effective = min(limit, 1000) if source == "semantic_scholar" else limit
    status: dict[str, Any] = {
        "status": "ok", "requested_limit": limit, "effective_limit": effective,
        "pages": 0, "errors": [], "exhausted": False,
    }
    if effective != limit:
        status["limit_reason"] = "Semantic Scholar relevance search exposes at most 1000 results"
    return status


def _page_error(status: dict[str, Any], exc: Exception, count: int, continuation: Any) -> None:
    status["status"] = "partial" if count else "error"
    status["errors"].append(str(exc)[:500])
    status["continuation"] = continuation
    if isinstance(exc, HttpError):
        status["http_status"] = exc.status
        for field in ("retry_after", "retry_at"):
            value = getattr(exc, field, None)
            if value is not None:
                status[field] = value


def _value_at(payload: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def _json_pages(
    client: HttpClient, url: str, params: dict[str, Any], limit: int, source: str,
    *, items_path: tuple[str, ...], size_key: str, token_key: str,
    next_path: tuple[str, ...] | None = None, headers: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    status = _search_status(limit, source)
    cap = status["effective_limit"]
    items: list[dict[str, Any]] = []
    token: Any = "*" if next_path else 0
    used_tokens: set[str] = set()
    while len(items) < cap:
        if str(token) in used_tokens:
            _page_error(status, ValueError("Provider repeated pagination token"), len(items), token)
            break
        used_tokens.add(str(token))
        size = min(PAGE_SIZES[source], cap - len(items))
        page_params = {**params, size_key: size, token_key: token}
        try:
            if headers is None:
                payload = client.get_json(url, page_params)
            else:
                payload = client.get_json(url, page_params, headers=headers)
            batch = _value_at(payload, items_path)
            if not isinstance(batch, list):
                raise ValueError(f"{source} returned no result array")
        except Exception as exc:
            _page_error(status, exc, len(items), token)
            break
        status["pages"] += 1
        items.extend(item for item in batch[:cap - len(items)] if isinstance(item, dict))
        if not batch:
            status["exhausted"] = True
            break
        if next_path:
            next_token = _value_at(payload, next_path)
            if next_token in (None, ""):
                status["exhausted"] = True
                break
            # Crossref can issue a cursor even on its final short page.
            if source == "crossref" and len(batch) < size:
                status["exhausted"] = True
                break
            token = next_token
        else:
            next_token = payload.get("next") if source == "semantic_scholar" else None
            if source == "semantic_scholar":
                if next_token is None:
                    status["exhausted"] = True
                    break
                if not isinstance(next_token, int) or next_token <= token:
                    _page_error(status, ValueError("Invalid provider pagination offset"), len(items), next_token)
                    break
                token = next_token
            else:
                token += len(batch)
                if len(batch) < size:
                    status["exhausted"] = True
                    break
    return items, status


def _finish_search(records: list[PaperRecord], status: dict[str, Any]) -> SearchResults:
    for rank, record in enumerate(records):
        record.relevance_score = _score(rank, status["effective_limit"])
    status["count"] = len(records)
    return SearchResults(records, status)


def _identifier_tail(value: Any) -> str:
    return str(value).strip().rstrip("/").rsplit("/", 1)[-1] if value not in (None, "") else ""


def _pmid(value: Any) -> str:
    tail = _identifier_tail(value)
    return tail if re.fullmatch(r"\d{1,9}", tail) else ""


def _add_pmc_cloud(record: PaperRecord) -> None:
    pmcid = normalize_pmcid(record.pmcid)
    if not pmcid:
        return
    # This is a resolver request, not an OA assertion. The downloader must check
    # current cloud metadata (active OA, license and declared PDF) before fetching.
    record.add_pdf_candidate(
        "https://pmc-oa-opendata.s3.amazonaws.com/?"
        + urlencode({"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"}),
        "pmc_oa_cloud", record.license, "publishedVersion",
    )


def _publication_types(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(str(value).strip().casefold() for value in (values or []) if value))



def detect_identifier(query: str) -> tuple[str, str]:
    query = query.strip()
    doi = normalize_doi(query)
    if doi and (query.lower().startswith(("10.", "doi:", "http://", "https://"))):
        return "doi", doi
    match = PMCID_RE.match(query)
    if match:
        return "pmcid", match.group(1).upper()
    match = PMID_RE.match(query)
    if match:
        return "pmid", match.group(1)
    arxiv_id = normalize_arxiv_id(query)
    if arxiv_id:
        return "arxiv", arxiv_id
    return "text", query


def _strip_tags(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def _year_from_parts(value: Any) -> int | None:
    try:
        year = int(value[0][0])
        return year if 1500 <= year <= datetime.now().year + 2 else None
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _score(rank: int, limit: int) -> float:
    return round(max(0.0, 1.0 - rank / max(limit, 1)), 5)


def _element_text(node: ElementTree.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _parse_pubmed_xml(raw: bytes, limit: int) -> list[PaperRecord]:
    root = ElementTree.fromstring(raw)
    records: list[PaperRecord] = []
    for rank, item in enumerate(root.findall(".//PubmedArticle")):
        if len(records) >= limit:
            break
        citation = item.find("MedlineCitation")
        article = citation.find("Article") if citation is not None else None
        if citation is None or article is None:
            continue
        title = _element_text(article.find("ArticleTitle"))
        if not title:
            continue
        authors: list[str] = []
        for author in article.findall("./AuthorList/Author"):
            collective = _element_text(author.find("CollectiveName"))
            name = collective or " ".join(
                value
                for value in (
                    _element_text(author.find("ForeName")),
                    _element_text(author.find("LastName")),
                )
                if value
            )
            if name:
                authors.append(name)
        abstract_parts: list[str] = []
        for node in article.findall("./Abstract/AbstractText"):
            text = _element_text(node)
            label = (node.attrib.get("Label") or "").strip()
            if text:
                abstract_parts.append(f"{label}: {text}" if label else text)

        pub_date = article.find("./Journal/JournalIssue/PubDate")
        year_text = _element_text(pub_date.find("Year")) if pub_date is not None else ""
        if not year_text and pub_date is not None:
            year_text = _element_text(pub_date.find("MedlineDate"))
        if not year_text:
            year_text = _element_text(article.find("./ArticleDate/Year"))
        year_match = re.search(r"\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b", year_text)

        identifiers: dict[str, str] = {}
        for node in item.findall("./PubmedData/ArticleIdList/ArticleId"):
            id_type = (node.attrib.get("IdType") or "").casefold()
            value = _element_text(node)
            if id_type and value:
                identifiers[id_type] = value
        for node in article.findall("ELocationID"):
            if (node.attrib.get("EIdType") or "").casefold() == "doi":
                identifiers.setdefault("doi", _element_text(node))

        publication_types = [
            _element_text(node).casefold()
            for node in article.findall("./PublicationTypeList/PublicationType")
        ]
        pmid = _element_text(citation.find("PMID"))
        record = PaperRecord(
            title=title,
            authors=authors,
            year=int(year_match.group(0)) if year_match else None,
            journal=_element_text(article.find("./Journal/Title")),
            abstract="\n".join(abstract_parts),
            doi=identifiers.get("doi", ""),
            pmid=pmid,
            pmcid=identifiers.get("pmc", ""),
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
            item_type="preprint" if any("preprint" in value for value in publication_types) else "journalArticle",
            sources=["pubmed"],
            relevance_score=_score(rank, limit),
            extra={
                "volume": _element_text(article.find("./Journal/JournalIssue/Volume")),
                "issue": _element_text(article.find("./Journal/JournalIssue/Issue")),
                "pages": _element_text(article.find("Pagination/MedlinePgn")),
                "publication_types": publication_types,
            },
        )
        _add_pmc_cloud(record)
        records.append(record)
    return records


def search_pubmed(
    client: HttpClient, query: str, limit: int, contact_email: str = "", api_key: str = "",
) -> list[PaperRecord]:
    status = _search_status(limit, "pubmed")
    pacer = _PROVIDER_PACERS["pubmed"]
    interval = 0.11 if api_key else 0.34
    kind, value = detect_identifier(query)
    common: dict[str, Any] = {"db": "pubmed", "tool": "literature_harvester"}
    if contact_email:
        common["email"] = contact_email
    if api_key:
        common["api_key"] = api_key
    ids: list[str] = []
    if kind == "pmid":
        ids = [value]
    else:
        term = {"doi": f'"{value}"[AID]', "pmcid": f'"{value}"[PMCID]'}.get(kind, value)
        start = 0
        while len(ids) < limit:
            size = min(PAGE_SIZES["pubmed"], limit - len(ids))
            try:
                with pacer.slot(interval):
                    payload = client.get_json(
                        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                        {**common, "term": term, "retmax": size, "retstart": start,
                         "retmode": "json", "sort": "relevance"},
                    )
                result = payload.get("esearchresult") or {}
                status.setdefault("query_trace", []).append({
                    "retstart": start, "retmax": size, "submitted_query": term,
                    **{key: result[key] for key in ("querytranslation", "translationstack", "warninglist", "errorlist", "count") if key in result},
                })
                if not isinstance(result.get("idlist"), list):
                    raise ValueError("PubMed returned no idlist")
                batch = [_pmid(item) for item in result["idlist"] if _pmid(item)]
            except Exception as exc:
                _page_error(status, exc, len(ids), start)
                break
            status["pages"] += 1
            new_ids = [item for item in batch if item not in ids]
            ids.extend(new_ids[:limit - len(ids)])
            if not batch or len(batch) < size:
                status["exhausted"] = True
                break
            if not new_ids:
                _page_error(status, ValueError("PubMed repeated a page"), len(ids), start)
                break
            start += len(batch)
            if str(result.get("count", "")).isdigit() and start >= int(result["count"]):
                status["exhausted"] = True
                break
    records: list[PaperRecord] = []
    for start in range(0, len(ids), 100):
        params = {**common, "id": ",".join(ids[start:start + 100]), "retmode": "xml"}
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urlencode(params)
        try:
            with pacer.slot(interval):
                with client.request(url, headers={"Accept": "application/xml"}) as response:
                    records.extend(_parse_pubmed_xml(response.read(), limit - len(records)))
        except Exception as exc:
            _page_error(status, exc, len(records), {"efetch_start": start})
            break
    if status["errors"]:
        status["status"] = "partial" if records else "error"
    return _finish_search(records, status)


def search_crossref(client: HttpClient, query: str, limit: int, contact_email: str = "") -> list[PaperRecord]:
    status = _search_status(limit, "crossref")
    kind, value = detect_identifier(query)
    if kind == "doi":
        payload = client.get_json(f"https://api.crossref.org/works/{quote(value, safe='')}")
        items = [payload.get("message", {})]
    else:
        params: dict[str, Any] = {"query.bibliographic": query}
        if contact_email:
            params["mailto"] = contact_email
        items, status = _json_pages(
            client, "https://api.crossref.org/works", params, limit, "crossref",
            items_path=("message", "items"), size_key="rows", token_key="cursor",
            next_path=("message", "next-cursor"),
        )
    records: list[PaperRecord] = []
    for rank, item in enumerate(items):
        title_values = item.get("title") or []
        title = _strip_tags(title_values[0] if title_values else "")
        if not title:
            continue
        authors = []
        for author in item.get("author") or []:
            name = " ".join(part for part in (author.get("given", ""), author.get("family", "")) if part)
            if name:
                authors.append(name)
        containers = item.get("container-title") or []
        licenses = [entry.get("URL", "") for entry in (item.get("license") or []) if entry.get("URL")]
        license_url = licenses[0] if licenses else ""
        verified_oa_license = any("creativecommons.org/" in value.lower() for value in licenses)
        record = PaperRecord(
            title=title,
            authors=authors,
            year=_year_from_parts((item.get("issued") or {}).get("date-parts")),
            journal=containers[0] if containers else "",
            abstract=_strip_tags(item.get("abstract")),
            doi=item.get("DOI", ""),
            url=item.get("URL", ""),
            item_type="journalArticle",
            citation_count=item.get("is-referenced-by-count"),
            is_oa=True if verified_oa_license else None,
            license=license_url,
            sources=["crossref"],
            relevance_score=_score(rank, limit),
            extra={
                "volume": item.get("volume", ""),
                "issue": item.get("issue", ""),
                "pages": item.get("page", ""),
                "publisher": item.get("publisher", ""),
                "publication_types": _publication_types(item.get("type")),
            },
        )
        if verified_oa_license:
            for link in item.get("link") or []:
                if link.get("content-type") == "application/pdf":
                    record.add_pdf_candidate(
                        link.get("URL", ""), "crossref_verified_oa", license_url, "publishedVersion"
                    )
        records.append(record)
    return _finish_search(records, status)


def search_europe_pmc(client: HttpClient, query: str, limit: int) -> list[PaperRecord]:
    kind, value = detect_identifier(query)
    epmc_query = {
        "doi": f'DOI:"{value}"',
        "pmid": f"EXT_ID:{value} AND SRC:MED",
        "pmcid": f"P_PMCID:{value}",
    }.get(kind, query)
    items, status = _json_pages(
        client, "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        {"query": epmc_query, "format": "json", "resultType": "core"}, limit, "europe_pmc",
        items_path=("resultList", "result"), size_key="pageSize", token_key="cursorMark",
        next_path=("nextCursorMark",),
    )
    records: list[PaperRecord] = []
    for rank, item in enumerate(items):
        title = _strip_tags(item.get("title"))
        if not title:
            continue
        authors = []
        for author in (item.get("authorList") or {}).get("author", []):
            name = author.get("fullName") or " ".join(
                part for part in (author.get("firstName", ""), author.get("lastName", "")) if part
            )
            if name:
                authors.append(name)
        pmcid = normalize_pmcid(item.get("pmcid"))
        is_oa = str(item.get("isOpenAccess", "")).upper() == "Y"
        record = PaperRecord(
            title=title,
            authors=authors,
            year=int(item["pubYear"]) if str(item.get("pubYear", "")).isdigit() else None,
            journal=item.get("journalTitle", ""),
            abstract=_strip_tags(item.get("abstractText")),
            doi=item.get("doi", ""),
            pmid=_pmid(item.get("pmid") or (item.get("id") if item.get("source") == "MED" else "")),
            pmcid=pmcid,
            url=f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', '')}",
            item_type="preprint" if item.get("source") == "PPR" else "journalArticle",
            citation_count=int(item["citedByCount"]) if str(item.get("citedByCount", "")).isdigit() else None,
            is_oa=is_oa,
            oa_status="open" if is_oa else "closed_or_unknown",
            license=item.get("license", ""),
            sources=["europe_pmc"],
            relevance_score=_score(rank, limit),
            extra={"publication_types": _publication_types((item.get("pubTypeList") or {}).get("pubType"))},
        )
        full_text_urls = (item.get("fullTextUrlList") or {}).get("fullTextUrl", [])
        for candidate in full_text_urls:
            if (str(candidate.get("documentStyle", "")).lower() == "pdf"
                    and (is_oa or str(candidate.get("availability", "")).lower() == "open access")):
                record.add_pdf_candidate(
                    candidate.get("url", ""), "europe_pmc", record.license, "publishedVersion"
                )
        if pmcid:
            _add_pmc_cloud(record)
        records.append(record)
    return _finish_search(records, status)


def _openalex_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        positions.extend((int(index), word) for index in indexes)
    return " ".join(word for _, word in sorted(positions))



def _safe_content_urls(value: Any, work_id: str) -> dict[str, str]:
    """Retain only explicitly declared, credential-free archive URLs for this work."""
    if not isinstance(value, dict) or not re.fullmatch(r"W\d+", work_id):
        return {}
    result: dict[str, str] = {}
    for field, suffix in (("pdf", ".pdf"), ("grobid_xml", ".grobid-xml")):
        url = value.get(field)
        if not isinstance(url, str):
            continue
        parsed = urlparse(url)
        if (parsed.scheme == "https" and parsed.hostname == "content.openalex.org"
                and parsed.path == f"/works/{work_id}{suffix}"
                and not parsed.query and not parsed.fragment and not parsed.username):
            result[field] = url
    return result


def openalex_content_policy(
    *, enabled: bool = False, free_only: bool = True, max_files: int = 0,
    api_key_present: bool = False,
) -> dict[str, Any]:
    """Fail closed until the service provides a verified free-only spending guard.

    GET /rate-limit and X-RateLimit-Remaining report usage, not an atomic
    reservation or a prohibition on charging prepaid funds. A local count cap
    cannot protect against another process using the same account in between.
    """
    if not enabled:
        return {"status": "disabled", "reason": "OpenAlex content delivery is disabled by default"}
    if not free_only:
        return {"status": "blocked", "reason": "Paid content delivery is outside this free-only workflow"}
    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files <= 0:
        return {"status": "blocked", "reason": "OpenAlex content requires an explicit positive max_files cap"}
    if not api_key_present:
        return {"status": "blocked", "reason": "OpenAlex content requires an API key"}
    return {
        "status": "blocked",
        "reason": (
            "OpenAlex content is metered. A verified account free-balance schema and "
            "server-enforced free-only reservation are unavailable in this adapter; "
            "a daily allowance or local max_files cap cannot guarantee prepaid funds "
            "remain untouched. No content request or download candidate was issued."
        ),
        "max_files": max_files, "free_only": True,
        "account_budget_verified": False, "server_spend_guard_verified": False,
    }


def enrich_openalex_content(
    client: HttpClient, records: list[PaperRecord], api_key: str = "", *,
    enabled: bool = False, free_only: bool = True, max_files: int = 0,
) -> tuple[int, int, list[str]]:
    """Record per-work eligibility without issuing a potentially metered request."""
    policy = openalex_content_policy(
        enabled=enabled, free_only=free_only, max_files=max_files,
        api_key_present=bool(api_key),
    )
    for record in records:
        has_content = record.extra.get("openalex_has_content") or {}
        urls = _safe_content_urls(record.extra.get("openalex_content_urls"), record.openalex_id)
        license_value = str(record.extra.get("openalex_content_license") or "").casefold()
        # Metadata CC0 does not license the article. Accept only a per-work CC
        # license and an explicit OA assertion as eligibility evidence.
        licensed = bool(re.fullmatch(r"cc-(?:by(?:-nc)?(?:-nd|-sa)?|0)", license_value))
        record.extra["openalex_content"] = {
            **policy,
            "declared_pdf": isinstance(has_content, dict) and has_content.get("pdf") is True and bool(urls.get("pdf")),
            "license_verified": licensed and record.is_oa is True,
        }
    errors = [policy["reason"]] if policy["status"] == "blocked" else []
    return 0, 0, errors


def search_openalex(
    client: HttpClient,
    query: str,
    limit: int,
    api_key: str = "",
    contact_email: str = "",
) -> list[PaperRecord]:
    kind, value = detect_identifier(query)
    params: dict[str, Any] = {}
    if kind == "doi":
        params["filter"] = f"doi:{value}"
    elif kind == "pmid":
        params["filter"] = f"pmid:{value}"
    elif kind == "pmcid":
        params["filter"] = f"pmcid:{value}"
    else:
        params["search"] = query
    if api_key:
        params["api_key"] = api_key
    if contact_email:
        params["mailto"] = contact_email
    items, status = _json_pages(
        client, "https://api.openalex.org/works", params, limit, "openalex",
        items_path=("results",), size_key="per_page", token_key="cursor",
        next_path=("meta", "next_cursor"),
    )
    records: list[PaperRecord] = []
    for rank, item in enumerate(items):
        title = item.get("display_name") or item.get("title") or ""
        if not title:
            continue
        authors = [
            authorship.get("author", {}).get("display_name", "")
            for authorship in (item.get("authorships") or [])
            if authorship.get("author", {}).get("display_name")
        ]
        best = item.get("best_oa_location") or {}
        primary = item.get("primary_location") or {}
        source = primary.get("source") or {}
        ids = item.get("ids") or {}
        open_access = item.get("open_access") or {}
        record = PaperRecord(
            title=title,
            authors=authors,
            year=item.get("publication_year"),
            journal=source.get("display_name", ""),
            abstract=_openalex_abstract(item.get("abstract_inverted_index")),
            doi=ids.get("doi") or item.get("doi") or "",
            pmid=_pmid(ids.get("pmid")),
            pmcid=normalize_pmcid(ids.get("pmcid")),
            openalex_id=_identifier_tail(item.get("id")),
            url=primary.get("landing_page_url") or item.get("id", ""),
            item_type="preprint" if item.get("type") == "preprint" else "journalArticle",
            citation_count=item.get("cited_by_count"),
            is_oa=open_access.get("is_oa"),
            oa_status=open_access.get("oa_status", ""),
            license=best.get("license", ""),
            sources=["openalex"],
            relevance_score=_score(rank, limit),
            extra={
                "publication_types": _publication_types(item.get("type")),
                "openalex_has_content": item.get("has_content") or {},
                "openalex_content_urls": _safe_content_urls(item.get("content_urls"), _identifier_tail(item.get("id"))),
                "openalex_content_license": best.get("license") or "",
            },
        )
        # A work usually has several OA locations (publisher, PMC, institutional
        # repository, preprint server). Taking only best_oa_location throws away
        # every fallback, so one dead link loses the record entirely.
        seen_locations: list[dict[str, Any]] = []
        for location in [best, primary, *(item.get("locations") or [])]:
            if isinstance(location, dict) and location:
                seen_locations.append(location)
        for location in seen_locations:
            if not location.get("is_oa"):
                continue
            license_value = location.get("license", "") or ""
            version = location.get("version", "") or ""
            pdf_url = location.get("pdf_url", "") or ""
            if pdf_url:
                record.add_pdf_candidate(pdf_url, "openalex", license_value, version)
            landing = location.get("landing_page_url", "") or ""
            if landing and landing != pdf_url:
                # No direct file, but the host declared OA. The landing page
                # usually advertises its own PDF via citation_pdf_url metadata.
                record.add_pdf_candidate(
                    landing, "openalex_landing", license_value, version, kind="landing"
                )
        _add_pmc_cloud(record)
        records.append(record)
    return _finish_search(records, status)


def search_semantic_scholar(
    client: HttpClient,
    query: str,
    limit: int,
    api_key: str = "",
) -> list[PaperRecord]:
    status = _search_status(limit, "semantic_scholar")
    fields = ",".join(
        [
            "paperId",
            "title",
            "authors",
            "year",
            "venue",
            "abstract",
            "citationCount",
            "externalIds",
            "openAccessPdf",
            "publicationTypes",
            "url",
        ]
    )
    headers = {"x-api-key": api_key} if api_key else None
    kind, value = detect_identifier(query)
    if kind in {"doi", "pmid", "arxiv"}:
        prefix = {"doi": "DOI", "pmid": "PMID", "arxiv": "ARXIV"}[kind]
        try:
            item = client.get_json(
                f"https://api.semanticscholar.org/graph/v1/paper/{prefix}:{quote(value, safe='')}",
                {"fields": fields},
                headers,
            )
            items = [item]
        except HttpError as exc:
            if exc.status == 404:
                items = []
            else:
                raise
    else:
        items, status = _json_pages(
            client, "https://api.semanticscholar.org/graph/v1/paper/search",
            {"query": query, "fields": fields}, limit, "semantic_scholar",
            items_path=("data",), size_key="limit", token_key="offset", headers=headers,
        )
    records: list[PaperRecord] = []
    for rank, item in enumerate(items):
        title = item.get("title", "")
        if not title:
            continue
        external = item.get("externalIds") or {}
        oa_pdf = item.get("openAccessPdf") or {}
        publication_types = item.get("publicationTypes") or []
        record = PaperRecord(
            title=title,
            authors=[author.get("name", "") for author in (item.get("authors") or []) if author.get("name")],
            year=item.get("year"),
            journal=item.get("venue", ""),
            abstract=item.get("abstract") or "",
            doi=external.get("DOI", ""),
            pmid=external.get("PubMed", ""),
            arxiv_id=external.get("ArXiv", ""),
            semantic_scholar_id=item.get("paperId", ""),
            url=item.get("url", ""),
            item_type="preprint" if "preprint" in _publication_types(publication_types) else "journalArticle",
            citation_count=item.get("citationCount"),
            is_oa=True if oa_pdf.get("url") else None,
            sources=["semantic_scholar"],
            relevance_score=_score(rank, limit),
            extra={"publication_types": _publication_types(publication_types)},
        )
        record.add_pdf_candidate(
            oa_pdf.get("url", ""), "semantic_scholar", oa_pdf.get("license", ""), ""
        )
        records.append(record)
    return _finish_search(records, status)


def search_arxiv(client: HttpClient, query: str, limit: int, *, native_query: bool = False) -> list[PaperRecord]:
    status = _search_status(limit, "arxiv")
    kind, value = detect_identifier(query)
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom",
          "opensearch": "http://a9.com/-/spec/opensearch/1.1/"}
    entries: list[ElementTree.Element] = []
    seen: set[str] = set()
    start = 0
    while len(entries) < limit:
        size = min(PAGE_SIZES["arxiv"], limit - len(entries))
        params = {"start": start, "max_results": size, "sortBy": "relevance", "sortOrder": "descending"}
        params["id_list" if kind == "arxiv" else "search_query"] = value if kind == "arxiv" else query if native_query else f'all:"{query}"'
        try:
            with _PROVIDER_PACERS["arxiv"].slot(3.0):
                with client.request(
                    "https://export.arxiv.org/api/query?" + urlencode(params),
                    headers={"Accept": "application/atom+xml"},
                ) as response:
                    root = ElementTree.fromstring(response.read())
            batch = root.findall("atom:entry", ns)
            if any(urlparse(entry.findtext("atom:id", "", ns)).hostname in {"arxiv.org", "export.arxiv.org"}
                   and urlparse(entry.findtext("atom:id", "", ns)).path == "/api/errors"
                   for entry in batch):
                raise ValueError("arXiv returned an API error entry")
        except Exception as exc:
            _page_error(status, exc, len(entries), start)
            break
        status["pages"] += 1
        new_entries = [entry for entry in batch if entry.findtext("atom:id", "", ns) not in seen]
        seen.update(entry.findtext("atom:id", "", ns) for entry in new_entries)
        entries.extend(new_entries[:limit - len(entries)])
        if kind == "arxiv" or not batch or len(batch) < size:
            status["exhausted"] = True
            break
        if not new_entries:
            _page_error(status, ValueError("arXiv repeated a page"), len(entries), start)
            break
        start += len(batch)
    records: list[PaperRecord] = []
    for rank, entry in enumerate(entries):
        title = " ".join((entry.findtext("atom:title", default="", namespaces=ns)).split())
        entry_url = entry.findtext("atom:id", default="", namespaces=ns)
        arxiv_id = re.sub(r"^https?://(?:export\.)?arxiv\.org/abs/", "", entry_url).strip("/")
        if not title or not normalize_arxiv_id(arxiv_id):
            continue
        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("type") == "application/pdf" or link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href", "")
                break
        published = entry.findtext("atom:published", default="", namespaces=ns)
        record = PaperRecord(
            title=title,
            authors=[
                node.findtext("atom:name", default="", namespaces=ns)
                for node in entry.findall("atom:author", ns)
            ],
            year=int(published[:4]) if published[:4].isdigit() else None,
            journal=entry.findtext("arxiv:journal_ref", default="", namespaces=ns),
            abstract=" ".join((entry.findtext("atom:summary", default="", namespaces=ns)).split()),
            doi=entry.findtext("arxiv:doi", default="", namespaces=ns),
            arxiv_id=arxiv_id,
            url=entry_url,
            item_type="preprint",
            is_oa=True,
            oa_status="green",
            license=entry.findtext("arxiv:license", default="", namespaces=ns),
            sources=["arxiv"],
            relevance_score=_score(rank, limit),
            extra={"publication_types": ["preprint"]},
        )
        record.add_pdf_candidate(pdf_url or f"https://arxiv.org/pdf/{arxiv_id}", "arxiv", record.license, "submittedVersion")
        records.append(record)
    return _finish_search(records, status)


def enrich_unpaywall(
    client: HttpClient, records: list[PaperRecord], email: str
) -> tuple[int, int, list[str]]:
    if not email:
        return 0, 0, []
    enriched = 0
    attempted = 0
    errors: list[str] = []
    for record in records:
        if not record.doi:
            continue
        attempted += 1
        try:
            payload = client.get_json(
                f"https://api.unpaywall.org/v2/{quote(record.doi, safe='')}", {"email": email}
            )
        except HttpError as exc:
            if exc.status == 404:
                continue
            errors.append(str(exc)[:500])
            continue
        except Exception as exc:
            errors.append(str(exc)[:500])
            continue
        before = len(record.pdf_candidates)
        locations = payload.get("oa_locations") or []
        best = payload.get("best_oa_location")
        if best:
            locations = [best] + locations
        for location in locations:
            if not isinstance(location, dict):
                continue
            license_value = location.get("license", "") or ""
            version = location.get("version", "") or ""
            pdf_url = location.get("url_for_pdf", "") or ""
            if pdf_url:
                record.add_pdf_candidate(pdf_url, "unpaywall", license_value, version)
            # Repository records frequently expose only a splash page. Dropping
            # them here was discarding genuinely open full text.
            landing = location.get("url_for_landing_page", "") or location.get("url", "") or ""
            if landing and landing != pdf_url:
                record.add_pdf_candidate(
                    landing, "unpaywall_landing", license_value, version, kind="landing"
                )
        if payload.get("is_oa"):
            record.is_oa = True
            record.oa_status = payload.get("oa_status", record.oa_status)
        if len(record.pdf_candidates) > before:
            enriched += 1
    return enriched, attempted, errors


def _resolve_biorxiv_preprint(
    client: HttpClient, doi: str
) -> tuple[dict[str, str] | None, list[str]]:
    """Resolve the latest official bioRxiv/medRxiv version for a 10.1101 DOI."""
    doi = normalize_doi(doi)
    if not doi.startswith("10.1101/"):
        return None, []
    errors: list[str] = []
    for server in ("medrxiv", "biorxiv"):
        try:
            payload = client.get_json(
                f"https://api.biorxiv.org/details/{server}/{quote(doi, safe='/')}/na/json"
            )
        except HttpError as exc:
            if exc.status != 404:
                errors.append(f"{server}: {exc}")
            continue
        except Exception as exc:
            errors.append(f"{server}: {exc}")
            continue
        matching = [
            item
            for item in (payload.get("collection") or [])
            if normalize_doi(item.get("doi", "")) == doi
        ]
        if not matching:
            continue
        latest = max(
            matching,
            key=lambda item: int(item.get("version", 0))
            if str(item.get("version", "")).isdigit()
            else 0,
        )
        version = str(latest.get("version", "1"))
        return (
            {
                "server": server,
                "doi": doi,
                "version": version,
                "license": str(latest.get("license", "")),
                "abstract": str(latest.get("abstract", "")),
                "pdf_url": f"https://www.{server}.org/content/{doi}v{version}.full.pdf",
            },
            errors,
        )
    return None, errors


def enrich_preprint_servers(
    client: HttpClient, records: list[PaperRecord]
) -> tuple[int, int, list[str]]:
    enriched = 0
    attempted = 0
    errors: list[str] = []
    for record in records:
        if not record.doi.startswith("10.1101/"):
            continue
        attempted += 1
        resolved, per_record_errors = _resolve_biorxiv_preprint(client, record.doi)
        if resolved:
            server = resolved["server"]
            record.add_pdf_candidate(
                resolved["pdf_url"],
                server,
                resolved["license"],
                "submittedVersion",
            )
            record.is_oa = True
            record.oa_status = record.oa_status or "green"
            record.item_type = "preprint"
            record.license = record.license or resolved["license"]
            record.sources = list(dict.fromkeys(record.sources + [server]))
            record.abstract = record.abstract or resolved["abstract"]
            enriched += 1
        elif per_record_errors:
            errors.append(f"{record.doi}: {' | '.join(per_record_errors)}"[:1000])
    return enriched, attempted, errors


def _has_direct_pdf(record: PaperRecord) -> bool:
    return any(candidate.get("kind", "pdf") == "pdf" for candidate in record.pdf_candidates)


def _author_match_tokens(values: list[str]) -> set[str]:
    """Return stable family-name candidates across `Given Family`/`Family Initials` styles."""
    tokens: set[str] = set()
    for value in values:
        parts = normalize_title(value).split()
        if not parts:
            continue
        for part in (parts[0], parts[-1]):
            if len(part) > 2:
                tokens.add(part)
    return tokens


def _preprint_metadata_matches(record: PaperRecord, item: dict[str, Any]) -> bool:
    if normalize_title(_strip_tags(item.get("title"))) != record.normalized_title:
        return False
    try:
        preprint_year = int(item.get("pubYear"))
    except (TypeError, ValueError):
        return False
    if record.year is None or abs(record.year - preprint_year) > 2:
        return False
    preprint_authors = [
        str(author.get("fullName") or "")
        for author in (item.get("authorList") or {}).get("author", [])
        if author.get("fullName")
    ]
    record_tokens = _author_match_tokens(record.authors)
    preprint_tokens = _author_match_tokens(preprint_authors)
    return bool(record_tokens and preprint_tokens and record_tokens & preprint_tokens)


def enrich_preprints_by_title(
    client: HttpClient, records: list[PaperRecord], limit: int = 5, force: bool = False
) -> tuple[int, int, list[str]]:
    """Find an author-deposited preprint of a paywalled paper by title.

    Matching on a ``10.1101/`` DOI prefix only catches records that are already
    the preprint. The common case is the opposite: the record is the published
    version under a publisher DOI, while a freely licensed preprint of the same
    manuscript sits on another server under a different DOI.

    Europe PMC indexes bioRxiv, medRxiv, Research Square, SSRN, ChemRxiv and
    Preprints.org together under ``SRC:PPR``, so one query covers them all.
    Only an exact normalized-title match with compatible author and year
    evidence is accepted -- a title alone can silently attach the wrong
    manuscript or conference abstract.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("preprint title limit must be an integer in 1..1000")
    enriched = 0
    attempted = 0
    errors: list[str] = []
    for record in records:
        if (not force and _has_direct_pdf(record)) or record.item_type == "preprint":
            continue
        if len(record.normalized_title) < 20:
            continue
        attempted += 1
        safe_title = record.title.replace('"', " ").replace("\\", " ").strip()
        try:
            payload = client.get_json(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                {
                    "query": f'TITLE:"{safe_title}" AND SRC:PPR',
                    "format": "json",
                    "pageSize": limit,
                    "resultType": "core",
                },
            )
        except HttpError as exc:
            errors.append(f"{record.doi or record.title[:60]}: {exc}"[:500])
            continue
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the pass
            errors.append(f"{record.doi or record.title[:60]}: {exc}"[:500])
            continue

        found = False
        for item in payload.get("resultList", {}).get("result", []):
            if not _preprint_metadata_matches(record, item):
                continue
            license_value = str(item.get("license", "") or "")
            for candidate in (item.get("fullTextUrlList") or {}).get("fullTextUrl", []):
                if str(candidate.get("documentStyle", "")).lower() == "pdf":
                    record.add_pdf_candidate(
                        candidate.get("url", ""),
                        "europe_pmc_preprint",
                        license_value,
                        "submittedVersion",
                    )
                    found = True
            preprint_doi = normalize_doi(item.get("doi", ""))
            if preprint_doi and not found:
                resolved = None
                resolution_errors: list[str] = []
                if preprint_doi.startswith("10.1101/"):
                    resolved, resolution_errors = _resolve_biorxiv_preprint(client, preprint_doi)
                if resolved:
                    server = resolved["server"]
                    record.add_pdf_candidate(
                        resolved["pdf_url"],
                        server,
                        resolved["license"] or license_value,
                        "submittedVersion",
                    )
                    record.sources = list(dict.fromkeys(record.sources + [server]))
                else:
                    errors.extend(
                        f"{record.doi or record.title[:60]} -> {preprint_doi}: {error}"[:500]
                        for error in resolution_errors
                    )
                    # Keep a resumable manual route when Europe PMC supplies a
                    # preprint DOI but no direct file, or when the official
                    # bioRxiv metadata service cannot resolve that DOI.
                    record.add_pdf_candidate(
                        f"https://doi.org/{preprint_doi}",
                        "europe_pmc_preprint_landing",
                        license_value,
                        "submittedVersion",
                        kind="landing",
                    )
                found = True
            if found:
                record.extra["preprint_doi"] = preprint_doi or item.get("id", "")
                record.extra["version_relation_confidence"] = "high"
                record.extra["version_relation_basis"] = "exact_title_author_year"
                record.sources = list(dict.fromkeys(record.sources + ["europe_pmc_preprint"]))
                if record.is_oa is None:
                    record.oa_status = record.oa_status or "green"
                break
        if found:
            enriched += 1
    return enriched, attempted, errors


def _is_oa_landing(url: str) -> bool:
    return bool(is_public_https_url(url))


def _pdf_urls_in_structure(value: Any, base: str = "") -> list[str]:
    """Collect PDF-looking URLs from an irregular nested API response.

    Aggregator responses nest their link objects at inconsistent depths, so a
    tolerant walker is more maintainable than a fixed field path. Only strings
    that already look like PDFs are kept -- every other URL is a landing page
    and would need separate resolution.
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(child, str):
                    lowered = child.casefold()
                    if ".pdf" in lowered and (
                        "url" in key.casefold() or "link" in key.casefold() or ".pdf" in lowered
                    ):
                        if is_public_https_url(child):
                            found.append(child)
                else:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                if isinstance(child, str):
                    if ".pdf" in child.casefold() and is_public_https_url(child):
                        found.append(child)
                else:
                    walk(child)

    walk(value)
    return list(dict.fromkeys(found))


def enrich_core(
    client: HttpClient, records: list[PaperRecord], api_key: str, force: bool = False,
) -> tuple[int, int, list[str]]:
    """Resolve open repository copies through CORE (api.core.ac.uk/v3)."""
    if not api_key:
        return 0, 0, []
    enriched = 0
    attempted = 0
    errors: list[str] = []
    for record in records:
        if (not force and _has_direct_pdf(record)) or not record.doi:
            continue
        attempted += 1
        try:
            payload = client.get_json(
                "https://api.core.ac.uk/v3/search/works",
                {
                    "q": f"doi:\"{record.doi}\"",
                    "limit": 5,
                    "scroll": "false",
                    "stats": "false",
                    "exclude": "fullText",
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except HttpError as exc:
            errors.append(f"{record.doi}: {exc}"[:500])
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{record.doi}: {exc}"[:500])
            continue
        before = len(record.pdf_candidates)
        for item in payload.get("results") or []:
            if not isinstance(item, dict) or normalize_doi(item.get("doi", "")) != record.doi:
                continue
            license_value = str(item.get("license") or "")
            pdf_url = str(item.get("downloadUrl") or "")
            if is_public_https_url(pdf_url):
                record.add_pdf_candidate(pdf_url, "core", license_value, "repositoryVersion")
            source_urls = item.get("sourceFulltextUrls") or []
            if isinstance(source_urls, str):
                source_urls = [source_urls]
            for url in source_urls:
                url = str(url or "")
                if url == pdf_url:
                    continue
                if is_public_https_url(url):
                    record.add_pdf_candidate(
                        url, "core_landing", license_value, "repositoryVersion", kind="landing"
                    )
        if len(record.pdf_candidates) > before:
            enriched += 1
            record.sources = list(dict.fromkeys(record.sources + ["core"]))
    return enriched, attempted, errors


def enrich_openaire(client: HttpClient, records: list[PaperRecord]) -> tuple[int, int, list[str]]:
    """Resolve European repository copies through OpenAIRE."""
    enriched = 0
    attempted = 0
    errors: list[str] = []
    for record in records:
        if _has_direct_pdf(record) or not record.doi:
            continue
        attempted += 1
        try:
            payload = client.get_json(
                "https://api.openaire.eu/search/publications",
                {"doi": record.doi, "format": "json"},
            )
        except HttpError as exc:
            errors.append(f"{record.doi}: {exc}"[:500])
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{record.doi}: {exc}"[:500])
            continue
        found = False
        for url in _pdf_urls_in_structure(payload):
            if url:
                record.add_pdf_candidate(url, "openaire", "", "acceptedVersion")
                found = True
        if found:
            enriched += 1
            record.sources = list(dict.fromkeys(record.sources + ["openaire"]))
    return enriched, attempted, errors


def enrich_doaj(client: HttpClient, records: list[PaperRecord]) -> tuple[int, int, list[str]]:
    """Resolve full-text links from DOAJ (Directory of Open Access Journals)."""
    enriched = 0
    attempted = 0
    errors: list[str] = []
    for record in records:
        if _has_direct_pdf(record) or not record.doi:
            continue
        attempted += 1
        try:
            payload = client.get_json(
                "https://doaj.org/api/search/articles/doi:" + quote(record.doi, safe="")
            )
        except HttpError as exc:
            errors.append(f"{record.doi}: {exc}"[:500])
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{record.doi}: {exc}"[:500])
            continue
        found = False
        for item in payload.get("results") or []:
            bibjson = item.get("bibjson") or {}
            for link in bibjson.get("link") or []:
                url = str(link.get("url", "") or "")
                if not is_public_https_url(url):
                    continue
                if ".pdf" in url.casefold():
                    record.add_pdf_candidate(url, "doaj", "", "publishedVersion")
                    found = True
                else:
                    record.add_pdf_candidate(url, "doaj_landing", "", "publishedVersion", kind="landing")
                    found = True
        if found:
            enriched += 1
            record.sources = list(dict.fromkeys(record.sources + ["doaj"]))
    return enriched, attempted, errors


SOURCE_FUNCTIONS = {
    "pubmed": search_pubmed,
    "crossref": search_crossref,
    "europe_pmc": search_europe_pmc,
    "openalex": search_openalex,
    "semantic_scholar": search_semantic_scholar,
    "arxiv": search_arxiv,
}
