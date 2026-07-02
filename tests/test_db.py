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
