import sqlite3

from casita import walk


def test_routes_api_disabled_without_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("CASITA_ROUTES_OFFLINE", raising=False)

    assert walk._routes_api_enabled() is False


def test_routes_api_disabled_when_offline(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake-key")
    monkeypatch.setenv("CASITA_ROUTES_OFFLINE", "1")

    assert walk._routes_api_enabled() is False


def test_routes_api_enabled_with_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake-key")
    monkeypatch.delenv("CASITA_ROUTES_OFFLINE", raising=False)

    assert walk._routes_api_enabled() is True


def test_ensure_cache_migrates_mode_into_primary_key(tmp_path):
    db_path = tmp_path / "routes.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE walk_cache (
                from_lat REAL, from_lng REAL,
                to_lat REAL, to_lng REAL,
                mode TEXT NOT NULL DEFAULT 'walk',
                minutes INTEGER NOT NULL,
                source TEXT NOT NULL,
                ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (from_lat, from_lng, to_lat, to_lng)
            )"""
        )
        conn.execute(
            "INSERT INTO walk_cache "
            "(from_lat, from_lng, to_lat, to_lng, mode, minutes, source) "
            "VALUES (1, 2, 3, 4, 'walk', 10, 'api')"
        )
        walk._ensure_cache(conn)

        pk_cols = {row[1] for row in conn.execute("PRAGMA table_info(walk_cache)") if row[5]}
        assert "mode" in pk_cols

        conn.execute(
            "INSERT INTO walk_cache "
            "(from_lat, from_lng, to_lat, to_lng, mode, minutes, source) "
            "VALUES (1, 2, 3, 4, 'drive', 5, 'api')"
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM walk_cache WHERE from_lat=1 AND from_lng=2"
        ).fetchone()[0]
        assert count == 2
