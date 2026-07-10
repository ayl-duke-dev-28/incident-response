from datetime import datetime, timezone

from incident_response.db import IncidentStore
from incident_response.models import Incident, IncidentStatus


def test_roundtrip_incident(tmp_db, alert):
    store = IncidentStore(tmp_db)
    incident = Incident(
        id="inc-1",
        alert=alert,
        status=IncidentStatus.INVESTIGATING,
        created_at=datetime.now(timezone.utc),
    )
    store.save(incident)
    fetched = store.get("inc-1")
    assert fetched is not None
    assert fetched.id == "inc-1"
    assert fetched.alert.service == "checkout"


def test_list_open_excludes_resolved(tmp_db, alert):
    store = IncidentStore(tmp_db)
    now = datetime.now(timezone.utc)
    a = Incident(id="a", alert=alert, status=IncidentStatus.INVESTIGATING, created_at=now)
    b = Incident(id="b", alert=alert, status=IncidentStatus.RESOLVED, created_at=now)
    store.save(a)
    store.save(b)
    open_ids = {i.id for i in store.list_open()}
    assert open_ids == {"a"}


def test_list_recent_orders_all_statuses_and_limits(tmp_db, alert):
    store = IncidentStore(tmp_db)
    oldest = Incident(
        id="oldest",
        alert=alert,
        status=IncidentStatus.RESOLVED,
        created_at=datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc),
    )
    newest = Incident(
        id="newest",
        alert=alert,
        status=IncidentStatus.INVESTIGATING,
        created_at=datetime(2026, 7, 2, 21, 7, tzinfo=timezone.utc),
    )
    middle = Incident(
        id="middle",
        alert=alert,
        status=IncidentStatus.MITIGATED,
        created_at=datetime(2026, 7, 2, 21, 6, tzinfo=timezone.utc),
    )
    store.save(oldest)
    store.save(newest)
    store.save(middle)

    assert [incident.id for incident in store.list_recent()] == ["newest", "middle", "oldest"]
    assert [incident.id for incident in store.list_recent(limit=2)] == ["newest", "middle"]
