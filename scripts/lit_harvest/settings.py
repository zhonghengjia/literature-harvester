"""One authority for configurable execution budgets, not provider entitlements."""
from __future__ import annotations

from .http import HttpClient

DEFAULT_CONFIG = {
    "general": {"output_root": "~/Documents/Literature", "max_results": 25,
                "max_candidates": 500, "max_pdf_mb": 100,
                "request_timeout_seconds": 30, "contact_email": ""},
    "transport": {"max_retries": 2, "max_inline_retry_delay": 10,
                  "max_retry_wait": 30, "body_timeout_seconds": 90, "ca_file": ""},
    "resources": {"json_bytes": 16 * 1024**2, "html_bytes": 4 * 1024**2,
                  "xml_bytes": 32 * 1024**2, "asset_bytes": 100 * 1024**2,
                  "pmc_listing_bytes": 1024**2, "pmc_listing_pages": 100,
                  "pmc_versions": 1000},
    "search": {"workers": 5, "max_records": 100000, "preprint_candidates": 25,
               "core_candidates": 50, "supplemental_candidates": 25},
    "discovery": {"additive_default": True, "additional_per_source": 50},
    "plan_limits": {"concepts": 100, "terms": 100, "routes": 100,
                    "route_records": 100000, "total_records": 100000,
                    "text_chars": 50000, "term_chars": 1000},
    "content": {"batch_size": 25, "max_records": 0, "max_seconds": 0,
                "page_batch_size": 500, "max_pages": 0, "ocr": "auto",
                "ocr_language": "eng", "ocr_dpi": 150,
                "asset_limit": 25, "fetch_assets": True, "cache_dir": ""},
    "reading": {"max_chars": 24000, "chunk_chars": 4000,
                "question_chars": 50000},
    "sources": {"pubmed": True, "crossref": True, "europe_pmc": True,
                "openalex": True, "semantic_scholar": True, "arxiv": True,
                "unpaywall": True, "biorxiv_medrxiv": True,
                "preprint_title_match": True, "core": True,
                "openaire": True, "doaj": True},
    "openalex_content": {"enabled": False, "free_only": True, "max_files": 0,
                         "allow_paid": False, "max_cost_usd": 0},
    "zotero": {"base_url": "http://127.0.0.1:23119/api", "collection": "",
               "attachment_mode": "imported_file", "timeout_seconds": 30},
}


def positive(value, name, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError(f"{name} must be {'nonnegative' if zero else 'positive'} integer")
    return value


def section(config, name):
    return {**DEFAULT_CONFIG.get(name, {}), **config.get(name, {})}


def client_options(config):
    transport = section(config, "transport")
    return {"timeout": section(config, "general")["request_timeout_seconds"],
            "max_retries": transport["max_retries"],
            "max_inline_retry_delay": transport["max_inline_retry_delay"],
            "max_retry_wait": transport["max_retry_wait"],
            "ca_file": transport["ca_file"] or None,
            "resource_limits": section(config, "resources")}


def validate_config(config):
    """Reject malformed known settings without printing private values."""
    nonnegative = {("transport", "max_retries"), ("transport", "max_inline_retry_delay"),
        ("transport", "max_retry_wait"), ("content", "max_records"), ("content", "max_seconds"),
        ("content", "max_pages"), ("content", "asset_limit"), ("openalex_content", "max_files")}
    for name, defaults in DEFAULT_CONFIG.items():
        if not isinstance(config.get(name, {}), dict):
            raise ValueError(f"Configuration section {name} must be a table")
        for key, default in defaults.items():
            value = config.get(name, {}).get(key, default)
            label = name + "." + key
            if key == "max_cost_usd":
                from decimal import Decimal, InvalidOperation
                try:
                    cost = Decimal(str(value))
                    if not cost.is_finite() or cost < 0:
                        raise ValueError
                except (ValueError, InvalidOperation):
                    raise ValueError(label + " must be finite and nonnegative") from None
            elif type(default) is bool and type(value) is not bool:
                raise ValueError(label + " must be boolean")
            elif type(default) is int:
                positive(value, label, zero=(name, key) in nonnegative)
            elif isinstance(default, str) and not isinstance(value, str):
                raise ValueError(label + " must be text")
    if section(config, "content")["ocr"] not in {"off", "auto"}:
        raise ValueError("content.ocr must be off or auto")
    return config


def make_client(config, **overrides):
    return HttpClient(**{**client_options(config), **overrides})


def resource_limit(client, name):
    values = getattr(client, "resource_limits", {})
    values = values if isinstance(values, dict) else {}
    return positive(values.get(name, DEFAULT_CONFIG["resources"][name]), "resources." + name)


def effective_settings(config):
    """Only public, explicitly enumerated budget values; never arbitrary config."""
    result = {name: {key: config.get(name, {}).get(key, default)
                     for key, default in values.items() if key not in {"contact_email", "ca_file"}}
              for name, values in DEFAULT_CONFIG.items()}
    result["configuration_source"] = config.get("_configuration_source", "built_in")
    return result
