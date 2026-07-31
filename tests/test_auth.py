"""WO-2026-07-17-RELAY-P1 §2.5: the four auth tests.

Note on the "signature vector": RSA-PSS uses a RANDOM salt, so an exact
expected-signature byte string is cryptographically impossible. The vector
test therefore pins everything deterministic (header set, key id, mocked
timestamp) and CRYPTOGRAPHICALLY VERIFIES the signature against the public
key over the exact expected message with the exact expected parameters —
a stronger check than byte equality, and the strongest one possible.
"""

import pytest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from relay_engine import auth
from relay_engine.errors import FatalIntegrityError

KEY_ID = "test-key-id-1234abcd"


@pytest.fixture
def throwaway_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return key, pem


@pytest.fixture
def creds_env(monkeypatch, throwaway_pem):
    _, pem = throwaway_pem
    monkeypatch.setenv(auth.ENV_KEY_ID, KEY_ID)
    monkeypatch.setenv(auth.ENV_PRIVATE_KEY, pem)
    return throwaway_pem


def test_signature_vector_fixed_timestamp(creds_env):
    key, _ = creds_env
    ts = 1752787200123  # mocked clock — deterministic
    headers = auth.signed_headers("get", "/trade-api/ws/v2", now_ms=ts)

    # exact expected header set
    assert set(headers) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE",
                            "KALSHI-ACCESS-TIMESTAMP"}
    assert headers["KALSHI-ACCESS-KEY"] == KEY_ID
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1752787200123"

    # signature verifies over EXACTLY ts + METHOD + path with EXACTLY
    # PSS(MGF1-SHA256, salt=DIGEST_LENGTH) — method upper-cased by the signer
    import base64
    sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(
        sig, b"1752787200123GET/trade-api/ws/v2",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    # and NOT over a mutated message
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        key.public_key().verify(
            sig, b"1752787200123GET/trade-api/ws/v2?x=1",
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )


def test_missing_creds_boot_stop(monkeypatch):
    monkeypatch.delenv(auth.ENV_KEY_ID, raising=False)
    monkeypatch.delenv(auth.ENV_PRIVATE_KEY, raising=False)
    monkeypatch.delenv(auth.ENV_PRIVATE_KEY_B64, raising=False)
    with pytest.raises(FatalIntegrityError, match="boot-stop"):
        auth.boot_check()
    # unparseable PEM is equally a boot-stop
    monkeypatch.setenv(auth.ENV_KEY_ID, KEY_ID)
    monkeypatch.setenv(auth.ENV_PRIVATE_KEY, "-----BEGIN PRIVATE KEY-----\ngarbage\n-----END PRIVATE KEY-----")
    with pytest.raises(FatalIntegrityError, match="unparseable"):
        auth.boot_check()


def test_boot_check_line_shows_last4_only(creds_env):
    line = auth.boot_check()
    assert line == "AUTH: key id …abcd loaded, PEM parsed"
    assert KEY_ID not in line  # never the full id, never key material


def test_three_401s_escalate_fatal():
    r = auth.HandshakeRejections(limit=3)
    r.rejected(401)
    r.rejected(403)  # mixed 401/403 both count as auth
    with pytest.raises(FatalIntegrityError, match="credentials rejected by venue"):
        r.rejected(401)


def test_single_401_then_success_no_false_fatal():
    r = auth.HandshakeRejections(limit=3)
    r.rejected(401)
    r.success()  # venue accepted — counter resets, ladder business resumes
    r.rejected(401)
    r.rejected(401)  # only 2 consecutive — no FATAL
    assert r.consecutive == 2
    # non-auth statuses are the ladder's business, never counted
    r.success()
    r.rejected(500)
    r.rejected(502)
    assert r.consecutive == 0


def test_runner_boot_stops_before_any_connect(monkeypatch):
    """§2.5: missing creds kill the runner at boot — before the boot tape,
    before the engine, before any connect attempt."""
    import asyncio
    monkeypatch.delenv(auth.ENV_KEY_ID, raising=False)
    monkeypatch.delenv(auth.ENV_PRIVATE_KEY, raising=False)
    monkeypatch.delenv(auth.ENV_PRIVATE_KEY_B64, raising=False)
    from relay_engine.shadow_runner import run
    with pytest.raises(FatalIntegrityError, match="boot-stop"):
        asyncio.run(run())


def test_base64_fallback_matches_live_env_form(monkeypatch, throwaway_pem):
    """§B3: the Render worker already carries KALSHI_PRIVATE_KEY_PEM_BASE64
    (the live tree's form) — the shadow reuses it when KALSHI_PRIVATE_KEY is
    unset; the plain var wins when both are present."""
    import base64
    key, pem = throwaway_pem
    monkeypatch.setenv(auth.ENV_KEY_ID, KEY_ID)
    monkeypatch.delenv(auth.ENV_PRIVATE_KEY, raising=False)
    monkeypatch.setenv(auth.ENV_PRIVATE_KEY_B64,
                       base64.b64encode(pem.encode()).decode())
    _, k1 = auth.load_credentials()
    assert k1.public_key().public_numbers().n == key.public_key().public_numbers().n

    # precedence: plain PEM var wins over the base64 fallback
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    monkeypatch.setenv(auth.ENV_PRIVATE_KEY, other_pem)
    _, k2 = auth.load_credentials()
    assert k2.public_key().public_numbers().n == other.public_key().public_numbers().n

    # garbage base64 is a boot-stop, not a fall-through
    monkeypatch.delenv(auth.ENV_PRIVATE_KEY, raising=False)
    monkeypatch.setenv(auth.ENV_PRIVATE_KEY_B64, "!!!not-base64!!!")
    with pytest.raises(FatalIntegrityError, match="not valid base64"):
        auth.load_credentials()


def test_pem_normalization_literal_backslash_n(monkeypatch, throwaway_pem):
    key, pem = throwaway_pem
    single_line = pem.replace("\n", "\\n")  # Render single-line paste form
    assert "\\n" in single_line and "\n" not in single_line

    monkeypatch.setenv(auth.ENV_KEY_ID, KEY_ID)
    monkeypatch.setenv(auth.ENV_PRIVATE_KEY, single_line)
    _, k1 = auth.load_credentials()
    monkeypatch.setenv(auth.ENV_PRIVATE_KEY, pem)
    _, k2 = auth.load_credentials()
    # identical key either way
    assert (k1.public_key().public_numbers().n
            == k2.public_key().public_numbers().n
            == key.public_key().public_numbers().n)
