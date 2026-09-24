from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import re
import unicodedata
from typing import Any
from urllib.parse import unquote, urlparse


DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
EMPTY_IDENTIFIERS = {"", "none", "null", "nan", "n/a", "undefined"}


def identifier_text(value: Any) -> str:
    if value is None or isinstance(value, (bool, dict, list, tuple, set)):
        return ""
    text = str(value).strip()
    return "" if text.casefold() in EMPTY_IDENTIFIERS else text


def normalize_doi(value: Any) -> str:
    text = DOI_PREFIX_RE.sub("", unquote(identifier_text(value)))
    match = DOI_RE.search(text)
    if not match:
        return ""
    doi = match.group(0).rstrip(".,;").lower()
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    return doi


def normalize_pmid(value: Any) -> str:
    text = identifier_text(value)
    if text.lower().startswith(("http://", "https://")):
        parsed = urlparse(text)
        if (parsed.hostname or "").casefold() not in {"pubmed.ncbi.nlm.nih.gov", "europepmc.org"}:
            return ""
        text = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    text = re.sub(r"^PMID\s*:\s*", "", text, flags=re.I)
    return text if re.fullmatch(r"[1-9]\d*", text) else ""


def normalize_pmcid(value: Any) -> str:
    text = identifier_text(value)
    if text.lower().startswith(("http://", "https://")):
        parsed = urlparse(text)
        if (parsed.hostname or "").casefold() not in {
            "pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "europepmc.org",
        }:
            return ""
        text = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    text = re.sub(r"^PMCID\s*:\s*", "", text, flags=re.I).upper()
    return text if re.fullmatch(r"PMC[1-9]\d*", text) else ""


def normalize_arxiv_id(value: Any) -> str:
    text = identifier_text(value)
    text = re.sub(r"^(?:https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", "", text, flags=re.I)
    text = re.sub(r"\.pdf$", "", text, flags=re.I)
    return text if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", text, re.I) else ""


