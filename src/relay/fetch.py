"""Bounded, SSRF-guarded outbound fetch for `remoteFetch` jobs.

Wraps :mod:`relay.ssrf`'s checks around a real HTTP GET: validates the URL
before every hop (redirects are not followed blindly — each `Location` is
re-validated from scratch, since a same-origin-looking redirect can still
point at a private address), caps the response body size, and enforces a
timeout.

This module does real network I/O and is intentionally not covered by the
unit test suite (nothing to fake here that wouldn't just be testing the
standard library) — it is scaffolded, not verified end-to-end. The pure
pieces it delegates to (``ssrf.validate_url_syntax`` /
``ssrf.validate_resolved_addresses``, ``extraction.extract_number``) are
unit-tested directly.

Known limitation, documented rather than fixed in this pass: there is a
TOCTOU gap between resolving+validating a hostname and the underlying
`urllib`/`http.client` connection performing its own resolution — a DNS
answer could theoretically change between the two (classic "DNS rebinding").
Closing that fully means connecting to a pinned IP with SNI/hostname
verification done manually; left as a follow-up (see README).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from .ssrf import DEFAULT_POLICY, SSRFBlocked, SSRFPolicy, resolve_host, validate_resolved_addresses, validate_url_syntax


class FetchError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never auto-follow; caller re-validates and re-requests


@dataclass(frozen=True)
class FetchResult:
    status: int
    body: bytes


def _fetch_once(url: str, policy: SSRFPolicy) -> FetchResult:
    hostname = validate_url_syntax(url, policy)
    validate_resolved_addresses(resolve_host(hostname))

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=policy.connect_timeout_seconds) as response:
            status = response.status
            location = response.headers.get("Location") if status in (301, 302, 303, 307, 308) else None
            body = response.read(policy.max_response_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            location = exc.headers.get("Location")
            return FetchResult(status=exc.code, body=(location or "").encode())
        raise FetchError(f"HTTP error fetching {url!r}: {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"error fetching {url!r}: {exc.reason}") from exc

    if location:
        return FetchResult(status=status, body=location.encode())
    if len(body) > policy.max_response_bytes:
        raise FetchError(
            f"response from {url!r} exceeded the {policy.max_response_bytes}-byte cap"
        )
    return FetchResult(status=status, body=body)


def fetch_json(
    url: str, *, policy: SSRFPolicy = DEFAULT_POLICY, max_redirects: int = 3
) -> Any:
    """GET ``url`` and parse the response body as JSON, enforcing the SSRF
    policy on every hop. Raises ``SSRFBlocked``/``FetchError`` on failure.
    """
    current = url
    for _ in range(max_redirects + 1):
        result = _fetch_once(current, policy)
        if result.status in (301, 302, 303, 307, 308):
            current = result.body.decode()
            continue
        try:
            return json.loads(result.body)
        except json.JSONDecodeError as exc:
            raise FetchError(f"response from {url!r} was not valid JSON: {exc}") from exc
    raise FetchError(f"too many redirects fetching {url!r} (max {max_redirects})")
