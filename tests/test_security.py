import base64
import hashlib
import hmac

from incident_response.security import verify_datadog, verify_generic_hmac, verify_pagerduty


def _dd_sig(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def _hex_sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_datadog_valid_signature():
    body = b'{"alert":"x"}'
    assert verify_datadog("s3cr3t", body, _dd_sig("s3cr3t", body))


def test_datadog_rejects_tampered_body():
    body = b'{"alert":"x"}'
    tampered = b'{"alert":"y"}'
    sig = _dd_sig("s3cr3t", body)
    assert not verify_datadog("s3cr3t", tampered, sig)


def test_datadog_empty_secret_denies():
    assert not verify_datadog("", b"body", "sig")


def test_pagerduty_matches_any_v1_entry():
    body = b'{"a":1}'
    good = _hex_sig("secret", body)
    header = f"v1=deadbeef,v1={good},v1=cafef00d"
    assert verify_pagerduty("secret", body, header)


def test_pagerduty_rejects_when_no_match():
    assert not verify_pagerduty("secret", b"body", "v1=deadbeef,v1=cafef00d")


def test_generic_hmac():
    body = b"hello"
    assert verify_generic_hmac("k", body, _hex_sig("k", body))
    assert not verify_generic_hmac("k", body, "0" * 64)
