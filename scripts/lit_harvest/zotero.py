from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import secrets
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, build_opener

from .models import PaperRecord, normalize_doi, normalize_title
from .identity import PdfValidationError, verify_pdf_identity
from .downloader import sha256_file


class ZoteroError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ZoteroAttachmentError(ZoteroError):
    def __init__(self, message: str, attachment_key: str) -> None:
        super().__init__(message)
        self.attachment_key = attachment_key


@dataclass
class ZoteroStatus:
    reachable: bool
    api_version: str = ""
    server_id: str = ""
    schema_version: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def default_auth_cache() -> Path:
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "LiteratureHarvester" / "zotero-auth.json"
    return Path.home() / ".literature-harvester" / "zotero-auth.json"


def _protect_key(value: str) -> tuple[str, str]:
    if os.name != "nt":
        return "plain", value

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    raw = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    protected = DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "Literature Harvester Zotero key", None, None, None, 0, ctypes.byref(protected)
    ):
        raise OSError("Windows DPAPI could not protect the Zotero key")
    try:
        encrypted = ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(protected.pbData)
    return "dpapi", base64.b64encode(encrypted).decode("ascii")


def _unprotect_key(mode: str, value: str) -> str:
    if mode == "plain":
        return value
    if mode != "dpapi" or os.name != "nt":
        return ""

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    raw = base64.b64decode(value)
    buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    decrypted = DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(decrypted)
    ):
        return ""
    try:
        return ctypes.string_at(decrypted.pbData, decrypted.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(decrypted.pbData)


class ZoteroLocalClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:23119/api",
        timeout: int = 10,
        auth_cache: str | Path | None = None,
        opener: Any | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Zotero local API must use an unexposed loopback HTTP address")
        if parsed.port not in {None, 23119}:
            raise ValueError("Unexpected Zotero local API port")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.auth_cache = Path(auth_cache) if auth_cache else default_auth_cache()
        self.opener = opener or build_opener()
        self.server_id = ""
        self.api_version = ""
        self.api_key = ""
        self.auth_remembered = False

    def _url(self, path: str) -> str:
        return self.base_url + (path if path.startswith("/") else "/" + path)

    def _raw_request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, Any, int]:
        request_headers = {
            "User-Agent": "LiteratureHarvester/0.1",
            "Zotero-API-Version": "3",
            **(headers or {}),
        }
        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                response_server = response.headers.get("Zotero-Server-ID", "")
                if self.server_id and response_server and response_server != self.server_id:
                    raise ZoteroError("Zotero server identity changed during this operation")
                return response.read(), response.headers, response.status
        except HTTPError as exc:
            body = exc.read(4096).decode("utf-8", "replace")
            if exc.code == 401:
                self._clear_cached_auth()
            raise ZoteroError(f"Zotero HTTP {exc.code}: {body[:500]}", exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ZoteroError(f"Cannot reach Zotero local API: {exc}") from exc

    def _json_request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Any | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = False,
    ) -> tuple[Any, Any, int]:
        merged = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            merged["Content-Type"] = "application/json"
        if authenticated:
            if not self.server_id or not self.api_key:
                raise ZoteroError("Zotero write authorization is not available")
            merged["Zotero-Server-ID"] = self.server_id
            merged["Zotero-API-Key"] = self.api_key
        raw, response_headers, status = self._raw_request(
            self._url(path), method=method, data=data, headers=merged
        )
        if not raw:
            return None, response_headers, status
        try:
            return json.loads(raw.decode("utf-8")), response_headers, status
        except json.JSONDecodeError as exc:
            raise ZoteroError("Zotero returned invalid JSON") from exc

    def status(self) -> ZoteroStatus:
        try:
            raw, headers, _ = self._raw_request(self.base_url + "/", headers={"Accept": "application/json"})
        except ZoteroError as exc:
            return ZoteroStatus(False, message=str(exc))
        self.api_version = headers.get("Zotero-API-Version", "")
        self.server_id = headers.get("Zotero-Server-ID", "")
        return ZoteroStatus(
            True,
            api_version=self.api_version,
            server_id=self.server_id,
            schema_version=headers.get("Zotero-Schema-Version", ""),
            message="Zotero local API is reachable",
        )

    def _load_cached_auth(self) -> str:
        try:
            payload = json.loads(self.auth_cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if payload.get("server_id") == self.server_id and payload.get("key_data"):
            try:
                key = _unprotect_key(str(payload.get("key_mode", "")), str(payload["key_data"]))
            except (OSError, ValueError):
                key = ""
            if key:
                self.auth_remembered = True
                return key
        return ""

    def _save_cached_auth(self, key: str) -> None:
        self.auth_cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.auth_cache.with_suffix(".tmp")
        key_mode, key_data = _protect_key(key)
        temporary.write_text(
            json.dumps(
                {"server_id": self.server_id, "key_mode": key_mode, "key_data": key_data}, indent=2
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.auth_cache)

    def _clear_cached_auth(self) -> None:
        self.api_key = ""
        try:
            self.auth_cache.unlink(missing_ok=True)
        except OSError:
            pass

    def authorize(self, app_name: str = "Literature Harvester") -> bool:
        status = self.status()
        if not status.reachable:
            raise ZoteroError(status.message)
        payload, _, _ = self._json_request(
            "/local/authorize",
            method="POST",
            payload={"appName": app_name},
            headers={"Zotero-Server-ID": self.server_id},
        )
        self.api_key = str((payload or {}).get("key", ""))
        self.auth_remembered = bool((payload or {}).get("remember"))
        if not self.api_key:
            raise ZoteroError("Zotero did not grant a local API key")
        if self.auth_remembered:
            self._save_cached_auth(self.api_key)
        return self.auth_remembered

    def ensure_write_auth(self, require_persistent: bool = True) -> None:
        status = self.status()
        if not status.reachable:
            raise ZoteroError(status.message)
        self.api_key = self._load_cached_auth()
        if not self.api_key:
            self.authorize()
        if require_persistent and not self.auth_remembered:
            raise ZoteroError(
                "Multi-step import requires a persistent local key. Rerun and choose 'Always Allow' in Zotero."
            )

    def get_items(self) -> list[dict[str, Any]]:
        payload, _, _ = self._json_request("/users/0/items?format=json")
        return payload if isinstance(payload, list) else []

    def get_collections(self) -> list[dict[str, Any]]:
        payload, _, _ = self._json_request("/users/0/collections?format=json")
        return payload if isinstance(payload, list) else []

    def get_template(self, item_type: str, link_mode: str = "") -> dict[str, Any]:
        query = urlencode({key: value for key, value in {"itemType": item_type, "linkMode": link_mode}.items() if value})
        try:
            payload, _, _ = self._json_request(f"/items/new?{query}")
            if not isinstance(payload, dict):
                raise ZoteroError(f"No Zotero template for {item_type}")
            return payload
        except ZoteroError as exc:
            if exc.status != 404:
                raise

        # Some Zotero 10 local builds expose item-type/field metadata but not
        # the Web API's /items/new convenience endpoint. Construct the same
        # editable JSON shape from the supported local schema endpoints.
        if item_type == "attachment":
            return {
                "itemType": "attachment",
                "linkMode": link_mode or "imported_file",
                "title": "",
                "accessDate": "",
                "url": "",
                "note": "",
                "tags": [],
                "relations": {},
                "contentType": "",
                "charset": "",
                "filename": "",
                "md5": None,
                "mtime": None,
            }

        fields, _, _ = self._json_request(f"/itemTypeFields?{urlencode({'itemType': item_type})}")
        if not isinstance(fields, list):
            raise ZoteroError(f"No Zotero field schema for {item_type}")
        template: dict[str, Any] = {"itemType": item_type}
        for entry in fields:
            if isinstance(entry, dict) and entry.get("field"):
                template[str(entry["field"])] = ""
        try:
            creator_types, _, _ = self._json_request(
                f"/itemTypeCreatorTypes?{urlencode({'itemType': item_type})}"
            )
        except ZoteroError:
            creator_types = []
        if isinstance(creator_types, list) and creator_types:
            template["creators"] = []
        template.update({"tags": [], "collections": [], "relations": {}})
        return template

    def _write_json(self, path: str, payload: Any) -> Any:
        headers = {"Zotero-Write-Token": secrets.token_hex(16)}
        response, _, _ = self._json_request(
            path, method="POST", payload=payload, headers=headers, authenticated=True
        )
        return response

    def create_collection(self, name: str) -> str:
        response = self._write_json(
            "/users/0/collections", [{"name": name, "parentCollection": False}]
        )
        try:
            return str(response["success"]["0"])
        except (KeyError, TypeError) as exc:
            raise ZoteroError(f"Collection creation failed: {response}") from exc

    def create_item(self, data: dict[str, Any]) -> str:
        response = self._write_json("/users/0/items", [data])
        try:
            return str(response["success"]["0"])
        except (KeyError, TypeError) as exc:
            raise ZoteroError(f"Item creation failed: {response}") from exc

    def get_item(self, key: str) -> dict[str, Any]:
        payload, _, _ = self._json_request(f"/users/0/items/{key}")
        return payload if isinstance(payload, dict) else {}

    def _attachment_md5(self, key: str, parent_key: str) -> str:
        item = self.get_item(key)
        data = _item_data(item)
        if (_item_key(item) != key or data.get("itemType") != "attachment"
                or data.get("parentItem") != parent_key or data.get("linkMode") != "imported_file"):
            raise ZoteroError("Attachment readback does not match the exact child/parent relationship")
        return str(data.get("md5") or "").lower()

    def upload_attachment(
        self, parent_key: str, pdf_path: Path, existing_attachment_key: str = "",
        *, checkpoint: Callable[[str], None] | None = None,
    ) -> str:
        attachment_key = existing_attachment_key
        if not attachment_key:
            template = self.get_template("attachment", "imported_file")
            values = {
                "itemType": "attachment",
                "parentItem": parent_key,
                "linkMode": "imported_file",
                "title": pdf_path.name,
                "contentType": "application/pdf",
                "charset": "",
                "filename": pdf_path.name,
                "tags": [],
                "relations": {},
            }
            for key, value in values.items():
                if key in template or key in {"itemType", "parentItem", "linkMode", "tags", "relations"}:
                    template[key] = value
            attachment_key = self.create_item(template)

        try:
            # Persist the child before the multi-step upload, including failed readback.
            if checkpoint:
                checkpoint(attachment_key)
            pdf_bytes = pdf_path.read_bytes()
            md5 = hashlib.md5(pdf_bytes, usedforsecurity=False).hexdigest()
            stored_md5 = self._attachment_md5(attachment_key, parent_key)
            if stored_md5 == md5:
                # Registration may have completed before a previous reply was lost.
                return attachment_key
            if stored_md5:
                raise ZoteroError("Existing attachment contains a different file; automatic replacement refused")
            metadata = {
                "md5": md5,
                "filename": pdf_path.name,
                "filesize": len(pdf_bytes),
                "mtime": int(pdf_path.stat().st_mtime * 1000),
            }
            form = urlencode(metadata).encode("ascii")
            authorization, _, _ = self._json_request(
                f"/users/0/items/{attachment_key}/file",
                method="POST",
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"},
                authenticated=True,
            )
            if (authorization or {}).get("exists"):
                if self._attachment_md5(attachment_key, parent_key) != md5:
                    raise ZoteroError("Attachment exists reply is not confirmed by exact stored-file MD5")
                return attachment_key
            upload_key = str((authorization or {}).get("uploadKey", ""))
            upload_url = urljoin(self.base_url + "/", str((authorization or {}).get("url", "")))
            parsed = urlparse(upload_url)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port not in {None, 23119}:
                raise ZoteroError("Zotero returned a non-local upload URL")
            prefix = str((authorization or {}).get("prefix", "")).encode("utf-8")
            suffix = str((authorization or {}).get("suffix", "")).encode("utf-8")
            if not upload_key or not upload_url:
                raise ZoteroError(f"Invalid upload authorization: {authorization}")
            upload_body = prefix + pdf_bytes + suffix
            self._raw_request(
                upload_url,
                method="POST",
                data=upload_body,
                headers={"Content-Type": str((authorization or {}).get("contentType", "application/octet-stream")),
                         **({"Zotero-Server-ID": self.server_id} if self.server_id else {})},
            )
            register_form = urlencode({"upload": upload_key}).encode("ascii")
            self._json_request(
                f"/users/0/items/{attachment_key}/file",
                method="POST",
                data=register_form,
                headers={"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"},
                authenticated=True,
            )
            if self._attachment_md5(attachment_key, parent_key) != md5:
                raise ZoteroError("Attachment registration is not confirmed by exact stored-file MD5")
            return attachment_key
        except Exception as exc:
            if isinstance(exc, ZoteroAttachmentError):
                raise
            raise ZoteroAttachmentError(str(exc), attachment_key) from exc


def _item_data(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("data")
    return value if isinstance(value, dict) else item


def _item_doi(item: dict[str, Any]) -> str:
    data = _item_data(item)
    doi = normalize_doi(str(data.get("DOI", "")))
    if doi:
        return doi
    return normalize_doi(str(data.get("extra", "")))


def _item_key(item: dict[str, Any]) -> str:
    data = _item_data(item)
    return str(item.get("key") or data.get("key") or "")


_PARTIAL_IMPORT_STATUSES = {
    "parent_created", "parent_readback_failed", "parent_created_attachment_failed",
    "parent_created_attachment_unverified",
}


def _verify_parent(client: ZoteroLocalClient, key: str, record: PaperRecord) -> None:
    item = client.get_item(key)
    data = _item_data(item)
    if (_item_key(item) != key or not data.get("itemType")
            or data.get("itemType") in {"attachment", "note", "annotation"} or data.get("parentItem")):
        raise ZoteroError("Parent readback does not match the exact bibliographic item")
    doi = _item_doi(item)
    if record.doi and doi:
        if record.doi != doi:
            raise ZoteroError("Parent readback DOI conflicts with this record")
    elif not record.normalized_title or normalize_title(str(data.get("title", ""))) != record.normalized_title:
        raise ZoteroError("Parent readback title does not match this record")


def plan_import(records: list[PaperRecord], items: list[dict[str, Any]]) -> dict[str, int]:
    by_doi: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    searchable: list[tuple[str, dict[str, Any]]] = []
    pdf_attachments: dict[str, str] = {}
    parent_items: list[dict[str, Any]] = []
    for item in items:
        data = _item_data(item)
        if data.get("itemType") == "attachment":
            content_type = str(data.get("contentType", "")).casefold()
            filename = str(data.get("filename", "")).casefold()
            if data.get("parentItem") and (content_type == "application/pdf" or filename.endswith(".pdf")):
                pdf_attachments.setdefault(str(data["parentItem"]), _item_key(item))
            continue
        parent_items.append(item)
    for item in parent_items:
        data = _item_data(item)
        doi = _item_doi(item)
        title = normalize_title(str(data.get("title", "")))
        if doi:
            by_doi.setdefault(doi, item)
        if title:
            by_title.setdefault(title, item)
            searchable.append((title, item))

    counts: dict[str, int] = {}
    for record in records:
        previous = record.zotero.copy()
        if previous.get("item_key") and previous.get("status") in _PARTIAL_IMPORT_STATUSES:
            plan = {**previous, "action": "resume_attachment" if record.local_pdf else "resume_parent",
                    "match_reason": "manifest_partial_state"}
        elif not record.title:
            plan = {"action": "skip_no_metadata", "match_reason": "missing_title"}
        elif record.doi and record.doi in by_doi:
            item = by_doi[record.doi]
            key = _item_key(item)
            action = "attach_existing" if record.local_pdf and key not in pdf_attachments else "skip_exact"
            plan = {"action": action, "match_reason": "doi", "match_key": key, "item_key": key}
            if key in pdf_attachments:
                plan["attachment_key"] = pdf_attachments[key]
        elif record.normalized_title in by_title:
            item = by_title[record.normalized_title]
            key = _item_key(item)
            existing_doi = _item_doi(item)
            if record.doi and existing_doi and record.doi != existing_doi:
                plan = {"action": "manual_review", "match_reason": "doi_conflict", "match_key": key}
            else:
                action = "attach_existing" if record.local_pdf and key not in pdf_attachments else "skip_exact"
                plan = {"action": action, "match_reason": "normalized_title", "match_key": key, "item_key": key}
                if key in pdf_attachments:
                    plan["attachment_key"] = pdf_attachments[key]
        else:
            best_ratio = 0.0
            best_item: dict[str, Any] | None = None
            if len(record.normalized_title) >= 20:
                for title, item in searchable:
                    ratio = SequenceMatcher(None, record.normalized_title, title, autojunk=False).ratio()
                    if ratio > best_ratio:
                        best_ratio, best_item = ratio, item
            if best_item is not None and best_ratio >= 0.92:
                plan = {
                    "action": "manual_review",
                    "match_reason": "fuzzy_title",
                    "match_key": _item_key(best_item),
                    "similarity": round(best_ratio, 4),
                }
            else:
                plan = {"action": "create", "match_reason": "no_exact_match"}
        if plan["action"] == "skip_exact":
            prior_status = str(previous.get("status", ""))
            if prior_status not in {"imported_with_pdf", "attached_pdf_to_existing"}:
                plan["status"] = (
                    "verified_existing_with_pdf" if plan.get("attachment_key") else "verified_existing"
                )
            else:
                plan["status"] = prior_status
            plan["error"] = ""
        record.zotero = plan
        counts[plan["action"]] = counts.get(plan["action"], 0) + 1
    return counts


def find_collection_key(collections: list[dict[str, Any]], name: str) -> str:
    if not name:
        return ""
    matches = []
    for collection in collections:
        data = _item_data(collection)
        if str(data.get("name", "")).casefold() == name.casefold():
            matches.append(str(collection.get("key") or data.get("key") or ""))
    matches = [value for value in matches if value]
    if len(matches) > 1:
        raise ZoteroError(f"Multiple Zotero collections are named '{name}'; use a unique name")
    return matches[0] if matches else ""


def _record_to_item(
    client: ZoteroLocalClient,
    record: PaperRecord,
    collection_key: str,
    query: str,
) -> dict[str, Any]:
    requested_type = "preprint" if record.item_type == "preprint" else "journalArticle"
    template = client.get_template(requested_type)
    extra_lines = []
    for label, value in (
        ("PMID", record.pmid),
        ("PMCID", record.pmcid),
        ("arXiv", record.arxiv_id),
        ("OpenAlex", record.openalex_id),
        ("Semantic Scholar", record.semantic_scholar_id),
        ("Imported by", "Literature Harvester"),
    ):
        if value:
            extra_lines.append(f"{label}: {value}")
    values: dict[str, Any] = {
        "title": record.title,
        "creators": [{"creatorType": "author", "name": name} for name in record.authors],
        "abstractNote": record.abstract,
        "publicationTitle": record.journal,
        "date": str(record.year or ""),
        "DOI": record.doi,
        "url": record.url or (f"https://doi.org/{record.doi}" if record.doi else ""),
        "rights": record.license,
        "extra": "\n".join(extra_lines),
        "tags": [{"tag": "literature-harvester"}]
        + ([{"tag": query}] if query and len(query) <= 80 else []),
        "collections": [collection_key] if collection_key else [],
        "relations": {},
    }
    for key, value in values.items():
        if key in template or key in {"creators", "tags", "collections", "relations"}:
            template[key] = value
    return template


def import_records(
    client: ZoteroLocalClient,
    records: list[PaperRecord],
    *,
    query: str,
    collection_name: str = "",
    create_collection: bool = False,
    checkpoint: Callable[[list[PaperRecord]], None] | None = None,
) -> dict[str, int]:
    client.ensure_write_auth(require_persistent=True)
    items = client.get_items()
    collections = client.get_collections()
    plan_import(records, items)
    server_id = getattr(client, "server_id", "")
    server_id = server_id if isinstance(server_id, str) else ""
    for record in records:
        previous_server = record.zotero.get("server_id")
        if (record.zotero.get("action") in {"resume_attachment", "resume_parent"}
                and previous_server and previous_server != server_id):
            raise ZoteroError("Pending import belongs to another Zotero server; refusing to reuse its item keys")
    collection_key = find_collection_key(collections, collection_name)
    if collection_name and not collection_key:
        if not create_collection:
            raise ZoteroError(
                f"Collection '{collection_name}' does not exist; rerun with --create-collection after reviewing the plan"
            )
        collection_key = client.create_collection(collection_name)

    counts: dict[str, int] = {}
    for record in records:
        action = record.zotero.get("action")
        if action in {"skip_exact", "manual_review", "skip_no_metadata"}:
            status = str(action)
            record.zotero["status"] = status
            counts[status] = counts.get(status, 0) + 1
            continue
        if action in {"resume_attachment", "resume_parent"}:
            parent_key = str(record.zotero.get("item_key", ""))
        elif action == "attach_existing":
            parent_key = str(record.zotero.get("match_key", ""))
            record.zotero["item_key"] = parent_key
        elif action == "create":
            try:
                parent_key = client.create_item(_record_to_item(client, record, collection_key, query))
                record.zotero.update({"item_key": parent_key, "status": "parent_created", "server_id": server_id})
                if checkpoint:
                    checkpoint(records)
            except Exception as exc:
                record.zotero.update({"status": "item_create_failed", "error": str(exc)})
                counts["item_create_failed"] = counts.get("item_create_failed", 0) + 1
                if checkpoint:
                    checkpoint(records)
                continue
        else:
            continue

        record.zotero.update({"server_id": server_id, "parent_readback": "pending"})
        try:
            _verify_parent(client, parent_key, record)
            record.zotero.update({"parent_readback": "verified", "error": ""})
        except ZoteroError as exc:
            record.zotero.update({"status": "parent_readback_failed", "error": str(exc)})
            counts["parent_readback_failed"] = counts.get("parent_readback_failed", 0) + 1
            if checkpoint:
                checkpoint(records)
            continue

        pdf_path = Path(record.local_pdf) if record.local_pdf else None
        if pdf_path and pdf_path.is_file():
            try:
                identity = verify_pdf_identity(pdf_path, record)
                checksum = sha256_file(pdf_path)
                if identity.status != "verified" or (record.sha256 and checksum != record.sha256):
                    raise PdfValidationError("manual_review", "PDF identity or recorded checksum no longer agrees with this record")
                record.identity_status, record.sha256 = "verified", checksum
                record.extra["pdf_identity"] = identity.to_dict()
            except (PdfValidationError, OSError) as exc:
                record.identity_status = "manual_review"
                record.zotero.update({"status": "parent_created_attachment_unverified", "error": str(exc)})
                counts["parent_created_attachment_unverified"] = counts.get("parent_created_attachment_unverified", 0) + 1
                if checkpoint:
                    checkpoint(records)
                continue
            try:
                def attachment_checkpoint(key: str) -> None:
                    record.zotero.update({"attachment_key": key, "status": "parent_created",
                                          "attachment_readback": "pending"})
                    if checkpoint:
                        checkpoint(records)

                attachment_key = client.upload_attachment(
                    parent_key, pdf_path, str(record.zotero.get("attachment_key", "")),
                    checkpoint=attachment_checkpoint,
                )
                record.zotero.update(
                    {
                        "attachment_key": attachment_key,
                        "status": "attached_pdf_to_existing" if action == "attach_existing" else "imported_with_pdf",
                        "attachment_readback": "verified",
                        "error": "",
                    }
                )
                status = str(record.zotero["status"])
                counts[status] = counts.get(status, 0) + 1
            except ZoteroAttachmentError as exc:
                record.zotero.update(
                    {
                        "attachment_key": exc.attachment_key,
                        "status": "parent_created_attachment_failed",
                        "error": str(exc),
                    }
                )
                counts["parent_created_attachment_failed"] = counts.get(
                    "parent_created_attachment_failed", 0
                ) + 1
        else:
            record.zotero["status"] = "imported_without_pdf"
            counts["imported_without_pdf"] = counts.get("imported_without_pdf", 0) + 1
        if checkpoint:
            checkpoint(records)
    return counts
