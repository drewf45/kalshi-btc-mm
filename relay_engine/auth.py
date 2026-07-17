"""Auth — the venue handshake signature (WO-2026-07-17-RELAY-P1).

Scheme (venue-documented, and byte-identical to the live tree's kalshi.py
`_sign_headers`): sign the UTF-8 string `timestamp_ms + METHOD + path` with
RSA-PSS (SHA-256, MGF1-SHA256, salt = DIGEST_LENGTH), base64 the signature.
Path is the request path only — no host, no query.

Env:
  KALSHI_API_KEY_ID              — UUID key id
  KALSHI_PRIVATE_KEY             — RSA PEM, full BEGIN/END. Literal `\\n`
                                   escapes are normalized (Render single-line
                                   paste tolerance).
  KALSHI_PRIVATE_KEY_PEM_BASE64  — FALLBACK, read only when KALSHI_PRIVATE_KEY
                                   is unset: the live tree's env form (base64-
                                   wrapped PEM), already on the Render worker
                                   per §B3 — the shadow reuses it read-only.

Doctrine (§4, banked): absent credentials are a BOOT-STOP; rejected
credentials are a THREE-STRIKE FATAL; only a live socket that dies is a
ladder event. The degrade ladder is for transport, never for auth.

NEVER log key material. The one permitted disclosure is the boot-tape line:
`AUTH: key id …last4 loaded, PEM parsed`.
"""

import base64
import os
import time
from typing import Optional

import requests

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

from . import config, failures
from .errors import FatalIntegrityError

ENV_KEY_ID = "KALSHI_API_KEY_ID"
ENV_PRIVATE_KEY = "KALSHI_PRIVATE_KEY"
ENV_PRIVATE_KEY_B64 = "KALSHI_PRIVATE_KEY_PEM_BASE64"  # live tree's form (§B3 reuse)

_session = requests.Session()


def _normalize_pem(raw: str) -> str:
    """Accept literal `\\n` escapes (single-line env paste) and normalize."""
    return raw.replace("\\n", "\n").strip()


def load_credentials():
    """Read and parse creds from env. FATAL on missing/unparseable — a boot-stop,
    never a retry loop (§4)."""
    key_id = os.environ.get(ENV_KEY_ID, "").strip()
    raw_pem = os.environ.get(ENV_PRIVATE_KEY, "")
    if not raw_pem.strip():
        b64 = os.environ.get(ENV_PRIVATE_KEY_B64, "").strip()
        if b64:
            try:
                raw_pem = base64.b64decode(b64).decode("utf-8")
            except Exception as e:
                failures.fail("CREDS_BAD_BASE64",
                              f"{ENV_PRIVATE_KEY_B64} present but not valid base64: "
                              f"{type(e).__name__}", fatal=True)
    if not key_id or not raw_pem.strip():
        failures.fail("CREDS_ABSENT",
                      f"missing credentials: set {ENV_KEY_ID} and {ENV_PRIVATE_KEY} "
                      f"(or {ENV_PRIVATE_KEY_B64}, the live tree's form) "
                      f"(absent credentials are a boot-stop, not a retry loop)",
                      fatal=True)
    try:
        private_key = serialization.load_pem_private_key(
            _normalize_pem(raw_pem).encode("utf-8"), password=None)
    except Exception as e:
        failures.fail("CREDS_BAD_PEM",
                      f"{ENV_PRIVATE_KEY} present but unparseable as PEM: "
                      f"{type(e).__name__}", fatal=True)
    return key_id, private_key


def boot_check() -> str:
    """Called from the shadow runner BEFORE the connect loop. Returns the one
    permitted boot-tape line; raises FatalIntegrityError on bad creds."""
    key_id, _ = load_credentials()
    return f"AUTH: key id …{key_id[-4:]} loaded, PEM parsed"


def signed_headers(method: str, path: str, *, now_ms: Optional[int] = None) -> dict:
    """Signature headers for one request. Sign at call time — a fresh timestamp
    per attempt, never at import time."""
    key_id, private_key = load_credentials()
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
    msg = f"{ts}{method.upper()}{path}".encode("utf-8")
    sig = private_key.sign(
        msg,
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


def signed_request(method: str, path: str, params: Optional[dict] = None,
                   json_body: Optional[dict] = None, timeout: float = 10.0):
    """THE signed-REST helper (§2.3): the custodian degrade path and future
    evidence reads go through here — one scheme, per-request method+path.
    (The Coinbase candle fetch in delta_builder is public and exempt.)"""
    if not path.startswith("/"):
        path = "/" + path
    full_path = f"{config.API_PREFIX}{path}"
    url = f"{config.API_BASE}{full_path}"
    headers = signed_headers(method, full_path)
    headers["Accept"] = "application/json"
    resp = _session.request(method=method.upper(), url=url, params=params,
                            json=json_body, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} {path}: {resp.text or ''}")
    return resp.json() if resp.content else None


class HandshakeRejections:
    """Three-strike auth escalation (§2.4): creds present but the venue says
    401/403 — that is NOT transport damage. Three consecutive rejections are
    FATAL; any success resets the count."""

    AUTH_STATUSES = (401, 403)

    def __init__(self, limit: int = 3):
        self.limit = limit
        self.consecutive = 0

    def rejected(self, status_code: int) -> None:
        if status_code not in self.AUTH_STATUSES:
            return  # not an auth event — the ladder owns it
        self.consecutive += 1
        if self.consecutive >= self.limit:
            failures.fail("CREDS_REJECTED_BY_VENUE",
                          f"credentials rejected by venue {self.consecutive}x "
                          f"(HTTP {status_code}) — check key id/PEM pair",
                          fatal=True, status_code=status_code)

    def success(self) -> None:
        self.consecutive = 0
