from algoflat.broker.credentials import (
    FlattradeConfig,
    credentials_status,
    mask_secret,
)


def test_mask_secret_short_value():
    assert mask_secret("abc") == "•••"


def test_mask_secret_long_value():
    masked = mask_secret("abcdefghijklmnop")
    assert masked.startswith("abcd")
    assert masked.endswith("mnop")
    assert "•" in masked


def test_merge_db_overrides_env():
    base = FlattradeConfig(
        user_id="ENV_USER",
        api_key="env_key",
        api_secret="env_secret",
        password=None,
        totp_secret=None,
        access_token=None,
        token_expires_at=None,
        redirect_url="http://127.0.0.1:8000/callback",
        token_file=".flattrade/session.json",
    )
    merged = base.merge_db(
        {
            "user_id": "DB_USER",
            "api_key": "db_key",
            "api_secret": None,
            "password": "pw",
        }
    )
    assert merged.user_id == "DB_USER"
    assert merged.api_key == "db_key"
    assert merged.api_secret == "env_secret"
    assert merged.password == "pw"


def test_credentials_status_flags():
    cfg = FlattradeConfig(
        user_id="U1",
        api_key="key",
        api_secret="secret",
        password="pw",
        totp_secret="totp",
        access_token=None,
        token_expires_at=None,
        redirect_url="http://127.0.0.1:8000/callback",
        token_file=".flattrade/session.json",
    )
    status = credentials_status(cfg, source="database")
    assert status["has_api_credentials"] is True
    assert status["has_auto_login"] is True
    assert status["password_set"] is True
