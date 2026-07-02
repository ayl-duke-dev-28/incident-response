from datetime import datetime, timezone
from pathlib import Path

import pytest

from incident_response.models import Alert, Severity


@pytest.fixture
def alert() -> Alert:
    return Alert(
        id="ddg-9273",
        title="Checkout 5xx > 5%",
        description="checkout service error rate at 18%",
        service="checkout",
        severity=Severity.SEV2,
        triggered_at=datetime(2026, 7, 2, 21, 5, 0, tzinfo=timezone.utc),
        metric="http.error_rate",
        threshold=0.05,
        value=0.184,
        tags={"env": "prod", "region": "us-east-1"},
    )


@pytest.fixture
def runbooks_dir() -> Path:
    return Path(__file__).parent.parent / "runbooks"


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "incidents.db"


@pytest.fixture
def postmortem_dir(tmp_path: Path) -> Path:
    return tmp_path / "postmortems"
