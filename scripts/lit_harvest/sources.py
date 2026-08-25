from __future__ import annotations

from datetime import datetime
import html
import math
import re
from typing import Any
from urllib.parse import quote, urlencode
from xml.etree import ElementTree

from .http import HttpClient, HttpError
from .models import PaperRecord, normalize_doi


ARXIV_ID_RE = re.compile(r"(?:arxiv:|arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+/\d{7})(?:\.pdf)?$", re.I)
PMID_RE = re.compile(r"^(?:pmid:\s*)?(\d{5,9})$", re.I)
PMCID_RE = re.compile(r"^(?:pmcid:\s*)?(PMC\d+)$", re.I)


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
    match = ARXIV_ID_RE.search(query)
    if match:
        return "arxiv", match.group(1)
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
        records.append(record)
    return records


def search_pubmed(
    client: HttpClient,
    query: str,
    limit: int,
    contact_email: str = "",
    api_key: str = "",
) -> list[PaperRecord]:
    kind, value = detect_identifier(query)
    if kind == "pmid":
        ids = [value]
    else:
        term = {
            "doi": f'"{value}"[AID]',
            "pmcid": f'"{value}"[PMCID]',
        }.get(kind, value)
        params: dict[str, Any] = {
            "db": "pubmed",
            "term": term,
            "retmax": min(limit, 500),
            "retmode": "json",
            "sort": "relevance",
            "tool": "literature_harvester",
        }
        if contact_email:
            params["email"] = contact_email
        if api_key:
            params["api_key"] = api_key
        payload = client.get_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params
        )
        ids = [str(item) for item in payload.get("esearchresult", {}).get("idlist", []) if item]
    if not ids:
        return []
    params = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "xml",
        "tool": "literature_harvester",
    }
    if contact_email:
        params["email"] = contact_email
    if api_key:
        params["api_key"] = api_key
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urlencode(params)
    with client.request(url, headers={"Accept": "application/xml"}) as response:
        return _parse_pubmed_xml(response.read(), limit)


def search_crossref(client: HttpClient, query: str, limit: int, contact_email: str = "") -> list[PaperRecord]:
    kind, value = detect_identifier(query)
    if kind == "doi":
        payload = client.get_json(f"https://api.crossref.org/works/{quote(value, safe='')}")
        items = [payload.get("message", {})]
    else:
        params: dict[str, Any] = {"query.bibliographic": query, "rows": limit}
        if contact_email:
            params["mailto"] = contact_email
        payload = client.get_json("https://api.crossref.org/works", params)
        items = payload.get("message", {}).get("items", [])
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
            },
        )
        if verified_oa_license:
            for link in item.get("link") or []:
                if link.get("content-type") == "application/pdf":
                    record.add_pdf_candidate(
                        link.get("URL", ""), "crossref_verified_oa", license_url, "publishedVersion"
                    )
        records.append(record)
    return records


def search_europe_pmc(client: HttpClient, query: str, limit: int) -> list[PaperRecord]:
    kind, value = detect_identifier(query)
    epmc_query = {
        "doi": f'DOI:"{value}"',
        "pmid": f"EXT_ID:{value} AND SRC:MED",
        "pmcid": f"P_PMCID:{value}",
    }.get(kind, query)
    payload = client.get_json(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        {"query": epmc_query, "format": "json", "pageSize": limit, "resultType": "core"},
    )
    records: list[PaperRecord] = []
    for rank, item in enumerate(payload.get("resultList", {}).get("result", [])):
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
        pmcid = item.get("pmcid", "")
        is_oa = str(item.get("isOpenAccess", "")).upper() == "Y"
        record = PaperRecord(
            title=title,
            authors=authors,
            year=int(item["pubYear"]) if str(item.get("pubYear", "")).isdigit() else None,
            journal=item.get("journalTitle", ""),
            abstract=_strip_tags(item.get("abstractText")),
            doi=item.get("doi", ""),
            pmid=item.get("pmid", ""),
            pmcid=pmcid,
            url=f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', '')}",
            item_type="journalArticle",
            citation_count=int(item["citedByCount"]) if str(item.get("citedByCount", "")).isdigit() else None,
            is_oa=is_oa,
            oa_status="open" if is_oa else "closed_or_unknown",
            license=item.get("license", ""),
            sources=["europe_pmc"],
            relevance_score=_score(rank, limit),
        )
        full_text_urls = (item.get("fullTextUrlList") or {}).get("fullTextUrl", [])
        for candidate in full_text_urls:
            if str(candidate.get("documentStyle", "")).lower() == "pdf":
                record.add_pdf_candidate(
                    candidate.get("url", ""), "europe_pmc", record.license, "publishedVersion"
                )
        if is_oa and pmcid and not record.pdf_candidates:
            record.add_pdf_candidate(
                f"https://europepmc.org/articles/{pmcid}?pdf=render",
                "europe_pmc",
                record.license,
                "publishedVersion",
            )
        if is_oa and pmcid:
            record.add_pdf_candidate(
                "https://pmc-oa-opendata.s3.amazonaws.com/?"
                + urlencode({"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"}),
                "pmc_oa_cloud",
                record.license,
                "publishedVersion",
            )
        records.append(record)
    return records


