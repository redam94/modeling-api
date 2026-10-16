"""The caller's address behind trusted proxies (``MAPI_TRUSTED_PROXIES``).

Every per-address control (sign-up and verification limits, the embed view limit, the Sybil
``same_ip`` heuristic, audit ``ip_hash``) reads ``request.client.host``. Behind a load
balancer or the web container's ``/v1/`` proxy that is the proxy's address, the same for
every caller, so those controls would act platform-wide. :class:`ClientAddressMiddleware`
rewrites the ASGI ``client`` once, before anything reads it:

* the peer is not a trusted proxy: it is the client, and its ``X-Forwarded-For`` is ignored
  (anyone can send one);
* the peer is a trusted proxy: walk ``X-Forwarded-For`` from the right, skipping trusted
  hops; the first untrusted hop is the client. Entries left of it were supplied by the
  client and are never used. A malformed entry stops the walk at the last trusted hop.

Only ``X-Forwarded-For`` is read (``X-Forwarded-Proto``/``-Host`` are not trusted for
anything).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from typing import Final

from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = [
    "ClientAddressMiddleware",
    "Network",
    "client_address",
    "normalise_trusted_proxies",
    "parse_trusted_proxies",
]

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

_XFF: Final = b"x-forwarded-for"


def normalise_trusted_proxies(values: Iterable[str]) -> list[str]:
    """Canonical CIDR strings (``192.0.2.1`` -> ``192.0.2.1/32``); ``ValueError`` names junk."""
    out: list[str] = []
    for raw in values:
        v = raw.strip()
        if not v:
            continue
        try:
            out.append(str(ipaddress.ip_network(v, strict=False)))
        except ValueError:
            raise ValueError(f"trusted proxy {v!r} is not an address or CIDR range") from None
    return out


def parse_trusted_proxies(values: Iterable[str]) -> list[Network]:
    return [ipaddress.ip_network(v, strict=False) for v in normalise_trusted_proxies(values)]


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _trusted(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, nets: Sequence[Network]) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in nets)


def client_address(
    peer: str | None, forwarded_for: Sequence[str], trusted: Sequence[Network]
) -> str | None:
    """The caller's address: ``peer`` unless it is a trusted proxy (see the module doc)."""
    if peer is None:
        return None
    peer_ip = _ip(peer)
    if peer_ip is None or not trusted or not _trusted(peer_ip, trusted):
        return peer
    hops = [h for header in forwarded_for for h in header.split(",")]
    current = str(peer_ip)
    for hop in reversed(hops):
        ip = _ip(hop)
        if ip is None:
            return current
        current = str(ip)
        if not _trusted(ip, trusted):
            return current
    return current


class ClientAddressMiddleware:
    """Replace the ASGI ``client`` host with :func:`client_address` (outermost middleware)."""

    def __init__(self, app: ASGIApp, *, trusted: Sequence[Network]) -> None:
        self.app = app
        self.trusted = list(trusted)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and self.trusted and scope.get("client"):
            host, port = scope["client"]
            values = [v.decode("latin-1") for k, v in scope.get("headers", ()) if k.lower() == _XFF]
            resolved = client_address(host, values, self.trusted)
            if resolved != host:
                scope = {**scope, "client": (resolved, port)}
        await self.app(scope, receive, send)
