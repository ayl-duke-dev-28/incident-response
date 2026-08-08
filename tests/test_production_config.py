import pytest
from pydantic import ValidationError

from incident_response.config import Settings


def test_development_profile_keeps_local_storage_defaults():
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.database_url == ""
    assert settings.redis_url == ""


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"database_url": ""}, "PostgreSQL"),
        ({"database_url": "sqlite+aiosqlite:///incidents.db"}, "PostgreSQL"),
        (
            {
                "database_url": "postgresql+asyncpg://app:secret@db/incidents",
                "redis_url": "",
            },
            "Redis",
        ),
        (
            {
                "database_url": "postgresql+asyncpg://app:secret@db/incidents",
                "redis_url": "redis://redis:6379/0",
            },
            "TLS",
        ),
    ],
)
def test_production_profile_fails_closed_without_secure_shared_services(
    overrides, message
):
    values = {
        "environment": "production",
        "database_url": "postgresql+asyncpg://app:secret@db/incidents",
        "redis_url": "rediss://redis:6379/0",
        **overrides,
    }

    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **values)
