"""Helpers for the M3 endpoint and webhook tests: fitted model versions, finished score
runs, and local webhook receivers (plain HTTP and HTTPS with a throwaway CA)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import ipaddress
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import apply_transition, load_run
from mapi_spec.run import Actor, RunTrigger

from .builders import TREND_SPEC, queued_fit, simulate_outputs, submit_fit
from .fake_executor import FakeExecutor

__all__ = [
    "Receiver",
    "TlsFiles",
    "finish_run",
    "fitted_version",
    "http_receiver",
    "make_tls",
    "refit_version",
]


async def finish_run(ctx: ServerContext, run_id: str, *, mode: str = "ok") -> m.Run:
    """Dispatch a QUEUED run on a fake executor, simulate its outputs and finalize it."""
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run.id, run.attempt))
        assert att is not None
    await simulate_outputs(ctx, run, att, mode=mode)
    await Finalizer(ctx).finalize(run.id, run.attempt)
    async with ctx.db.session() as s:
        return await load_run(s, None, run_id)


async def fitted_version(ctx: ServerContext, *, yaml: str = TREND_SPEC) -> m.ModelVersion:
    """A ready model version of the ``trend`` model (scorecard ``pass``)."""
    run = await finish_run(ctx, await queued_fit(ctx, yaml=yaml))
    return await _output(ctx, run)


async def refit_version(ctx: ServerContext, mv: m.ModelVersion) -> m.ModelVersion:
    """Another version of ``mv``'s model: a second fit of the same spec revision."""
    async with ctx.db.session() as s:
        fit = await s.get(m.Run, mv.run_id)
        assert fit is not None and fit.spec_id is not None
        spec_id = fit.spec_id
    run = await finish_run(ctx, await submit_fit(ctx, spec_id))
    return await _output(ctx, run)


async def _output(ctx: ServerContext, run: m.Run) -> m.ModelVersion:
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, run.output_model_version_id)
        assert mv is not None
        return mv


# ------------------------------------------------------------------------------- receivers


@dataclass
class TlsFiles:
    ca_pem: Path
    cert_pem: Path
    key_pem: Path

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert_pem, self.key_pem)
        return context

    def client_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=str(self.ca_pem))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context


def make_tls(tmp: Path, *names: str) -> TlsFiles:
    """A throwaway CA and a server certificate for ``names`` (written under ``tmp``)."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mapi test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = []
    for n in names:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            sans.append(x509.DNSName(n))
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    out = TlsFiles(tmp / "ca.pem", tmp / "cert.pem", tmp / "key.pem")
    out.ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    out.cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    out.key_pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return out


@dataclass
class Received:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Receiver:
    """A minimal HTTP/1.1 receiver: records requests and answers from ``responses``."""

    port: int = 0
    requests: list[Received] = field(default_factory=list)
    sni: list[str] = field(default_factory=list)
    responses: list[tuple[int, dict[str, str], bytes]] = field(default_factory=list)
    default: tuple[int, dict[str, str], bytes] = (200, {}, b'{"ok":true}')

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            self.requests.append(Received(method, target, headers, body))
            code, extra, payload = self.responses.pop(0) if self.responses else self.default
            reason = {200: "OK", 201: "Created", 302: "Found", 410: "Gone", 503: "Unavailable"}
            out = [f"HTTP/1.1 {code} {reason.get(code, 'X')}", f"Content-Length: {len(payload)}",
                   "Connection: close", *(f"{k}: {v}" for k, v in extra.items())]  # fmt: skip
            writer.write(("\r\n".join(out) + "\r\n\r\n").encode() + payload)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()


@contextlib.asynccontextmanager
async def http_receiver(tls: ssl.SSLContext | None = None) -> AsyncIterator[Receiver]:
    """Serve a :class:`Receiver` on 127.0.0.1 (HTTPS with ``tls``) on a free port."""
    rec = Receiver()
    if tls is not None:

        def remember(_sock: Any, name: str | None, _ctx: Any) -> None:
            rec.sni.append(name or "")

        tls.sni_callback = remember
    server = await asyncio.start_server(rec.handle, "127.0.0.1", 0, ssl=tls)
    rec.port = server.sockets[0].getsockname()[1]
    try:
        yield rec
    finally:
        server.close()
        await server.wait_closed()
