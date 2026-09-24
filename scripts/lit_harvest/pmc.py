"""Resolve per-version PMC Cloud metadata, preserving OA and TDM boundaries.

Authority: https://pmc-oa-opendata.s3.amazonaws.com/README.txt (checked 2026-09-08).
Versions reflect deposit processing order, not an automatic preference ranking.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from xml.etree import ElementTree

from .fulltext import FullTextError
from .http import HttpClient, HttpError, NETWORK_ERRORS, is_public_https_url
from .models import normalize_pmcid

BUCKET_HOST = "pmc-oa-opendata.s3.amazonaws.com"
BUCKET = "pmc-oa-opendata"
MAX_LISTING_BYTES = 1024 * 1024
MAX_LISTING_PAGES = 20
MAX_ARTICLE_VERSIONS = 100


def is_pmc_listing_candidate(candidate, url=None):
    """Recognize current and legacy resolver entries; this does not grant access."""
    return candidate.get("kind") == "pmc_listing" or (
        candidate.get("source") == "pmc_oa_cloud"
        and "list-type" in parse_qs(urlparse(url or candidate.get("url", "")).query))


class PmcError(FullTextError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message, code=code)


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return {"true": True, "yes": True, "false": False, "no": False}.get(value.strip().casefold())
    return None


def _listing_identity(url: str) -> tuple[str, dict[str, str]]:
    try:
        parsed = urlparse(url)
        values = parse_qs(parsed.query, keep_blank_values=True)
        valid = (parsed.scheme == "https" and parsed.netloc == BUCKET_HOST
                 and parsed.path in {"", "/"} and not parsed.fragment
                 and set(values) <= {"list-type", "prefix", "delimiter", "continuation-token", "max-keys"}
                 and all(len(items) == 1 for items in values.values())
                 and values.get("list-type") == ["2"] and values.get("delimiter") == ["/"])
        prefix = values.get("prefix", [""])[0]
        if not valid or not re.fullmatch(r"PMC[1-9]\d*\.", prefix):
            raise ValueError
        return normalize_pmcid(prefix[:-1]), {name: items[0] for name, items in values.items()}
    except (ValueError, TypeError):
        raise PmcError("unsafe_url", "PMC listing must be an exact official single-PMCID ListObjectsV2 URL") from None


def _read_listing(client: HttpClient, url: str) -> ElementTree.Element:
    if not is_public_https_url(url):
        raise PmcError("unsafe_url", "PMC listing failed public HTTPS validation")
    try:
        with client.request(url, headers={"Accept": "application/xml"}) as response:
            raw = response.read(MAX_LISTING_BYTES + 1)
            final_url = getattr(response, "url", url) or url
            requested_id, _ = _listing_identity(url)
            final_id, _ = _listing_identity(final_url)
            if final_id != requested_id:
                raise PmcError("identity_mismatch", "PMC listing redirected to another article")
    except HttpError:
        raise
    except NETWORK_ERRORS as exc:
        raise HttpError(f"PMC listing body read failed ({type(exc).__name__})", url=url,
                        code="body_read_failed") from None
    if len(raw) > MAX_LISTING_BYTES:
        raise PmcError("body_too_large", "PMC listing exceeded 1 MB")
    if b"<!ENTITY" in raw.upper():
        raise PmcError("invalid_metadata", "PMC listing contained an entity declaration")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise PmcError("invalid_metadata", "PMC listing returned invalid XML") from None
    for node in root.iter():
        node.tag = node.tag.split("}")[-1]
    if root.tag != "ListBucketResult":
        raise PmcError("invalid_metadata", "PMC listing returned an unexpected XML document")
    return root


def _object_candidate(value: Any, version_id: str, extension: str) -> tuple[str, str]:
    if not value:
        return "", ""
    if not isinstance(value, str):
        raise PmcError("invalid_metadata", "PMC object URL is not a string")
    parsed = urlparse(value)
    if (parsed.scheme != "s3" or parsed.netloc != BUCKET
            or parsed.path != f"/{version_id}/{version_id}.{extension}" or parsed.fragment):
        raise PmcError("identity_mismatch", "PMC object URL does not match the declared article version")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {"md5"} or len(query.get("md5", [])) > 1:
        raise PmcError("invalid_metadata", "PMC object has unexpected query parameters")
    checksum = query.get("md5", [""])[0]
    if checksum and not re.fullmatch(r"[0-9a-fA-F]{32}", checksum):
        raise PmcError("invalid_metadata", "PMC object MD5 is invalid")
    url = urlunparse(("https", BUCKET_HOST, parsed.path, "", "", ""))
    if not is_public_https_url(url):
        raise PmcError("unsafe_url", "PMC object failed public HTTPS validation")
    return url, checksum.lower()


def _metadata_candidates(metadata: Any, pmcid: str, number: int) -> list[dict[str, Any]]:
    if not isinstance(metadata, dict):
        raise PmcError("invalid_metadata", "PMC version metadata is not an object")
    version_value = metadata.get("version")
    if isinstance(version_value, bool) or not re.fullmatch(r"[1-9]\d*", str(version_value)):
        raise PmcError("invalid_metadata", "PMC metadata version is not a positive integer")
    if normalize_pmcid(metadata.get("pmcid")) != pmcid or int(version_value) != number:
        raise PmcError("identity_mismatch", "PMC metadata does not match the listing identifier and version")
    retracted = _bool(metadata.get("is_retracted"))
    if retracted is not False:
        raise PmcError("no_oa_version", "PMC version is retracted or active status is unknown")
    oa = _bool(metadata.get("is_pmc_openaccess"))
    manuscript = _bool(metadata.get("is_manuscript"))
    license_value = metadata.get("license_code")
    if not isinstance(license_value, str) or not license_value.strip():
        raise PmcError("license_unknown", "PMC version has no explicit license code")
    license_value = license_value.strip()
    license_key = " ".join(license_value.upper().replace("_", " ").split())
    if oa is True and license_key not in {"TDM", "UNKNOWN", "NONE", "NULL", "N/A"}:
        route = "open_access"
    elif oa is False and manuscript is True and license_key == "TDM":
        route = "author_manuscript_tdm"
    else:
        raise PmcError("no_oa_version", "PMC version lacks explicit OA or author-manuscript TDM eligibility")
    version_id = f"{pmcid}.{number}"
    common = {"source": "pmc_oa_cloud", "license": license_value,
              "version": "acceptedVersion" if manuscript is True else "",
              "pmcid": pmcid, "article_version": number, "oa_route": route,
              "metadata_url": f"https://{BUCKET_HOST}/metadata/{version_id}.json",
              "is_manuscript": manuscript, "is_pmc_openaccess": oa}
    candidates: list[dict[str, Any]] = []
    # TDM permission applies to the text dataset, not to arbitrary PDF/media files.
    fields = [("pdf_url", "pdf", "pdf")] if route == "open_access" else []
    fields.append(("xml_url", "xml", "jats"))
    errors: list[PmcError] = []
    for field, extension, kind in fields:
        try:
            url, checksum = _object_candidate(metadata.get(field), version_id, extension)
            if url:
                candidate = {**common, "url": url, "kind": kind}
                if checksum:
                    candidate["expected_md5"] = checksum
                candidates.append(candidate)
        except PmcError as exc:
            errors.append(exc)
    if errors:
        if not candidates:
            raise errors[0]
        for candidate in candidates:
            candidate["resolver_warnings"] = [{"code": e.code, "message": str(e)} for e in errors]
    if not candidates:
        raise PmcError("no_fulltext_object", "Eligible PMC version has no declared usable PDF or JATS object")
    return candidates


def resolve_pmc_candidates(client: HttpClient, listing_url: str) -> list[dict[str, Any]]:
    """Read all bounded article versions and return eligible PDF/JATS candidates.

    One bad version does not hide valid alternatives. Partial version failures
    are preserved as resolver_warnings on returned candidates; if none survive,
    raise the diagnostic (prefer a deferred HTTP error) instead of an empty list.
    """
    pmcid, params = _listing_identity(listing_url)
    versions: set[int] = set()
    tokens: set[str] = set()
    current = listing_url
    for _ in range(MAX_LISTING_PAGES):
        root = _read_listing(client, current)
        if root.findtext("Prefix", f"{pmcid}.") != f"{pmcid}.":
            raise PmcError("identity_mismatch", "PMC listing prefix differs from requested identifier")
        for node in root.findall("./CommonPrefixes/Prefix"):
            match = re.fullmatch(r"(PMC[1-9]\d*)\.([1-9]\d*)/", (node.text or "").strip())
            if not match or match.group(1) != pmcid:
                raise PmcError("identity_mismatch", "PMC listing includes a different or malformed article version")
            versions.add(int(match.group(2)))
        if len(versions) > MAX_ARTICLE_VERSIONS:
            raise PmcError("listing_limit", "PMC article version count exceeded the bounded resolver limit")
        truncated = _bool(root.findtext("IsTruncated", "false"))
        if truncated is False:
            break
        token = root.findtext("NextContinuationToken", "")
        if truncated is None or not token or token in tokens:
            raise PmcError("invalid_metadata", "PMC listing pagination token is missing or repeated")
        tokens.add(token)
        params["continuation-token"] = token
        current = f"https://{BUCKET_HOST}/?{urlencode(params)}"
    else:
        raise PmcError("listing_limit", "PMC listing exceeded the bounded page limit")
    if not versions:
        raise PmcError("no_oa_version", "No current PMC Cloud version is available")
    candidates: list[dict[str, Any]] = []
    errors: list[Exception] = []
    for number in sorted(versions):
        metadata_url = f"https://{BUCKET_HOST}/metadata/{pmcid}.{number}.json"
        try:
            candidates.extend(_metadata_candidates(client.get_json(metadata_url), pmcid, number))
        except (HttpError, PmcError) as exc:
            errors.append(exc)
        except NETWORK_ERRORS as exc:
            errors.append(HttpError(f"PMC metadata read failed ({type(exc).__name__})", url=metadata_url,
                                    code="body_read_failed"))
    if not candidates:
        deferred = next((e for e in errors if isinstance(e, HttpError) and e.deferred), None)
        raise deferred or errors[0]
    if errors:
        warnings = [{"code": getattr(e, "code", "resolver_error"), "message": str(e),
                     "retry_at": getattr(e, "retry_at", None), "deferred": getattr(e, "deferred", False)}
                    for e in errors]
        for candidate in candidates:
            candidate.setdefault("resolver_warnings", []).extend(warnings)
    return candidates
