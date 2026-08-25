from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
import unicodedata
from typing import Any


DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)


def normalize_doi(value: str | None) -> str:
    if not value:
        return ""
    value = DOI_PREFIX_RE.sub("", value.strip())
    match = DOI_RE.search(value)
    return match.group(0).rstrip(".,;)").lower() if match else ""


def normalize_title(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = "".join(ch if ch.isalnum() else " " for ch in value)
    return " ".join(value.split())


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return ""


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
    pdf_candidates: list[dict[str, str]] = field(default_factory=list)
    relevance_score: float = 0.0
    download_status: str = "pending"
    local_pdf: str = ""
    sha256: str = ""
    download_source: str = ""
    download_url: str = ""
    failure_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    zotero: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()
        self.doi = normalize_doi(self.doi)
        self.authors = [str(author).strip() for author in self.authors if str(author).strip()]
        self.sources = list(dict.fromkeys(source for source in self.sources if source))
        original = list(self.pdf_candidates)
        self.pdf_candidates = []
        for candidate in original:
            self.add_pdf_candidate(**candidate)

    @property
    def normalized_title(self) -> str:
        return normalize_title(self.title)

    @property
    def identity_keys(self) -> list[str]:
        keys: list[str] = []
        for prefix, value in (
            ("doi", self.doi),
            ("pmid", self.pmid),
            ("pmcid", self.pmcid),
            ("arxiv", self.arxiv_id),
        ):
            if value:
                keys.append(f"{prefix}:{value.casefold()}")
        if self.normalized_title:
            keys.append(f"title:{self.normalized_title}")
        return keys

    def add_pdf_candidate(
        self,
        url: str = "",
        source: str = "",
        license: str = "",
        version: str = "",
        **_: Any,
    ) -> None:
        url = (url or "").strip()
        if not url:
            return
        if any(existing.get("url") == url for existing in self.pdf_candidates):
            return
        self.pdf_candidates.append(
            {"url": url, "source": source, "license": license, "version": version}
        )

    def merge(self, other: "PaperRecord") -> "PaperRecord":
        self.title = first_nonempty(self.title, other.title)
        self.authors = self.authors or other.authors
        self.year = self.year or other.year
        self.journal = first_nonempty(self.journal, other.journal)
        self.abstract = max((self.abstract, other.abstract), key=len)
        for name in (
            "doi",
            "pmid",
            "pmcid",
            "arxiv_id",
            "openalex_id",
            "semantic_scholar_id",
            "url",
            "license",
        ):
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
        self.extra.update({key: value for key, value in other.extra.items() if value not in (None, "")})
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PaperRecord":
        field_names = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in field_names})
