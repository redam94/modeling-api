"""The webhook SSRF guard (plan §9): resolve every address, refuse any non-global one, pin
the checked address (SNI and Host keep the name), never follow redirects.

Resolution goes through a fake resolver, so these tests also model DNS rebinding (a second
lookup that answers differently). Real sockets are used against local receivers: plain HTTP
for an allow-listed development host, and HTTPS with a throwaway CA to show that the pinned
connection still verifies the certificate against the original name.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from mapi_server.services.webhooks.delivery import WebhookSender, excerpt
from mapi_server.services.webhooks.ssrf import (
    DeliveryTarget,
    ResolutionFailed,
    StaticResolver,
    UrlPolicy,
)
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.integrations import http_receiver, make_tls
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.webhooks import SIGNATURE_HEADER, signature_header, verify_signature

PUBLIC = "93.184.216.34"
POLICY = UrlPolicy()

BLOCKED_ANSWERS = [
    ["169.254.169.254"],  # metadata
    ["100.64.0.7"],  # CGNAT
    ["10.0.0.5"],
    ["127.0.0.1"],
    ["172.16.3.4"],
    ["192.168.1.1"],
    ["0.0.0.0"],
    ["fc00::1"],  # ULA
    ["fd12:3456::1"],
    ["fe80::1"],
    ["::1"],
    ["::ffff:127.0.0.1"],  # IPv4-mapped loopback
    ["::ffff:169.254.169.254"],
    ["64:ff9b::a9fe:a9fe"],  # NAT64 of 169.254.169.254
    ["2002:a9fe:a9fe::1"],  # 6to4 of 169.254.169.254
    ["224.0.0.1"],
    [PUBLIC, "10.0.0.5"],  # one bad answer refuses the host
]


@pytest.mark.parametrize("answers", BLOCKED_ANSWERS, ids=lambda a: "+".join(a))
async def test_every_non_global_address_is_refused(answers: list[str]) -> None:
    resolver = StaticResolver({"hooks.example.com": answers})
    with pytest.raises(MapiError) as err:
        await POLICY.resolve("https://hooks.example.com/mapi", resolver)
    assert err.value.code is ErrorCode.WEBHOOK_URL_BLOCKED


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",  # https only
        "https://user:pw@hooks.example.com/x",
        "https://hooks.example.com:22/x",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/x",
        "https://localhost/x",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://printer.local/x",
        "https://hooks.example.com/x#frag",
        "https://intranet/x",
        "https://0x7f000001/x",
        "https://hooks.example.com/a b",
    ],
)
async def test_static_checks_refuse_before_any_lookup(url: str) -> None:
    resolver = StaticResolver({"hooks.example.com": [PUBLIC]})
    with pytest.raises(MapiError) as err:
        await POLICY.resolve(url, resolver)
    assert err.value.code is ErrorCode.WEBHOOK_URL_BLOCKED
    assert resolver.calls == []


async def test_a_public_host_is_pinned_to_the_checked_address() -> None:
    resolver = StaticResolver({"hooks.example.com": [PUBLIC, "2606:2800:220:1::1"]})
    target = await POLICY.resolve("https://hooks.example.com:8443/in?x=1", resolver)
    assert target.address == PUBLIC
    assert target.connect_url == f"https://{PUBLIC}:8443/in?x=1"
    assert target.host_header == "hooks.example.com:8443"
    v6 = DeliveryTarget("u", "https", "h.example.com", 443, "/", "2606:2800:220:1::1")
    assert v6.connect_url == "https://[2606:2800:220:1::1]:443/"
    assert v6.host_header == "h.example.com"


async def test_unresolvable_hosts_are_a_transient_failure() -> None:
    with pytest.raises(ResolutionFailed):
        await POLICY.resolve("https://nowhere.example.com/x", StaticResolver())


def test_dev_exemptions_are_refused_in_the_multi_tenant_profile() -> None:
    with pytest.raises(ValueError, match="MAPI_WEBHOOK_ALLOW_HTTP"):
        Settings(profile=Profile.MULTI_TENANT, webhook_allow_http=True)
    with pytest.raises(ValueError, match="MAPI_WEBHOOK_DEV_ALLOWLIST"):
        Settings(profile=Profile.MULTI_TENANT, webhook_dev_allowlist="localhost")
    dev = Settings(webhook_allow_http=True, webhook_dev_allowlist="localhost, hooks.test")
    policy = UrlPolicy.from_settings(dev)
    assert policy.allow_http and policy.dev_allowlist == {"localhost", "hooks.test"}


async def test_the_dev_allowlist_admits_named_hosts_only() -> None:
    policy = UrlPolicy(allow_http=True, dev_allowlist=frozenset({"localhost"}))
    resolver = StaticResolver({"localhost": ["127.0.0.1"], "other.test": ["127.0.0.1"]})
    target = await policy.resolve("http://localhost:9999/hook", resolver)
    assert (target.address, target.allowlisted, target.port) == ("127.0.0.1", True, 9999)
    with pytest.raises(MapiError):  # not allow-listed: the full policy applies
        await policy.resolve("http://other.test:9999/hook", resolver)
    with pytest.raises(MapiError):  # credentials are refused even for allow-listed hosts
        await policy.resolve("http://u:p@localhost:9999/hook", resolver)
    https_only = UrlPolicy(dev_allowlist=frozenset({"localhost"}))
    with pytest.raises(MapiError, match="https"):
        await https_only.resolve("http://localhost:9999/hook", resolver)


# ------------------------------------------------------------------- pinned connections


class Capture(httpx.AsyncBaseTransport):
    """Answers every request with ``status`` and records what was sent."""

    def __init__(self, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, headers=self.headers, stream=httpx.ByteStream(b"x" * 10))


async def test_dns_rebinding_cannot_redirect_a_checked_delivery() -> None:
    """The first lookup answers a public address, the second a private one (rebinding).
    Each try resolves exactly once and connects to the address it checked; the try whose
    lookup turns private is refused without any connection."""
    resolver = StaticResolver({"hooks.example.com": [[PUBLIC], ["169.254.169.254"]]})
    transport = Capture()
    sender = WebhookSender(POLICY, resolver=resolver, transport=transport)
    first = await sender.send("https://hooks.example.com/in", b"{}", {"X-Test": "1"})
    assert first.ok and first.target is not None
    sent = transport.requests[0]
    assert sent.url.host == PUBLIC  # the pinned address, not a second lookup
    assert sent.headers["host"] == "hooks.example.com"
    assert sent.extensions["sni_hostname"] == "hooks.example.com"
    assert sent.headers["user-agent"] == "mapi-webhooks/1"
    assert resolver.calls == ["hooks.example.com"]
    second = await sender.send("https://hooks.example.com/in", b"{}", {})
    assert second.blocked and "private" in (second.error or "")
    assert len(transport.requests) == 1  # nothing was sent to the rebound address


async def test_redirects_are_never_followed() -> None:
    resolver = StaticResolver({"hooks.example.com": [PUBLIC]})
    transport = Capture(302, {"Location": "http://169.254.169.254/latest/meta-data"})
    sender = WebhookSender(POLICY, resolver=resolver, transport=transport)
    result = await sender.send("https://hooks.example.com/in", b"{}", {})
    assert result.status == 302 and not result.ok
    assert "redirects are not followed" in (result.error or "")
    assert len(transport.requests) == 1


async def test_answers_are_read_up_to_the_cap_and_shown_as_text() -> None:
    assert excerpt(b"") is None
    assert excerpt(b"ok\x1b[31m\x00done") == "ok [31m done"
    assert len(excerpt(b"a" * 100_000) or "") == 1024


async def test_plain_http_to_an_allowlisted_receiver(tmp_path: Path) -> None:
    async with http_receiver() as rec:
        policy = UrlPolicy(allow_http=True, dev_allowlist=frozenset({"hooks.test"}))
        sender = WebhookSender(policy, resolver=StaticResolver({"hooks.test": ["127.0.0.1"]}))
        body = b'{"id":"evt_1"}'
        sig = signature_header(body, ["whsec_" + "a" * 43])
        result = await sender.send(
            f"http://hooks.test:{rec.port}/hook?k=1", body, {SIGNATURE_HEADER: sig}
        )
    assert result.ok, result.error
    got = rec.requests[0]
    assert got.method == "POST" and got.target == "/hook?k=1"
    assert got.headers["host"] == f"hooks.test:{rec.port}"
    assert verify_signature(got.body, got.headers["mapi-signature"], "whsec_" + "a" * 43)


async def test_https_pins_the_address_and_verifies_the_original_name(tmp_path: Path) -> None:
    tls = make_tls(tmp_path, "hooks.test")
    resolver = StaticResolver({"hooks.test": ["127.0.0.1"], "evil.test": ["127.0.0.1"]})
    policy = UrlPolicy(dev_allowlist=frozenset({"hooks.test", "evil.test"}))
    sender = WebhookSender(policy, resolver=resolver, ssl_context=tls.client_context())
    async with http_receiver(tls.server_context()) as rec:
        ok = await sender.send(f"https://hooks.test:{rec.port}/in", b"{}", {})
        assert ok.ok, ok.error
        assert rec.sni[-1] == "hooks.test"  # SNI is the name, though we dialled 127.0.0.1
        assert rec.requests[0].headers["host"] == f"hooks.test:{rec.port}"
        # the same address under another name: the certificate does not match
        bad = await sender.send(f"https://evil.test:{rec.port}/in", b"{}", {})
    assert not bad.ok and bad.status is None
    assert "TLS" in (bad.error or "") or "connect" in (bad.error or "")
    assert len(rec.requests) == 1


async def test_a_receiver_that_does_not_answer_times_out() -> None:
    import asyncio

    async def hang(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(5)

    server = await asyncio.start_server(hang, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        from dataclasses import replace

        from mapi_spec.webhooks import SSRF_POLICY

        quick = UrlPolicy(
            policy=replace(SSRF_POLICY, timeout_s=0.2),
            allow_http=True,
            dev_allowlist=frozenset({"slow.test"}),
        )
        sender = WebhookSender(quick, resolver=StaticResolver({"slow.test": ["127.0.0.1"]}))
        result = await sender.send(f"http://slow.test:{port}/", b"{}", {})
    finally:
        server.close()
    assert result.status is None and "in time" in (result.error or "")
