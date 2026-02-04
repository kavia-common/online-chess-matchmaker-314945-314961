from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.chess_rules import STARTING_FEN
from src.api.db import get_db_session
from src.api.main import app

# Minimal schema required by src.api.main SQL statements.
_SQLITE_SCHEMA = [
    # players
    """
    CREATE TABLE IF NOT EXISTS players (
        id TEXT PRIMARY KEY,
        nickname TEXT UNIQUE NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # matchmaking queue
    """
    CREATE TABLE IF NOT EXISTS matchmaking_queue (
        player_id TEXT PRIMARY KEY,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # matchmaking tickets
    """
    CREATE TABLE IF NOT EXISTS matchmaking_tickets (
        id TEXT PRIMARY KEY,
        player_id TEXT NOT NULL,
        status TEXT NOT NULL,
        game_id TEXT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # games
    """
    CREATE TABLE IF NOT EXISTS games (
        id TEXT PRIMARY KEY,
        white_player_id TEXT NOT NULL,
        black_player_id TEXT NOT NULL,
        status TEXT NOT NULL,
        winner_color TEXT NULL,
        initial_fen TEXT NOT NULL,
        current_fen TEXT NOT NULL,
        pgn TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        ended_at TEXT NULL
    )
    """,
    # moves
    """
    CREATE TABLE IF NOT EXISTS moves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id TEXT NOT NULL,
        move_number INTEGER NOT NULL,
        color TEXT NOT NULL,
        uci TEXT NOT NULL,
        san TEXT NOT NULL,
        fen_after TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
]


@pytest.fixture()
def api_client():
    """
    Provide a FastAPI TestClient backed by a SQLite async DB, without relying on
    private TestClient internals like `TestClient._loop`.

    Implementation details:
    - Uses a *shared* in-memory SQLite database so multiple async connections see the same schema.
    - Initializes schema via an AsyncEngine connection (no pytest-asyncio needed; we run it via `asyncio.run`).
    - Overrides `get_db_session` so the FastAPI app uses the test engine.
    """
    import asyncio

    # Shared in-memory DB across connections:
    # https://www.sqlite.org/inmemorydb.html#sharedmemdb
    engine = create_async_engine(
        "sqlite+aiosqlite:///file::memory:?cache=shared",
        future=True,
        connect_args={"uri": True},
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def init_schema():
        async with engine.begin() as conn:
            for stmt in _SQLITE_SCHEMA:
                await conn.execute(text(stmt))

    async def override_get_db_session():
        async with sessionmaker() as session:
            yield session

    # Initialize schema once before using the client.
    asyncio.run(init_schema())

    # Install dependency override before creating TestClient.
    app.dependency_overrides[get_db_session] = override_get_db_session

    with TestClient(app) as client:
        try:
            yield client
        finally:
            # Cleanup overrides to avoid cross-test leakage.
            app.dependency_overrides.clear()
            asyncio.run(engine.dispose())


async def _insert_player(db: AsyncSession, player_id: str, nickname: str):
    await db.execute(
        text("INSERT INTO players (id, nickname, created_at) VALUES (:id, :n, :ts)"),
        {"id": player_id, "n": nickname, "ts": datetime.utcnow().isoformat() + "Z"},
    )


def test_health_check(api_client):
    res = api_client.get("/")
    assert res.status_code == 200
    assert res.json()["message"] == "Healthy"


def test_create_player_creates_and_is_idempotent(api_client):
    res1 = api_client.post("/players", json={"nickname": "alpha"})
    assert res1.status_code == 200
    p1 = res1.json()
    assert p1["nickname"] == "alpha"
    assert "id" in p1

    # Same nickname should return same player record.
    res2 = api_client.post("/players", json={"nickname": "alpha"})
    assert res2.status_code == 200
    p2 = res2.json()
    assert p2["id"] == p1["id"]


def test_enqueue_matchmaking_404_for_missing_player(api_client):
    res = api_client.post("/matchmaking/enqueue", json={"player_id": "missing"})
    assert res.status_code == 404
    assert res.json()["detail"] == "Player not found"


def test_matchmaking_two_players_get_matched_and_can_play_moves(api_client):
    # Create two players
    p1 = api_client.post("/players", json={"nickname": "p1"}).json()
    p2 = api_client.post("/players", json={"nickname": "p2"}).json()

    # First enqueue -> queued
    t1 = api_client.post("/matchmaking/enqueue", json={"player_id": p1["id"]}).json()
    assert t1["status"] == "queued"

    # Second enqueue triggers match; second ticket should be matched.
    t2 = api_client.post("/matchmaking/enqueue", json={"player_id": p2["id"]}).json()
    assert t2["status"] == "matched"
    assert t2["game_id"]

    game_id = t2["game_id"]

    # First player's ticket should now be matched too (poll it).
    t1_polled = api_client.get(f"/matchmaking/tickets/{t1['ticket_id']}").json()
    assert t1_polled["status"] == "matched"
    assert t1_polled["game_id"] == game_id

    # Get game state
    game = api_client.get(f"/games/{game_id}").json()
    assert game["id"] == game_id
    assert game["status"] == "active"
    assert game["current_fen"] == STARTING_FEN

    # White is deterministic by lexicographic player id ordering.
    white_id = min(p1["id"], p2["id"])
    black_id = max(p1["id"], p2["id"])
    assert game["white_player_id"] == white_id
    assert game["black_player_id"] == black_id

    # Black trying to play first should fail.
    res_bad_turn = api_client.post(f"/games/{game_id}/moves", json={"player_id": black_id, "uci": "e7e5"})
    assert res_bad_turn.status_code == 409
    assert "white" in res_bad_turn.json()["detail"]

    # White plays e2e4
    res_move1 = api_client.post(f"/games/{game_id}/moves", json={"player_id": white_id, "uci": "e2e4"})
    assert res_move1.status_code == 200
    m1 = res_move1.json()
    assert m1["color"] == "white"
    assert m1["move_number"] == 1
    assert m1["uci"] == "e2e4"
    assert m1["san"] == "e4"

    # Black plays e7e5
    res_move2 = api_client.post(f"/games/{game_id}/moves", json={"player_id": black_id, "uci": "e7e5"})
    assert res_move2.status_code == 200
    m2 = res_move2.json()
    assert m2["color"] == "black"
    assert m2["move_number"] == 1
    assert m2["uci"] == "e7e5"

    # With-moves endpoint returns chronological list
    g_with = api_client.get(f"/games/{game_id}/with-moves").json()
    assert g_with["id"] == game_id
    assert len(g_with["moves"]) == 2
    assert [g_with["moves"][0]["uci"], g_with["moves"][1]["uci"]] == ["e2e4", "e7e5"]


def test_history_requires_player_and_returns_games(api_client):
    p = api_client.post("/players", json={"nickname": "hist"}).json()

    # No games yet.
    r0 = api_client.get(f"/players/{p['id']}/history?limit=10")
    assert r0.status_code == 200
    assert r0.json()["games"] == []

    # Make a game by matching with another player.
    p2 = api_client.post("/players", json={"nickname": "hist2"}).json()
    api_client.post("/matchmaking/enqueue", json={"player_id": p["id"]})
    t2 = api_client.post("/matchmaking/enqueue", json={"player_id": p2["id"]}).json()
    game_id = t2["game_id"]

    r1 = api_client.get(f"/players/{p['id']}/history?limit=10")
    assert r1.status_code == 200
    games = r1.json()["games"]
    assert len(games) == 1
    assert games[0]["id"] == game_id

    # Unknown player -> 404
    r404 = api_client.get("/players/does-not-exist/history?limit=10")
    assert r404.status_code == 404
    assert r404.json()["detail"] == "Player not found"
