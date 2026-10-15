"""V4 signing (``GOOG4-HMAC-SHA256``): query-signed URLs and POST policies.

This follows the Cloud Storage V4 signing process for HMAC keys:

* **canonical request** = ``METHOD \\n path \\n canonical-query \\n canonical-headers \\n
  signed-headers \\n UNSIGNED-PAYLOAD``, where the query contains every parameter except
  ``X-Goog-Signature`` and the headers always include ``host``;
* **string to sign** = ``GOOG4-HMAC-SHA256 \\n timestamp \\n scope \\n hex(sha256(request))``
  with scope ``{YYYYMMDD}/auto/storage/goog4_request``;
* **signing key** = ``HMAC(HMAC(HMAC(HMAC("GOOG4"+secret, date), "auto"), "storage"),
  "goog4_request")`` and the signature is the lowercase hex HMAC of the string to sign;
* a **POST policy** signature is the hex HMAC of the base64 policy document with the same key.

The path is built from the *decoded* bucket and object name with :func:`resource_path`, so the
signer and the verifier agree regardless of how a client percent-encodes the URL.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import quote, urlencode, urlsplit

from mapi_gcs_shim._errors import ShimError

__all__ = [
    "ALGORITHM",
    "MAX_EXPIRES_SECONDS",
    "PolicyDecision",
    "SignedRequest",
    "SigningIdentity",
    "build_post_policy",
    "canonical_headers",
    "canonical_query",
    "canonical_request",
    "derive_signing_key",
    "resource_path",
    "sign_url",
    "string_to_sign",
    "verify_post_policy",
    "verify_signed_request",
]

ALGORITHM: Final = "GOOG4-HMAC-SHA256"
REGION: Final = "auto"
SERVICE: Final = "storage"
REQUEST_TYPE: Final = "goog4_request"
UNSIGNED_PAYLOAD: Final = "UNSIGNED-PAYLOAD"
MAX_EXPIRES_SECONDS: Final = 7 * 24 * 3600
TIMESTAMP_FORMAT: Final = "%Y%m%dT%H%M%SZ"
MAX_POLICY_BYTES: Final = 64 * 1024

P_ALGORITHM: Final = "X-Goog-Algorithm"
P_CREDENTIAL: Final = "X-Goog-Credential"
P_DATE: Final = "X-Goog-Date"
P_EXPIRES: Final = "X-Goog-Expires"
P_SIGNED_HEADERS: Final = "X-Goog-SignedHeaders"
P_SIGNATURE: Final = "X-Goog-Signature"
_SIGNING_PARAMS: Final = (P_ALGORITHM, P_CREDENTIAL, P_DATE, P_EXPIRES, P_SIGNED_HEADERS)

UNSIGNED_HEADERS_ALLOWED: Final = frozenset({"x-goog-hash"})
"""``x-goog-*`` request headers that may be sent without being signed (they can only add a
verification constraint). Every other ``x-goog-*`` header must be signed."""

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TS_RE = re.compile(r"^\d{8}T\d{6}Z$")
_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class SigningIdentity:
    """Who signs: the service-account email (access id) and its HMAC secret."""

    access_id: str
    secret: str

    def __repr__(self) -> str:
        return f"SigningIdentity(access_id={self.access_id!r}, secret=***)"


# ----------------------------------------------------------------------------- primitives


def _q(text: str) -> str:
    return quote(text, safe="-_.~")


def resource_path(bucket: str, name: str | None = None) -> str:
    """The canonical (and URL) path: ``/{bucket}`` or ``/{bucket}/{percent-encoded name}``."""
    if name is None:
        return f"/{_q(bucket)}"
    return f"/{_q(bucket)}/{quote(name, safe='/-_.~')}"


def canonical_query(params: Iterable[tuple[str, str]]) -> str:
    """RFC 3986-encoded, sorted ``k=v`` pairs joined by ``&`` (``X-Goog-Signature`` excluded)."""
    pairs = sorted((_q(k), _q(v)) for k, v in params if k != P_SIGNATURE)
    return "&".join(f"{k}={v}" for k, v in pairs)


def canonical_headers(headers: Mapping[str, str], signed: Sequence[str]) -> str:
    """``name:value\\n`` for every signed header (lowercase names, whitespace collapsed)."""
    lower = {k.lower(): v for k, v in headers.items()}
    out = []
    for name in signed:
        value = lower.get(name)
        if value is None:
            raise ShimError.forbidden(
                f"signed header {name!r} is missing from the request",
                xml_code="SignatureDoesNotMatch",
            )
        out.append(f"{name}:{_WS.sub(' ', value.strip())}\n")
    return "".join(out)


def canonical_request(
    method: str,
    path: str,
    query: Iterable[tuple[str, str]],
    headers: Mapping[str, str],
    signed_headers: Sequence[str],
) -> str:
    return "\n".join(
        [
            method.upper(),
            path,
            canonical_query(query),
            canonical_headers(headers, signed_headers),
            ";".join(signed_headers),
            UNSIGNED_PAYLOAD,
        ]
    )


def credential_scope(date8: str) -> str:
    return f"{date8}/{REGION}/{SERVICE}/{REQUEST_TYPE}"


def string_to_sign(timestamp: str, scope: str, creq: str) -> str:
    return "\n".join([ALGORITHM, timestamp, scope, hashlib.sha256(creq.encode()).hexdigest()])


def derive_signing_key(secret: str, date8: str) -> bytes:
    """The GOOG4 HMAC signing key for ``secret`` on ``date8`` (``YYYYMMDD``)."""
    key = hmac.new(("GOOG4" + secret).encode(), date8.encode(), hashlib.sha256).digest()
    for part in (REGION, SERVICE, REQUEST_TYPE):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return key


def _hmac_hex(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def _fmt(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC).replace(microsecond=0)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC).replace(microsecond=0)


def _host(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"endpoint {endpoint!r} must be an http(s)://host[:port] URL")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"endpoint {endpoint!r} must not have a path, query or fragment")
    return parts.netloc


# ------------------------------------------------------------------------------ signed URLs


def sign_url(
    identity: SigningIdentity,
    *,
    method: str,
    endpoint: str,
    bucket: str,
    name: str,
    expires_in: timedelta,
    now: datetime | None = None,
    headers: Mapping[str, str] | None = None,
    query: Mapping[str, str] | None = None,
) -> str:
    """A V4 query-signed URL ``{endpoint}/{bucket}/{name}?...&X-Goog-Signature=...``.

    ``headers`` (lowercase names) become signed headers and must be sent verbatim with the
    request; ``host`` is always signed. ``query`` parameters (e.g. ``generation``) are signed.
    """
    seconds = int(expires_in.total_seconds())
    if seconds < 1 or seconds > MAX_EXPIRES_SECONDS:
        raise ValueError("expires_in must be between 1 second and 7 days")
    ts = _now(now)
    timestamp = _fmt(ts)
    scope = credential_scope(timestamp[:8])
    all_headers = {k.lower(): v for k, v in (headers or {}).items()}
    all_headers["host"] = _host(endpoint)
    signed = sorted(all_headers)
    params: dict[str, str] = dict(query or {})
    params.update(
        {
            P_ALGORITHM: ALGORITHM,
            P_CREDENTIAL: f"{identity.access_id}/{scope}",
            P_DATE: timestamp,
            P_EXPIRES: str(seconds),
            P_SIGNED_HEADERS: ";".join(signed),
        }
    )
    path = resource_path(bucket, name)
    creq = canonical_request(method, path, params.items(), all_headers, signed)
    signature = _hmac_hex(
        derive_signing_key(identity.secret, timestamp[:8]), string_to_sign(timestamp, scope, creq)
    )
    params[P_SIGNATURE] = signature
    return f"{endpoint.rstrip('/')}{path}?{urlencode(sorted(params.items()), quote_via=quote)}"


@dataclass(frozen=True, slots=True)
class SignedRequest:
    """A verified signed request: who signed it and until when it is valid."""

    access_id: str
    signed_at: datetime
    expires_at: datetime
    signed_headers: tuple[str, ...]


def _parse_credential(credential: str) -> tuple[str, str]:
    parts = credential.split("/")
    if len(parts) != 5 or parts[2:] != [REGION, SERVICE, REQUEST_TYPE] or not parts[0]:
        raise ShimError.bad_request(
            "X-Goog-Credential must be '{access_id}/{YYYYMMDD}/auto/storage/goog4_request'",
            xml_code="AuthorizationQueryParametersError",
        )
    return parts[0], parts[1]


def _parse_timestamp(value: str, what: str) -> datetime:
    if not _TS_RE.match(value):
        raise ShimError.bad_request(
            f"{what} must be YYYYMMDD'T'HHMMSS'Z'", xml_code="AuthorizationQueryParametersError"
        )
    try:
        return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise ShimError.bad_request(
            f"{what} is not a valid timestamp", xml_code="AuthorizationQueryParametersError"
        ) from exc


def verify_signed_request(
    *,
    method: str,
    bucket: str,
    name: str,
    query: Sequence[tuple[str, str]],
    headers: Mapping[str, str],
    lookup_secret: Callable[[str], str | None],
    now: datetime,
    clock_skew: timedelta = timedelta(minutes=5),
) -> SignedRequest:
    """Verify a V4 query-signed request; raise :class:`ShimError` (400/403) otherwise.

    ``headers`` must be the request headers (any case). ``lookup_secret`` maps an access id to
    its HMAC secret (``None`` = unknown signer).
    """
    single: dict[str, str] = {}
    for k, v in query:
        if k in single and k in (*_SIGNING_PARAMS, P_SIGNATURE):
            raise ShimError.bad_request(
                f"duplicate {k} parameter", xml_code="AuthorizationQueryParametersError"
            )
        single.setdefault(k, v)
    missing = [p for p in (*_SIGNING_PARAMS, P_SIGNATURE) if p not in single]
    if missing:
        raise ShimError.bad_request(
            f"missing signed-URL parameter(s): {', '.join(missing)}",
            xml_code="AuthorizationQueryParametersError",
        )
    if single[P_ALGORITHM] != ALGORITHM:
        raise ShimError.bad_request(
            f"X-Goog-Algorithm must be {ALGORITHM}", xml_code="AuthorizationQueryParametersError"
        )
    access_id, date8 = _parse_credential(single[P_CREDENTIAL])
    signed_at = _parse_timestamp(single[P_DATE], P_DATE)
    if single[P_DATE][:8] != date8:
        raise ShimError.bad_request(
            "the X-Goog-Credential date does not match X-Goog-Date",
            xml_code="AuthorizationQueryParametersError",
        )
    try:
        expires = int(single[P_EXPIRES])
    except ValueError:
        expires = -1
    if not 1 <= expires <= MAX_EXPIRES_SECONDS:
        raise ShimError.bad_request(
            "X-Goog-Expires must be an integer between 1 and 604800",
            xml_code="AuthorizationQueryParametersError",
        )
    signature = single[P_SIGNATURE]
    if not _HEX64.match(signature):
        raise ShimError.forbidden("malformed X-Goog-Signature", xml_code="SignatureDoesNotMatch")
    signed_headers = single[P_SIGNED_HEADERS].split(";")
    if "host" not in signed_headers or signed_headers != sorted(set(signed_headers)):
        raise ShimError.bad_request(
            "X-Goog-SignedHeaders must be sorted, unique, lowercase and include 'host'",
            xml_code="AuthorizationQueryParametersError",
        )
    lower_headers = {k.lower(): v for k, v in headers.items()}
    unsigned = sorted(
        h
        for h in lower_headers
        if h.startswith("x-goog-") and h not in signed_headers and h not in UNSIGNED_HEADERS_ALLOWED
    )
    if unsigned:
        raise ShimError.bad_request(
            f"x-goog-* headers must be signed: {', '.join(unsigned)}",
            xml_code="MissingSecurityHeader",
        )

    expires_at = signed_at + timedelta(seconds=expires)
    if now + clock_skew < signed_at:
        raise ShimError.bad_request("the request is not valid yet", xml_code="RequestNotValidYet")
    if now > expires_at:
        raise ShimError(400, "invalid", "the signed URL has expired", xml_code="ExpiredToken")

    secret = lookup_secret(access_id)
    if secret is None:
        raise ShimError.forbidden(
            "the signer is not a known service account", xml_code="SignatureDoesNotMatch"
        )
    path = resource_path(bucket, name)
    creq = canonical_request(method, path, query, lower_headers, signed_headers)
    expected = _hmac_hex(
        derive_signing_key(secret, date8),
        string_to_sign(single[P_DATE], credential_scope(date8), creq),
    )
    if not hmac.compare_digest(expected, signature):
        raise ShimError.forbidden(
            "the request signature does not match", xml_code="SignatureDoesNotMatch"
        )
    return SignedRequest(
        access_id=access_id,
        signed_at=signed_at,
        expires_at=expires_at,
        signed_headers=tuple(signed_headers),
    )


# ------------------------------------------------------------------------------ POST policy

F_POLICY: Final = "policy"
F_ALGORITHM: Final = "x-goog-algorithm"
F_CREDENTIAL: Final = "x-goog-credential"
F_DATE: Final = "x-goog-date"
F_SIGNATURE: Final = "x-goog-signature"
F_KEY: Final = "key"
F_IF_GENERATION_MATCH: Final = "x-goog-if-generation-match"
_UNCOVERED_FIELDS: Final = frozenset({F_SIGNATURE, F_POLICY, "file"})


def build_post_policy(
    *,
    access_id: str,
    bucket: str,
    prefix: str,
    min_bytes: int,
    max_bytes: int,
    expires_in: timedelta,
    sign: Callable[[str, str], str],
    algorithm: str = ALGORITHM,
    now: datetime | None = None,
    extra_conditions: Sequence[Any] = (),
    if_generation_match: int | None = None,
) -> tuple[dict[str, str], datetime]:
    """V4 POST policy form fields restricting uploads to ``prefix`` and a size range.

    ``if_generation_match`` (``0`` = create only) becomes a signed exact-match condition
    and a form field, so the uploader can neither drop nor change it.

    ``sign(policy_b64, date8)`` returns the hex signature (HMAC for the shim, RSA for real
    GCS). Returns ``(fields, expires_at)``; the uploader adds ``key`` and ``file``.
    """
    seconds = int(expires_in.total_seconds())
    if seconds < 1 or seconds > MAX_EXPIRES_SECONDS:
        raise ValueError("expires_in must be between 1 second and 7 days")
    if not 0 <= min_bytes <= max_bytes:
        raise ValueError("need 0 <= min_bytes <= max_bytes")
    ts = _now(now)
    timestamp = _fmt(ts)
    expires_at = ts + timedelta(seconds=seconds)
    credential = f"{access_id}/{credential_scope(timestamp[:8])}"
    conditions: list[Any] = [
        {"bucket": bucket},
        ["starts-with", "$key", prefix],
        ["starts-with", "$content-type", ""],
        ["content-length-range", min_bytes, max_bytes],
        {F_ALGORITHM: algorithm},
        {F_CREDENTIAL: credential},
        {F_DATE: timestamp},
        *extra_conditions,
    ]
    if if_generation_match is not None:
        if if_generation_match < 0:
            raise ValueError("if_generation_match must be >= 0")
        conditions.append({F_IF_GENERATION_MATCH: str(if_generation_match)})
    document = {
        "conditions": conditions,
        "expiration": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    policy = base64.b64encode(
        json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    ).decode()
    fields = {
        F_POLICY: policy,
        F_ALGORITHM: algorithm,
        F_CREDENTIAL: credential,
        F_DATE: timestamp,
        F_SIGNATURE: sign(policy, timestamp[:8]),
    }
    if if_generation_match is not None:
        fields[F_IF_GENERATION_MATCH] = str(if_generation_match)
    return fields, expires_at


def hmac_policy_signer(secret: str) -> Callable[[str, str], str]:
    """The ``sign`` callback of :func:`build_post_policy` for an HMAC (shim) identity."""

    def sign(policy_b64: str, date8: str) -> str:
        return _hmac_hex(derive_signing_key(secret, date8), policy_b64)

    return sign


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """A verified POST policy: the signer and the constraints the upload must satisfy."""

    access_id: str
    expires_at: datetime
    content_length_range: tuple[int, int] | None


def _policy_error(message: str) -> ShimError:
    return ShimError.forbidden(message, xml_code="InvalidPolicyDocument")


def verify_post_policy(
    fields: Mapping[str, str],
    *,
    bucket: str,
    lookup_secret: Callable[[str], str | None],
    now: datetime,
    clock_skew: timedelta = timedelta(minutes=5),
) -> PolicyDecision:
    """Verify the signature, expiry and every condition of a POST policy form.

    ``fields`` maps **lowercase** form-field names to values, for every field before ``file``.
    Every field except ``x-goog-signature``, ``policy``, ``file`` and ``x-ignore-*`` must be
    covered by a condition (GCS semantics), and the ``bucket`` condition is checked against
    the bucket in the URL.
    """
    missing = [
        f
        for f in (F_KEY, F_POLICY, F_ALGORITHM, F_CREDENTIAL, F_DATE, F_SIGNATURE)
        if f not in fields
    ]
    if missing:
        raise ShimError.bad_request(
            f"missing POST form field(s): {', '.join(missing)}", xml_code="InvalidArgument"
        )
    if fields[F_ALGORITHM] != ALGORITHM:
        raise _policy_error(f"x-goog-algorithm must be {ALGORITHM}")
    access_id, date8 = _parse_credential(fields[F_CREDENTIAL])
    signed_at = _parse_timestamp(fields[F_DATE], F_DATE)
    if fields[F_DATE][:8] != date8:
        raise _policy_error("the x-goog-credential date does not match x-goog-date")
    if now + clock_skew < signed_at:
        raise ShimError.bad_request("the policy is not valid yet", xml_code="RequestNotValidYet")
    policy_b64 = fields[F_POLICY]
    if len(policy_b64) > MAX_POLICY_BYTES:
        raise _policy_error("the policy document is too large")
    signature = fields[F_SIGNATURE]
    if not _HEX64.match(signature):
        raise ShimError.forbidden("malformed x-goog-signature", xml_code="SignatureDoesNotMatch")
    secret = lookup_secret(access_id)
    if secret is None:
        raise ShimError.forbidden(
            "the signer is not a known service account", xml_code="SignatureDoesNotMatch"
        )
    expected = _hmac_hex(derive_signing_key(secret, date8), policy_b64)
    if not hmac.compare_digest(expected, signature):
        raise ShimError.forbidden(
            "the policy signature does not match", xml_code="SignatureDoesNotMatch"
        )

    try:
        document = json.loads(base64.b64decode(policy_b64, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise _policy_error("the policy is not base64-encoded JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("conditions"), list):
        raise _policy_error("the policy must be an object with a 'conditions' list")
    expiration = document.get("expiration")
    try:
        expires_at = datetime.fromisoformat(str(expiration).replace("Z", "+00:00"))
    except ValueError as exc:
        raise _policy_error("the policy 'expiration' is not an ISO-8601 timestamp") from exc
    if expires_at.tzinfo is None:
        raise _policy_error("the policy 'expiration' must carry a timezone")
    if now > expires_at:
        raise ShimError(400, "invalid", "the POST policy has expired", xml_code="ExpiredToken")

    covered: set[str] = set()
    length_range: tuple[int, int] | None = None

    def field_value(name: str) -> str:
        if name == "bucket":
            return bucket
        if name not in fields:
            raise _policy_error(f"policy condition on {name!r} but the form has no such field")
        return fields[name]

    for cond in document["conditions"]:
        if isinstance(cond, dict):
            if len(cond) != 1:
                raise _policy_error("an exact-match condition must have exactly one entry")
            ((name, value),) = cond.items()
            name = str(name).lower()
            if field_value(name) != str(value):
                raise _policy_error(f"form field {name!r} does not match the policy")
            covered.add(name)
        elif isinstance(cond, list) and len(cond) == 3:
            op = str(cond[0]).lower()
            if op == "content-length-range":
                lo, hi = cond[1], cond[2]
                if not (isinstance(lo, int) and isinstance(hi, int) and 0 <= lo <= hi):
                    raise _policy_error("content-length-range needs integers 0 <= min <= max")
                length_range = (lo, hi)
                continue
            target = str(cond[1])
            if not target.startswith("$"):
                raise _policy_error(f"condition field {target!r} must start with '$'")
            name = target[1:].lower()
            if op == "starts-with" and str(cond[2]) == "" and name not in fields:
                continue  # "any value, if present" (e.g. an optional content-type)
            value = field_value(name)
            if op == "eq":
                ok = value == str(cond[2])
            elif op == "starts-with":
                ok = value.startswith(str(cond[2]))
            else:
                raise _policy_error(f"unsupported policy operator {op!r}")
            if not ok:
                raise _policy_error(f"form field {name!r} violates the policy ({op})")
            covered.add(name)
        else:
            raise _policy_error("malformed policy condition")

    uncovered = sorted(
        f
        for f in fields
        if f not in covered and f not in _UNCOVERED_FIELDS and not f.startswith("x-ignore-")
    )
    if uncovered:
        raise _policy_error(f"form field(s) not covered by the policy: {', '.join(uncovered)}")
    return PolicyDecision(
        access_id=access_id, expires_at=expires_at, content_length_range=length_range
    )
