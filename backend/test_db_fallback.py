import importlib


def test_db_uses_sqlite_fallback_when_primary_database_is_unreachable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://bad-host.example/postgres")

    import db.db as db_module
    importlib.reload(db_module)

    assert db_module.DATABASE_URL.startswith("sqlite:///")
    assert "hogriders.db" in db_module.DATABASE_URL
