from datetime import datetime, timedelta, timezone

from incident_response.dedup import DedupIndex, alert_fingerprint
from incident_response.models import Alert, Severity


def _alert(minute: int, service: str = "checkout", metric: str = "http.error_rate") -> Alert:
    return Alert(
        id=f"a-{minute}",
        title="x",
        service=service,
        severity=Severity.SEV2,
        triggered_at=datetime(2026, 7, 2, 21, minute, tzinfo=timezone.utc),
        metric=metric,
    )


def test_same_bucket_same_fingerprint():
    a = _alert(1)
    b = _alert(3)  # same 15-min bucket
    assert alert_fingerprint(a) == alert_fingerprint(b)


def test_different_bucket_different_fingerprint():
    a = _alert(1)
    b = _alert(20)  # different 15-min bucket
    assert alert_fingerprint(a) != alert_fingerprint(b)


def test_service_changes_fingerprint():
    a = _alert(1, service="checkout")
    b = _alert(1, service="auth")
    assert alert_fingerprint(a) != alert_fingerprint(b)


def test_dedup_index_ttl_expiry():
    t = {"now": 0.0}
    idx = DedupIndex(ttl_seconds=10.0, clock=lambda: t["now"])
    idx.set("fp", "inc-1")
    assert idx.get("fp") == "inc-1"
    t["now"] = 20.0
    assert idx.get("fp") is None


def test_dedup_index_evicts_oldest_when_full():
    idx = DedupIndex(ttl_seconds=1000.0, max_keys=2, clock=lambda: 0.0)
    idx.set("a", "1")
    idx.set("b", "2")
    idx.set("c", "3")
    assert idx.get("a") is None
    assert idx.get("b") == "2"
    assert idx.get("c") == "3"
