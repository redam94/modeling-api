"""Where a webhook delivery may connect: the SSRF guard (plan §9 "Webhook SSRF").

:mod:`mapi_spec.webhooks` holds the policy as data and the pure checks; this module applies
them around a real DNS lookup:

1. **static checks** of the URL (:func:`mapi_spec.webhooks.check_webhook_url`): ``https``,
   allowed ports, no credentials, no IP literals, no internal names;
2. **resolve** the host *once* (every A and AAAA answer);
3. **check every address** (:func:`mapi_spec.webhooks.check_resolved_addresses`): one
   private, loopback, link-local (``169.254.169.254``), CGNAT (``100.64.0.0/10``), ULA
   (``fc00::/7``), IPv4-mapped (``::ffff:…``) or otherwise non-global answer refuses the host;
4. **pin** the checked address: the sender connects to that IP, with SNI and ``Host`` set to
   the original name (certificate checked against it), so a second, different DNS answer
   (rebinding) is never used. Redirects are never followed.

Development installs can exempt named hosts (``MAPI_WEBHOOK_DEV_ALLOWLIST``) from the
address and port checks, and allow ``http`` for them only (``MAPI_WEBHOOK_ALLOW_HTTP``);
the multi-tenant profile refuses both settings.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.webhooks import (
    SSRF_POLICY,
    SsrfPolicy,
    check_resolved_addresses,
    check_webhook_url,
)

from mapi_server.settings import Settings

__all__ = [
    "DeliveryTarget",
    "ResolutionFailed",
    "Resolver",
    "StaticResolver",
    "SystemResolver",
    "UrlPolicy",
    "blocked",
    "describe_host",
]


def blocked(reason: str) -> MapiError:
    return MapiError(reason, code=ErrorCode.WEBHOOK_URL_BLOCKED, pointer="/url")


class ResolutionFailed(Exception):
    """The host did not resolve (NXDOMAIN, timeout): a transient delivery failure."""


class Resolver(Protocol):
    """Resolves a host to every address it has (A and AAAA), in answer order."""

    async def resolve(self, host: str, port: int) -> list[str]: ...


class SystemResolver:
    """The operating system's resolver (``getaddrinfo``), bounded by a timeout."""

    def __init__(self, timeout_s: float = SSRF_POLICY.timeout_s) -> None:
        self.timeout_s = timeout_s

    async def resolve(self, host: str, port: int) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP),
                self.timeout_s,
            )
        except TimeoutError:
            raise ResolutionFailed("the host did not answer in time (DNS)") from None
        except (OSError, UnicodeError):
            # plain words for the people who read it (the web app shows it at the URL field);
            # the exception class (gaierror, ...) means nothing to them
            raise ResolutionFailed("the host did not resolve: no such name") from None
        out: list[str] = []
        for _family, _type, _proto, _canon, sockaddr in infos:
            addr = str(sockaddr[0])
            if addr not in out:
                out.append(addr)
        return out


@dataclass
class StaticResolver:
    """A resolver from a fixed table (tests and air-gapped development).

    A value that is a list of lists answers each lookup with the next list (the last one
    repeats), which is how tests model DNS rebinding.
    """

    table: dict[str, list[str] | list[list[str]]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    _seen: dict[str, int] = field(default_factory=dict)

    async def resolve(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        answers = self.table.get(host)
        if answers is None:
            raise ResolutionFailed("the host did not resolve (NXDOMAIN)")
        if answers and isinstance(answers[0], list):
            n = self._seen.get(host, 0)
            self._seen[host] = n + 1
            seq = [list(a) for a in answers if isinstance(a, list)]
            return list(seq[min(n, len(seq) - 1)])
        return [str(a) for a in answers]


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """A checked URL and the address the sender must connect to."""

    url: str
    scheme: str
    host: str
    port: int
    target: str
    """Path and query (``/hooks?x=1``)."""
    address: str
    """The pinned IP (checked against the policy unless ``allowlisted``)."""
    allowlisted: bool = False

    @property
    def default_port(self) -> int:
        return 443 if self.scheme == "https" else 80

    @property
    def host_header(self) -> str:
        return self.host if self.port == self.default_port else f"{self.host}:{self.port}"

    @property
    def connect_url(self) -> str:
        """The URL naming the pinned address (IPv6 in brackets)."""
        ip = ipaddress.ip_address(self.address)
        literal = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
        return f"{self.scheme}://{literal}:{self.port}{self.target}"


@dataclass(frozen=True, slots=True)
class UrlPolicy:
    """The SSRF policy plus the development exemptions of this deployment."""

    policy: SsrfPolicy = SSRF_POLICY
    allow_http: bool = False
    dev_allowlist: frozenset[str] = frozenset()

    @classmethod
    def from_settings(cls, settings: Settings) -> UrlPolicy:
        return cls(
            allow_http=settings.webhook_allow_http,
            dev_allowlist=frozenset(h.strip().lower() for h in settings.webhook_dev_allowlist),
        )

    def _allowlisted(self, host: str, port: int) -> bool:
        return host in self.dev_allowlist or f"{host}:{port}" in self.dev_allowlist

    def check(self, url: str) -> tuple[SplitResult, str, int, bool]:
        """Static checks; returns ``(parts, host, port, allowlisted)`` or raises
        ``WEBHOOK_URL_BLOCKED``."""
        p = self.policy
        if len(url) > p.max_url_length or any(c.isspace() or ord(c) < 32 for c in url):
            raise blocked("the URL is too long or contains spaces or control characters")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise blocked("the URL cannot be parsed") from None
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").rstrip(".").lower()
        port = port or (443 if scheme == "https" else 80)
        if host and self._allowlisted(host, port):
            schemes = ("https", "http") if self.allow_http else ("https",)
            if scheme not in schemes:
                raise blocked(f"webhook URLs must use {' or '.join(schemes)}")
            if parts.username is not None or parts.password is not None or "@" in parts.netloc:
                raise blocked("webhook URLs cannot contain credentials")
            if parts.fragment:
                raise blocked("webhook URLs cannot contain a fragment")
            return parts, host, port, True
        if scheme == "http":
            raise blocked("webhook URLs must use https")
        host = check_webhook_url(url, p)
        return parts, host, port, False

    async def resolve(self, url: str, resolver: Resolver) -> DeliveryTarget:
        """Static checks, one resolution, the address checks: the target to connect to.

        Raises ``WEBHOOK_URL_BLOCKED`` (never retried) or :class:`ResolutionFailed`.
        """
        parts, host, port, allowlisted = self.check(url)
        addresses = await resolver.resolve(host, port)
        if not addresses:
            raise ResolutionFailed("the host did not resolve (no addresses)")
        if allowlisted:
            pinned = addresses[0]
            try:
                ipaddress.ip_address(pinned)
            except ValueError:
                raise blocked("the host resolved to something that is not an address") from None
        else:
            pinned = check_resolved_addresses(addresses, self.policy)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        return DeliveryTarget(
            url=url,
            scheme=parts.scheme.lower(),
            host=host,
            port=port,
            target=target,
            address=pinned,
            allowlisted=allowlisted,
        )


def describe_host(url: str) -> str:
    """The host of ``url`` for logs and audit details (never the path or query)."""
    try:
        return (urlsplit(url).hostname or "")[:253]
    except ValueError:
        return ""
