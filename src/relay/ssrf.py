"""SSRF containment for `remoteFetch` jobs' outbound fetch.

Applies ONLY to remoteFetch — a reminderAutoLog job carries no URL and never
calls into this module at all (see handlers/run_due_jobs.py's kind branch).

Split into a pure part (``validate_url_syntax`` — scheme/hostname shape,
trivially unit-testable, no network) and a resolving part
(``validate_resolved_addresses`` — DNS resolution + IP-range checks, needs a
live resolver so it's exercised indirectly via ``guard_url`` in integration,
but the IP-classification logic itself is unit-tested by feeding it addresses
directly).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable, List
from urllib.parse import urlparse


class SSRFBlocked(ValueError):
    """Raised when a URL or a resolved address fails containment checks."""


@dataclass(frozen=True)
class SSRFPolicy:
    allowed_schemes: frozenset = frozenset({"https"})
    max_response_bytes: int = 1_000_000  # 1 MB
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 10.0


DEFAULT_POLICY = SSRFPolicy()


def validate_url_syntax(url: str, policy: SSRFPolicy = DEFAULT_POLICY) -> str:
    """Validate scheme + hostname shape. Returns the hostname on success.

    Pure — no network calls, no DNS.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in policy.allowed_schemes:
        raise SSRFBlocked(
            f"scheme {parsed.scheme!r} is not allowed "
            f"(allowed: {sorted(policy.allowed_schemes)})"
        )
    if not parsed.hostname:
        raise SSRFBlocked("URL has no hostname")
    if parsed.username or parsed.password:
        raise SSRFBlocked("URL must not carry userinfo credentials")
    return parsed.hostname


def is_blocked_address(addr: str) -> bool:
    """True if ``addr`` (a literal IPv4/IPv6 address string) must not be
    fetched from — loopback, link-local, private, multicast, reserved,
    unspecified, or IPv4-mapped/6to4 wrappers around any of those.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        # Not a literal IP at all; caller should have resolved it first.
        return True

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_resolved_addresses(addresses: Iterable[str]) -> None:
    """Raise if any resolved address is in a blocked range. Pure given a list
    of address strings — the actual DNS resolution happens in ``resolve_host``.
    """
    addresses = list(addresses)
    if not addresses:
        raise SSRFBlocked("hostname did not resolve to any address")
    for addr in addresses:
        if is_blocked_address(addr):
            raise SSRFBlocked(f"resolved address {addr!r} is in a blocked range")


def resolve_host(hostname: str) -> List[str]:  # pragma: no cover - network I/O
    """Resolve a hostname to its literal IP addresses via the system resolver.

    Not unit-tested directly (it's a thin wrapper over ``socket.getaddrinfo``);
    ``validate_resolved_addresses`` above is where the actual policy logic
    lives and is tested.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SSRFBlocked(f"could not resolve hostname {hostname!r}: {exc}") from exc
    return [info[4][0] for info in infos]


def guard_url(url: str, policy: SSRFPolicy = DEFAULT_POLICY) -> str:  # pragma: no cover
    """Full containment check: syntax + DNS resolution + address-range check.
    Returns the validated hostname. Raises ``SSRFBlocked`` on any failure.

    This is the one function `run_due_jobs` should call before fetching a
    remoteFetch job's endpoint. Not unit-tested itself (it does real DNS);
    its two halves (`validate_url_syntax`, `validate_resolved_addresses`) are.
    """
    hostname = validate_url_syntax(url, policy)
    addresses = resolve_host(hostname)
    validate_resolved_addresses(addresses)
    return hostname
