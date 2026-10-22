"""Explicit opt-in OpenAlex PDF delivery; never infer a free-only guarantee."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlencode
from .http import HttpClient, HttpError
from .settings import client_options, section

UNIT_ESTIMATE_USD = Decimal("0.01")
PRICING_SOURCE = "https://help.openalex.org/access/example-costs/"


def content_policy(*, enabled=False, free_only=True, max_files=0, api_key_present=False,
                   allow_paid=False, max_cost_usd=0):
    if not enabled:
        return {"status": "disabled", "reason": "OpenAlex content delivery is disabled by default"}
    if free_only:
        return {"status": "blocked", "reason": "No verified server-enforced free-only guard; free allowance is not a zero-charge guarantee",
                "free_only": True, "server_spend_guard_verified": False}
    if not allow_paid:
        return {"status": "blocked", "reason": "Paid delivery needs explicit allow_paid authorization"}
    try:
        cost = Decimal(str(max_cost_usd))
        if not cost.is_finite() or cost < UNIT_ESTIMATE_USD:
            raise ValueError
    except (InvalidOperation, ValueError):
        return {"status": "blocked", "reason": "A finite positive estimated cost budget is required"}
    if type(max_files) is not int or max_files <= 0 or not api_key_present:
        return {"status": "blocked", "reason": "An API key and positive max_files request cap are required"}
    return {"status": "enabled", "reason": "Explicitly authorized metered PDF route",
        "max_files": min(max_files, int(cost / UNIT_ESTIMATE_USD)), "estimated_unit_cost_usd": str(UNIT_ESTIMATE_USD),
        "estimated_cost_cap_usd": str(cost), "pricing_source": PRICING_SOURCE,
        "pricing_checked": "2026-09-25", "server_spend_guard_verified": False,
        "billing_note": "Request cap is enforced locally; actual billing and rate changes are controlled by the provider"}


class ContentBudget:
    def __init__(self, config, api_key, directory):
        self.policy = content_policy(**section(config, "openalex_content"), api_key_present=bool(api_key))
        self._api_key = api_key
        self.path = Path(directory) / "metered-requests.json"
        self._lock = threading.Lock()
        self.requests = []
        self.client = HttpClient(**{**client_options(config), "max_retries": 0})

    def request(self, url, **kwargs):
        if not re.fullmatch(r"https://content\.openalex\.org/works/W[1-9]\d*\.pdf", url):
            raise HttpError("Not a declared official OpenAlex PDF URL", code="unsafe_url")
        with self._lock:
            if self.policy["status"] != "enabled":
                raise HttpError(self.policy["reason"], code="metered_not_authorized")
            if len(self.requests) >= self.policy["max_files"]:
                raise HttpError("Authorized metered request budget exhausted", code="metered_budget_exhausted")
            # Reserve before I/O, including uncertain failures. Do not refund or
            # automatically retry a request that could already have been charged.
            self.requests.append({"url": url, "state": "reserved_may_have_been_charged"})
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"policy": self.policy, "requests": self.requests}, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        return self.client.request(url + "?" + urlencode({"api_key": self._api_key}), **kwargs)


def bind_budget(config, env, directory):
    values = section(config, "openalex_content")
    if not values["enabled"]:
        return config
    return {**config, "_content_budget": ContentBudget(config, env.get("OPENALEX_API_KEY", ""), directory)}
