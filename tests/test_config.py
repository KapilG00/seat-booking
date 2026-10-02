import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_production_refuses_dev_secrets():
    with pytest.raises(ValidationError):
        Settings(environment="production")


def test_production_accepts_real_secrets():
    s = Settings(environment="production", jwt_secret="x" * 40, admin_token="y" * 40)
    assert s.environment == "production"