def normalize_title(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return ""


def candidate_request_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    """Behavioral request context, distinct from article and OA-location identity."""
    return (str(candidate.get("url") or "").strip(), str(candidate.get("kind") or "pdf"),
            str(candidate.get("referer") or "").strip())


@dataclass
class PaperRecord:
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str = ""
    abstract: str = ""
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    arxiv_id: str = ""
    openalex_id: str = ""
    semantic_scholar_id: str = ""
    url: str = ""
    item_type: str = "journalArticle"
    citation_count: int | None = None
    is_oa: bool | None = None
    oa_status: str = ""
    license: str = ""
    sources: list[str] = field(default_factory=list)
    pdf_candidates: list[dict[str, Any]] = field(default_factory=list)
    relevance_score: float = 0.0
    download_status: str = "pending"
    local_pdf: str = ""
    local_fulltext: str = ""
    fulltext_format: str = ""
    retrieval_type: str = ""
    download_version: str = ""
    sha256: str = ""
    download_source: str = ""
    download_url: str = ""
    failure_reason: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    identity_status: str = "unverified"
    duplicate_of: str = ""
    retry_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    zotero: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = str(self.title or "").strip()
        self.doi = normalize_doi(self.doi)
        self.pmid = normalize_pmid(self.pmid)
        self.pmcid = normalize_pmcid(self.pmcid)
        self.arxiv_id = normalize_arxiv_id(self.arxiv_id)
        self.openalex_id = identifier_text(self.openalex_id)
        self.semantic_scholar_id = identifier_text(self.semantic_scholar_id)
        self.authors = [identifier_text(author) for author in (self.authors or []) if identifier_text(author)]
        self.sources = list(dict.fromkeys(source for source in (self.sources or []) if source))
        self.attempts = [dict(item) for item in (self.attempts or []) if isinstance(item, dict)]
        self.extra = dict(self.extra or {})
        self.zotero = dict(self.zotero or {})
        original = list(self.pdf_candidates or [])
        self.pdf_candidates = []
        for candidate in original:
            if isinstance(candidate, dict):
                self.add_pdf_candidate(**candidate)

    @property
    def normalized_title(self) -> str:
        return normalize_title(self.title)

    @property
    def identity_keys(self) -> list[str]:
        keys = []
        for prefix, value in (
            ("doi", normalize_doi(self.doi)), ("pmid", normalize_pmid(self.pmid)),
            ("pmcid", normalize_pmcid(self.pmcid)), ("arxiv", normalize_arxiv_id(self.arxiv_id)),
        ):
            if value:
                keys.append(f"{prefix}:{value.casefold()}")
        if self.normalized_title:
            keys.append(f"title:{self.normalized_title}")
        return keys

    @property
    def identity_digest(self) -> str:
        strong = [key for key in self.identity_keys if not key.startswith("title:")]
        identity = strong[0] if strong else "|".join(
            [self.normalized_title, str(self.year or ""), *(normalize_title(a) for a in self.authors)]
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    def add_pdf_candidate(
        self, url: str = "", source: str = "", license: str = "", version: str = "",
        kind: str = "pdf", **metadata: Any,
    ) -> None:
        url = str(url or "").strip()
        if not url:
            return
        candidate = {
            "url": url, "source": str(source or ""), "license": str(license or ""),
            "version": str(version or ""), "kind": str(kind or "pdf"),
        }
        # Provenance and access eligibility must survive manifest round trips;
        # request credentials are never part of a persistent candidate.
        for key, value in metadata.items():
            if key.casefold() in {"headers", "authorization", "api_key", "apikey", "token", "cookies"}:
                continue
            if value is None or isinstance(value, (str, bool, int, float, list, dict)):
                candidate[key] = value
        for existing in self.pdf_candidates:
            if candidate_request_key(existing) == candidate_request_key(candidate):
                for key, value in candidate.items():
                    if not existing.get(key) and value:
                        existing[key] = value
                return
        self.pdf_candidates.append(candidate)

    def merge(self, other: "PaperRecord") -> "PaperRecord":
        # The pipeline retains and flags conflicts instead of merging them.
        for name in ("doi", "pmid", "pmcid"):
            current, incoming = getattr(self, name), getattr(other, name)
            if current and incoming and current != incoming:
                raise ValueError(f"Conflicting {name} identifiers cannot be merged")
        self.title = first_nonempty(self.title, other.title)
        self.authors = self.authors or other.authors
        self.year = self.year or other.year
        self.journal = first_nonempty(self.journal, other.journal)
        self.abstract = max((self.abstract or "", other.abstract or ""), key=len)
        for name in ("doi", "pmid", "pmcid", "arxiv_id", "openalex_id", "semantic_scholar_id", "url", "license"):
            setattr(self, name, first_nonempty(getattr(self, name), getattr(other, name)))
        self.item_type = first_nonempty(self.item_type, other.item_type)
        counts = [value for value in (self.citation_count, other.citation_count) if value is not None]
        self.citation_count = max(counts) if counts else None
        if self.is_oa is not True:
            self.is_oa = other.is_oa if other.is_oa is not None else self.is_oa
        self.oa_status = first_nonempty(self.oa_status, other.oa_status)
        self.sources = list(dict.fromkeys(self.sources + other.sources))
        for candidate in other.pdf_candidates:
            self.add_pdf_candidate(**candidate)
        self.relevance_score = max(self.relevance_score, other.relevance_score)
        publication_types = list(dict.fromkeys(
            list(self.extra.get("publication_types") or []) + list(other.extra.get("publication_types") or [])))
        discovery_hits = list(self.extra.get("discovery_hits") or [])
        for hit in other.extra.get("discovery_hits") or []:
            if hit not in discovery_hits:
                discovery_hits.append(hit)
        self.extra.update({key: value for key, value in other.extra.items() if value not in (None, "")})
        if discovery_hits:
            self.extra["discovery_hits"] = discovery_hits
        if publication_types:
            self.extra["publication_types"] = publication_types
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PaperRecord":
        return cls(**{key: value for key, value in data.items() if key in cls.__dataclass_fields__})
