"""Resolve declared OA PDF links and render explicitly eligible JATS readers.

Callers establish access eligibility before fetching landing pages or JATS.
HTTP errors retain their retry/deferred metadata for the run coordinator.
"""
from __future__ import annotations

from html.parser import HTMLParser
import copy
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree

from .http import HttpClient, HttpError, NETWORK_ERRORS, is_public_https_url, redact_url
from .models import normalize_pmcid

MAX_LANDING_BYTES = 768 * 1024
MAX_XML_BYTES = 12 * 1024 * 1024


class FullTextError(RuntimeError):
    def __init__(self, message: str, *, code: str = "fulltext_failed") -> None:
        super().__init__(message)
        self.code = code


def _decode(raw: bytes, response: Any) -> str:
    try:
        charset = response.headers.get_content_charset() or "utf-8"
    except AttributeError:
        charset = "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class _DeclaredPdfParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): (value or "").strip() for key, value in attrs}
        if tag.casefold() == "meta":
            name = (values.get("name") or values.get("property") or "").casefold()
            if name in {"citation_pdf_url", "eprints.document_url"} and values.get("content"):
                self.candidates.append(values["content"])
        elif tag.casefold() == "link":
            rel = {part.casefold() for part in values.get("rel", "").split()}
            media_type = values.get("type", "").casefold().split(";", 1)[0].strip()
            if "alternate" in rel and media_type == "application/pdf" and values.get("href"):
                self.candidates.append(values["href"])


def extract_pdf_urls(html: str, base_url: str) -> list[str]:
    """Return all source-declared public HTTPS URLs, in document order."""
    parser = _DeclaredPdfParser()
    parser.feed(html)
    parser.close()
    results: list[str] = []
    for value in parser.candidates:
        try:
            parsed = urlparse(urljoin(base_url, value))
            candidate = urlunparse(parsed._replace(fragment=""))
        except ValueError:
            continue
        if candidate not in results and is_public_https_url(candidate):
            results.append(candidate)
    return results


def extract_pdf_url(html: str, base_url: str) -> str:
    """Compatibility wrapper; new callers should retain the plural result."""
    return next(iter(extract_pdf_urls(html, base_url)), "")


def _fetch_bounded(client: HttpClient, url: str, limit: int, accept: str) -> tuple[bytes, str, str]:
    if not is_public_https_url(url):
        raise FullTextError(f"Not a public HTTPS URL: {redact_url(url)}", code="unsafe_url")
    try:
        with client.request(url, headers={"Accept": accept}) as response:
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise FullTextError(f"Response exceeded {limit} bytes", code="body_too_large")
            resolved_url = getattr(response, "url", url) or url
            if not is_public_https_url(resolved_url):
                raise FullTextError("Response resolved to an unsafe URL", code="unsafe_url")
            return raw, resolved_url, _decode(raw, response)
    except HttpError:
        raise
    except NETWORK_ERRORS as exc:
        raise HttpError(f"Response body read failed for {redact_url(url)} ({type(exc).__name__})",
                        url=url, code="body_read_failed") from None


def resolve_landing_pdfs(client: HttpClient, url: str) -> list[str]:
    """Fetch an already eligible OA page; never infer PDF paths."""
    _, resolved_base, html = _fetch_bounded(client, url, MAX_LANDING_BYTES,
                                           "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1")
    candidates = extract_pdf_urls(html, resolved_base)
    if not candidates:
        raise FullTextError("Landing page declared no public HTTPS PDF metadata", code="no_declared_pdf")
    return candidates


def resolve_landing_pdf(client: HttpClient, url: str) -> str:
    return resolve_landing_pdfs(client, url)[0]


_JATS_SKIP = {"xref", "fn", "table-wrap", "inline-formula", "disp-formula", "graphic", "media"}


def _jats_text(node: ElementTree.Element) -> str:
    parts: list[str] = []

    def walk(element: ElementTree.Element) -> None:
        if element.tag not in _JATS_SKIP:
            if element.text:
                parts.append(element.text)
            for child in element:
                walk(child)
        if element.tail:
            parts.append(element.tail)

    walk(node)
    return " ".join("".join(parts).split())


def _jats_section(node: ElementTree.Element, depth: int, lines: list[str]) -> int:
    prose_chars = 0
    title = node.find("title")
    if title is not None:
        heading = _jats_text(title)
        if heading:
            lines.extend([f"{'#' * min(depth, 6)} {heading}", ""])
    for child in node:
        if child.tag == "sec":
            prose_chars += _jats_section(child, depth + 1, lines)
        elif child.tag in {"boxed-text", "disp-quote", "speech", "statement"}:
            prose_chars += _jats_section(child, depth, lines)
        elif child.tag in {"p", "list-item"}:
            text = _jats_text(child)
            if text:
                lines.extend([text, ""])
                prose_chars += len(text)
        elif child.tag == "list":
            for item in child:
                text = _jats_text(item)
                if text:
                    lines.append(f"- {text}")
                    prose_chars += len(text)
            lines.append("")
    return prose_chars


