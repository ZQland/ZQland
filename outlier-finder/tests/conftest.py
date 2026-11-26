import pytest

from app.config import load_config
from app.db import init_db
from app.providers import reset_cache


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    for key in ("YOUTUBE_API_KEY", "APIFY_TOKEN", "IG_ACCESS_TOKEN", "IG_USER_ID",
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("OUTLIER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUTLIER_ENV_FILE", str(tmp_path / "no.env"))  # never read the real .env
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "no-ant-profile"))  # ignore real `ant` logins
    monkeypatch.delenv("OUTLIER_DB", raising=False)
    reset_cache()
    c = load_config()
    c.genres = {"fitness": ["abs workout", "fat loss routine"], "ai": ["ai tools"]}
    init_db(c.db_path)
    yield c
    reset_cache()
