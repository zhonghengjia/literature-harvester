"""Conservative first-page article identity checks, separate from PDF syntax."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any

from .models import DOI_RE, PaperRecord, normalize_arxiv_id, normalize_doi, normalize_title


class PdfValidationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class IdentityResult:
    status: str
    reason: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pdf_backend() -> tuple[str, Any]:
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError as exc:
            try:
                from pypdf import PdfReader
            except ImportError:
                raise PdfValidationError("validation_unavailable", "A PDF parser is required; install PyMuPDF or pypdf before verification") from exc
            return "pypdf", PdfReader
    if not hasattr(fitz, "open"):
        raise PdfValidationError("validation_unavailable", "The installed fitz module is not a usable PyMuPDF parser")
    return "pymupdf", fitz


def read_first_page(path: Path) -> str:
    with path.open("rb") as handle:
        if b"%PDF-" not in handle.read(4096):
            raise PdfValidationError("not_pdf", "Content has no PDF signature")
    backend_name, backend = _pdf_backend()
    try:
        if backend_name == "pypdf":
            with path.open("rb") as handle:
                document = backend(handle)
                if document.is_encrypted:
                    raise PdfValidationError("manual_review", "Encrypted PDF cannot be verified without a password")
                if len(document.pages) < 1:
                    raise PdfValidationError("not_pdf", "PDF parser found zero pages")
                return (document.pages[0].extract_text() or "")[:20000]
        with backend.open(path) as document:
            if document.page_count < 1:
                raise PdfValidationError("not_pdf", "PDF parser found zero pages")
            if document.needs_pass:
                raise PdfValidationError("manual_review", "Encrypted PDF cannot be verified without a password")
            return document[0].get_text("text", sort=True)[:20000]
    except PdfValidationError:
        raise
    except Exception as exc:
        raise PdfValidationError("not_pdf", f"PDF parser rejected the file: {exc}") from exc


def validate_pdf_structure(path: Path) -> None:
    read_first_page(path)


def identity_from_first_page(text: str, record: PaperRecord) -> IdentityResult:
    # Never turn identifiers in a references section into evidence about this
    # article. We also do not search later pages or PDF metadata for a match.
    front = re.split(r"(?im)^\s*(?:\d+[. ]+)?(?:references|bibliography|literature cited)\s*[:.]?\s*$", text, maxsplit=1)[0]
    front = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", front)
    header_lines = [line.strip() for line in front.splitlines() if line.strip()][:8]
    supplemental = any(re.match(
        r"^(?:electronic\s+)?(?:supplementary|supplemental|supporting)\s+(?:information|material|data|appendix|file|methods)\b",
        line, re.I,
    ) for line in header_lines)
    if supplemental:
        return IdentityResult("mismatch", "The PDF identifies itself as supplementary material", {"supplementary": True})
    notice_heading = re.compile(
        r"^(?:(?:publisher|author|editor)(?:'s)?\s+)?(?:correction|erratum|corrigendum|retraction)(?:\s*:|\s+to\b|\s*$)",
        re.I,
    )
    if not notice_heading.match(record.title) and any(notice_heading.match(line) for line in header_lines):
        return IdentityResult("manual_review", "A correction/retraction notice cannot establish the original article identity", {"related_article_notice": True})
    normalized = normalize_title(front)
    title = record.normalized_title
    # A title is only evidence in the article header, not in prose later on
    # the first page. Exact normalized wording tolerates punctuation/line wrap.
    header = normalize_title(front[:6000])
    title_match = bool(title) and f" {title} " in f" {header} "
    expected_dois = {normalize_doi(record.doi)} - {""}
    if record.extra.get("version_relation_confidence") == "high":
        expected_dois.add(normalize_doi(record.extra.get("preprint_doi")))
    expected_dois.update(normalize_doi(v) for v in record.extra.get("approved_preprint_dois", []))
    expected_dois.discard("")
    observed_dois = {normalize_doi(match.group(0)) for match in DOI_RE.finditer(front)} - {""}
    matching_dois = sorted(expected_dois & observed_dois)
    expected_arxiv = re.sub(r"v\d+$", "", normalize_arxiv_id(record.arxiv_id), flags=re.I).casefold()
    arxiv_pattern = r"(?:\barxiv\s*:\s*|https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/)((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)"
    observed_arxiv = {
        re.sub(r"v\d+$", "", normalize_arxiv_id(match.group(1)), flags=re.I).casefold()
        for match in re.finditer(arxiv_pattern, front, re.I)
    } - {""}
    if expected_arxiv and observed_arxiv and observed_arxiv != {expected_arxiv}:
        return IdentityResult("manual_review", "First-page arXiv identity conflicts with the requested article",
                              {"expected_arxiv": expected_arxiv, "first_page_arxiv_ids": sorted(observed_arxiv)})
    author_tokens: set[str] = set()
    for author in record.authors:
        parts = normalize_title(author).split()
        if not parts:
            continue
        # Family Initials and Given Family are both common source formats.
        surname = parts[0] if len(parts) > 1 and len(parts[-1]) <= 2 else parts[-1]
        if len(surname) >= 2:
            author_tokens.add(surname)
    author_match = bool(author_tokens & set(header.split()))
    evidence = {
        "page": 1, "title_match": title_match, "matching_dois": matching_dois,
        "author_match": author_match, "first_page_dois": sorted(observed_dois),
        "reference_section_excluded": front != text,
    }
    if not normalized:
        return IdentityResult("manual_review", "First page has no extractable text; OCR/manual identity review is required", evidence)
    if title_match and matching_dois and (len(title) >= 20 or author_match):
        return IdentityResult("verified", "Article title and expected DOI agree on the first page", evidence)
    if title_match and len(title) >= 20 and author_match and not observed_dois:
        return IdentityResult("verified", "Specific title and author agree in the first-page header; no conflicting DOI is present", evidence)
    if title_match and expected_dois and observed_dois and not matching_dois:
        return IdentityResult("manual_review", "Title agrees but first-page DOI is not an approved identifier/version relation", evidence)
    if matching_dois:
        return IdentityResult("manual_review", "A DOI match without the article title is insufficient identity evidence", evidence)
    return IdentityResult("manual_review", "First-page title/identifier evidence is insufficient to bind this PDF to the record", evidence)


def verify_pdf_identity(path: Path, record: PaperRecord, *, allow_ocr=True) -> IdentityResult:
    try:
        text = read_first_page(path)
    except PdfValidationError as exc:
        if exc.code in {"validation_unavailable", "manual_review"}:
            return IdentityResult("manual_review", str(exc), {"validation_error": exc.code})
        raise
    result = identity_from_first_page(text, record)
    if result.status == "verified" or result.evidence.get("supplementary") or result.evidence.get("related_article_notice"):
        return result
    # Only a cover page without conflicting publication IDs may defer identity.
    if (result.evidence.get("first_page_dois") or result.evidence.get("first_page_arxiv_ids")
            or result.evidence.get("reference_section_excluded")):
        return result
    if text.strip() and not re.search(r"(?im)\b(?:cover page|downloaded from|repository cover|this is a cover|author accepted manuscript)\b", text):
        return result
    backend_name, backend = _pdf_backend()
    try:
        if backend_name == "pymupdf":
            with backend.open(path) as document:
                if allow_ocr and not text.strip():
                    try:
                        textpage = document[0].get_textpage_ocr(language="eng", dpi=150, full=True)
                        value = document[0].get_text("text", textpage=textpage)[:20000]
                        checked = identity_from_first_page(value, record)
                        if checked.status == "verified":
                            return IdentityResult("verified", "Local OCR header matches article identity",
                                                  {**checked.evidence, "identity_text_origin": "local_OCR"})
                        if checked.evidence.get("first_page_dois") or checked.evidence.get("supplementary"):
                            return checked
                    except Exception as exc:
                        result = IdentityResult(result.status, result.reason,
                                                {**result.evidence, "local_ocr_error": type(exc).__name__})
                pages = [(i+1, document[i].get_text("text", sort=True)[:20000]) for i in range(1, min(3, document.page_count))]
        else:
            with path.open("rb") as stream:
                document = backend(stream)
                pages = [(i+1, (document.pages[i].extract_text() or "")[:20000]) for i in range(1, min(3, len(document.pages)))]
        for number, value in pages:
            checked = identity_from_first_page(value, record)
            if checked.status == "verified" and checked.evidence.get("matching_dois"):
                return IdentityResult("verified", "Title and approved DOI agree after an identifier-free cover page",
                                      {**checked.evidence, "page": number, "cover_page_examined": True})
            if checked.evidence.get("supplementary") or checked.evidence.get("related_article_notice") or checked.evidence.get("first_page_dois"):
                break
    except Exception as exc:
        return IdentityResult(result.status, result.reason, {**result.evidence, "later_page_error": type(exc).__name__})
    return result