def _parse_jats(raw: bytes, expected_pmcid: str = "", *, preserve_namespaces: bool = False) -> ElementTree.Element:
    if len(raw) > MAX_XML_BYTES:
        raise FullTextError("Full-text XML exceeded size limit", code="body_too_large")
    # Do not expand locally declared entities from remote XML. Standard external
    # JATS DOCTYPE declarations are allowed; ElementTree does not fetch them.
    if b"<!ENTITY" in raw.upper():
        raise FullTextError("Full-text XML contained an entity declaration", code="invalid_xml")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise FullTextError("Full-text XML did not parse", code="invalid_xml") from None
    original = root
    if preserve_namespaces:
        root = copy.deepcopy(root)
    for node in root.iter():
        node.tag = node.tag.split("}")[-1]
    if root.tag != "article":
        raise FullTextError("Full-text response is not a JATS article", code="invalid_xml")
    if expected_pmcid:
        ids = set()
        for node in root.findall("./front/article-meta/article-id"):
            if node.get("pub-id-type", "").casefold() not in {"pmc", "pmcid"}:
                continue
            value = (node.text or "").strip()
            # JATS pub-id-type='pmc' commonly stores the numeric portion.
            if re.fullmatch(r"[1-9]\d*", value):
                value = "PMC" + value
            ids.add(normalize_pmcid(value))
        ids.discard("")
        if ids != {expected_pmcid}:
            raise FullTextError("JATS PMCID is missing or differs from the requested article", code="identity_mismatch")
    return original if preserve_namespaces else root


def jats_to_markdown(raw: bytes, expected_pmcid: str = "") -> str:
    """Render a prose reader, not a lossless representation of tables/formulas."""
    root = _parse_jats(raw, expected_pmcid)
    body = root.find("./body")
    if body is None:
        raise FullTextError("Full-text XML contained no usable body text", code="no_usable_body")
    body_lines: list[str] = []
    if _jats_section(body, 2, body_lines) < 400:
        raise FullTextError("Full-text XML contained no usable body text", code="no_usable_body")
    lines: list[str] = []
    title_node = root.find("./front/article-meta/title-group/article-title")
    if title_node is None:
        title_node = root.find(".//article-title")
    if title_node is not None:
        title = _jats_text(title_node)
        if title:
            lines.extend([f"# {title}", ""])
    authors: list[str] = []
    for contrib in root.findall("./front/article-meta/contrib-group/contrib"):
        if contrib.get("contrib-type", "author") != "author":
            continue
        surname, given = contrib.find(".//surname"), contrib.find(".//given-names")
        name = " ".join(value for value in (_jats_text(given) if given is not None else "",
                                             _jats_text(surname) if surname is not None else "") if value)
        if name:
            authors.append(name)
    if authors:
        lines.extend(["*" + "; ".join(dict.fromkeys(authors)) + "*", ""])
    abstract = root.find("./front/article-meta/abstract")
    if abstract is not None:
        lines.extend(["## Abstract", ""])
        _jats_section(abstract, 3, lines)
    lines.extend(body_lines)
    references = [_jats_text(ref) for ref in root.findall("./back/ref-list/ref")]
    if any(references):
        lines.extend(["## References", "", *(f"- {ref}" for ref in references if ref), ""])
    return "\n".join(lines).strip() + "\n"


def fetch_jats_url(client: HttpClient, url: str, pmcid: str) -> str:
    """Fetch a caller-authorized JATS candidate and verify its PMCID."""
    normalized = normalize_pmcid(pmcid)
    if not normalized:
        raise FullTextError("Not a usable PMCID", code="invalid_identifier")
    raw, _, _ = _fetch_bounded(client, url, MAX_XML_BYTES, "application/xml")
    return jats_to_markdown(raw, expected_pmcid=normalized)


def fetch_jats_fulltext(client: HttpClient, pmcid: str) -> str:
    """Compatibility helper; caller must first establish article OA eligibility."""
    normalized = normalize_pmcid(pmcid)
    if not normalized:
        raise FullTextError("Not a usable PMCID", code="invalid_identifier")
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{normalized}/fullTextXML"
    return fetch_jats_url(client, url, normalized)