def _openalex_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        positions.extend((int(index), word) for index in indexes)
    return " ".join(word for _, word in sorted(positions))


def search_openalex(
    client: HttpClient,
    query: str,
    limit: int,
    api_key: str = "",
    contact_email: str = "",
) -> list[PaperRecord]:
    kind, value = detect_identifier(query)
    params: dict[str, Any] = {"per-page": limit}
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
    payload = client.get_json("https://api.openalex.org/works", params)
    records: list[PaperRecord] = []
    for rank, item in enumerate(payload.get("results", [])):
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
            pmid=str(ids.get("pmid", "")).rstrip("/").split("/")[-1],
            pmcid=str(ids.get("pmcid", "")).rstrip("/").split("/")[-1],
            openalex_id=str(item.get("id", "")).rstrip("/").split("/")[-1],
            url=primary.get("landing_page_url") or item.get("id", ""),
            item_type="preprint" if item.get("type") == "preprint" else "journalArticle",
            citation_count=item.get("cited_by_count"),
            is_oa=open_access.get("is_oa"),
            oa_status=open_access.get("oa_status", ""),
            license=best.get("license", ""),
            sources=["openalex"],
            relevance_score=_score(rank, limit),
        )
        record.add_pdf_candidate(
            best.get("pdf_url", ""), "openalex", best.get("license", ""), best.get("version", "")
        )
        records.append(record)
    return records


def search_semantic_scholar(
    client: HttpClient,
    query: str,
    limit: int,
    api_key: str = "",
) -> list[PaperRecord]:
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
        payload = client.get_json(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            {"query": query, "limit": min(limit, 100), "fields": fields},
            headers,
        )
        items = payload.get("data", [])
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
            item_type="preprint" if "Review" not in publication_types and external.get("ArXiv") else "journalArticle",
            citation_count=item.get("citationCount"),
            is_oa=True if oa_pdf.get("url") else None,
            sources=["semantic_scholar"],
            relevance_score=_score(rank, limit),
        )
        record.add_pdf_candidate(
            oa_pdf.get("url", ""), "semantic_scholar", oa_pdf.get("license", ""), ""
        )
        records.append(record)
    return records


def search_arxiv(client: HttpClient, query: str, limit: int) -> list[PaperRecord]:
    kind, value = detect_identifier(query)
    params = {"start": 0, "max_results": limit, "sortBy": "relevance", "sortOrder": "descending"}
    if kind == "arxiv":
        params["id_list"] = value
    else:
        params["search_query"] = f'all:"{query}"'
    with client.request(
        "https://export.arxiv.org/api/query?" + "&".join(f"{key}={quote(str(value))}" for key, value in params.items()),
        headers={"Accept": "application/atom+xml"},
    ) as response:
        root = ElementTree.fromstring(response.read())
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    records: list[PaperRecord] = []
    for rank, entry in enumerate(root.findall("atom:entry", ns)):
        title = " ".join((entry.findtext("atom:title", default="", namespaces=ns)).split())
        entry_url = entry.findtext("atom:id", default="", namespaces=ns)
        arxiv_id = entry_url.rstrip("/").split("/")[-1]
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
        )
        record.add_pdf_candidate(pdf_url or f"https://arxiv.org/pdf/{arxiv_id}", "arxiv", record.license, "submittedVersion")
        records.append(record)
    return records


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
            record.add_pdf_candidate(
                location.get("url_for_pdf", ""),
                "unpaywall",
                location.get("license", ""),
                location.get("version", ""),
            )
        if payload.get("is_oa"):
            record.is_oa = True
            record.oa_status = payload.get("oa_status", record.oa_status)
        if len(record.pdf_candidates) > before:
            enriched += 1
    return enriched, attempted, errors


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
        found = False
        per_record_errors: list[str] = []
        for server in ("medrxiv", "biorxiv"):
            try:
                payload = client.get_json(
                    f"https://api.biorxiv.org/details/{server}/{quote(record.doi, safe='/')}/na/json"
                )
            except HttpError as exc:
                if exc.status != 404:
                    per_record_errors.append(f"{server}: {exc}")
                continue
            except Exception as exc:
                per_record_errors.append(f"{server}: {exc}")
                continue
            versions = payload.get("collection") or []
            matching = [
                item for item in versions if normalize_doi(item.get("doi", "")) == record.doi
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
            license_value = str(latest.get("license", ""))
            record.add_pdf_candidate(
                f"https://www.{server}.org/content/{record.doi}v{version}.full.pdf",
                server,
                license_value,
                "submittedVersion",
            )
            record.is_oa = True
            record.oa_status = record.oa_status or "green"
            record.item_type = "preprint"
            record.license = record.license or license_value
            record.sources = list(dict.fromkeys(record.sources + [server]))
            record.abstract = record.abstract or str(latest.get("abstract", ""))
            found = True
            break
        if found:
            enriched += 1
        elif per_record_errors:
            errors.append(f"{record.doi}: {' | '.join(per_record_errors)}"[:1000])
    return enriched, attempted, errors


SOURCE_FUNCTIONS = {
    "pubmed": search_pubmed,
    "crossref": search_crossref,
    "europe_pmc": search_europe_pmc,
    "openalex": search_openalex,
    "semantic_scholar": search_semantic_scholar,
    "arxiv": search_arxiv,
}
