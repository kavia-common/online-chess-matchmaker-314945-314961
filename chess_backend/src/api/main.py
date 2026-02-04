"""
Chess Backend API (FastAPI)

Provides:
- Player registration (nickname-based)
- Matchmaking queue (polling ticket)
- Game state retrieval
- Server-side move validation + persistence
- Player game history

This backend is designed to be consumed by the React frontend via REST.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any, Mapping

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.chess_rules import STARTING_FEN, apply_uci_move, side_to_move, winner_if_game_over
from src.api.db import get_db_session
from src.api.schemas import (
    GameResponse,
    GameWithMovesResponse,
    HistoryResponse,
    MatchmakingEnqueueRequest,
    MatchmakingTicketResponse,
    MoveCreateRequest,
    MoveResponse,
    PlayerCreateRequest,
    PlayerResponse,
)

openapi_tags = [
    {"name": "Health", "description": "Service health and basic diagnostics."},
    {"name": "Players", "description": "Create and manage player identities."},
    {"name": "Matchmaking", "description": "Queue players and match them into games."},
    {"name": "Games", "description": "Game state, moves, and history."},
]

app = FastAPI(
    title="Online Chess Matchmaker API",
    description="Backend for online chess play: matchmaking, move validation, persistence, and history.",
    version="1.0.0",
    openapi_tags=openapi_tags,
)

allowed_origins = (os.getenv("ALLOWED_ORIGINS") or "*").split(",") if os.getenv("ALLOWED_ORIGINS") else ["*"]
allowed_headers = (os.getenv("ALLOWED_HEADERS") or "*").split(",") if os.getenv("ALLOWED_HEADERS") else ["*"]
allowed_methods = (os.getenv("ALLOWED_METHODS") or "*").split(",") if os.getenv("ALLOWED_METHODS") else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in allowed_origins],
    allow_credentials=True,
    allow_methods=[m.strip() for m in allowed_methods],
    allow_headers=[h.strip() for h in allowed_headers],
)


def _as_mapping(row: Any) -> Mapping[str, Any]:
    """
    Convert a SQLAlchemy row into a mapping-like interface.

    We fetch rows using `Result.mappings()` in most endpoints, which yields
    `RowMapping` objects. Those must be accessed by key (`row["id"]`) rather than
    attribute (`row.id`). This helper keeps the conversion logic centralized.
    """
    # RowMapping already behaves like a Mapping; keep as-is.
    if isinstance(row, Mapping):
        return row
    # Fallback: attempt to use SQLAlchemy Row._mapping if present.
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping
    raise TypeError("Unsupported row type; expected a mapping/RowMapping.")


def _row_to_player(row: Any) -> PlayerResponse:
    """Convert a DB row mapping into a PlayerResponse."""
    m = _as_mapping(row)
    return PlayerResponse(id=str(m["id"]), nickname=m["nickname"], created_at=m["created_at"])


def _row_to_game(row: Any) -> GameResponse:
    """Convert a DB row mapping into a GameResponse."""
    m = _as_mapping(row)
    return GameResponse(
        id=str(m["id"]),
        white_player_id=str(m["white_player_id"]),
        black_player_id=str(m["black_player_id"]),
        status=m["status"],
        winner_color=m["winner_color"],
        current_fen=m["current_fen"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        ended_at=m["ended_at"],
    )


@app.get("/", tags=["Health"], summary="Health check")
def health_check():
    """
    PUBLIC_INTERFACE
    Basic health check.

    Returns:
        JSON message indicating the service is running.
    """
    return {"message": "Healthy"}


@app.get("/healthz", tags=["Health"], summary="Detailed health check")
async def healthz(db: AsyncSession = Depends(get_db_session)):
    """
    PUBLIC_INTERFACE
    Health check that also validates database connectivity.

    Returns:
        JSON with db_ok boolean and timestamp.
    """
    try:
        await db.execute(text("SELECT 1"))
        return {"ok": True, "db_ok": True, "ts": datetime.utcnow().isoformat() + "Z"}
    except Exception:
        return {"ok": True, "db_ok": False, "ts": datetime.utcnow().isoformat() + "Z"}


@app.post(
    "/players",
    tags=["Players"],
    summary="Create or fetch player by nickname",
    response_model=PlayerResponse,
)
async def create_player(payload: PlayerCreateRequest, db: AsyncSession = Depends(get_db_session)):
    """
    PUBLIC_INTERFACE
    Create a new player with a unique nickname.

    If the nickname already exists, returns the existing player.
    """
    nickname = payload.nickname.strip()
    if not nickname:
        raise HTTPException(status_code=400, detail="Nickname required")

    existing = await db.execute(text("SELECT id, nickname, created_at FROM players WHERE nickname = :n"), {"n": nickname})
    row = existing.mappings().first()
    if row:
        return _row_to_player(row)

    # SQLite test schema defines `id TEXT PRIMARY KEY` with no default, so we must
    # generate IDs in application code (also works fine for Postgres).
    player_id = str(uuid.uuid4())
    await db.execute(
        text("INSERT INTO players (id, nickname) VALUES (:id, :n)"),
        {"id": player_id, "n": nickname},
    )
    await db.commit()

    created = await db.execute(
        text("SELECT id, nickname, created_at FROM players WHERE id = :id"),
        {"id": player_id},
    )
    created_row = created.mappings().first()
    if not created_row:  # pragma: no cover (defensive)
        raise HTTPException(status_code=500, detail="Player creation failed")
    return _row_to_player(created_row)


@app.post(
    "/matchmaking/enqueue",
    tags=["Matchmaking"],
    summary="Join matchmaking queue",
    response_model=MatchmakingTicketResponse,
)
async def enqueue_matchmaking(payload: MatchmakingEnqueueRequest, db: AsyncSession = Depends(get_db_session)):
    """
    PUBLIC_INTERFACE
    Enqueue a player for matchmaking.

    Returns a ticket id. Client should poll /matchmaking/tickets/{ticket_id}.
    """
    # Verify player exists (use mappings for consistent access patterns)
    player = await db.execute(text("SELECT id FROM players WHERE id = :pid"), {"pid": payload.player_id})
    if not player.mappings().first():
        raise HTTPException(status_code=404, detail="Player not found")

    # Create ticket (explicit id for SQLite TEXT PK schema)
    ticket_id = str(uuid.uuid4())
    await db.execute(
        text(
            "INSERT INTO matchmaking_tickets (id, player_id, status) "
            "VALUES (:tid, :pid, 'queued')"
        ),
        {"tid": ticket_id, "pid": payload.player_id},
    )
    ticket_res = await db.execute(
        text("SELECT id, status, game_id, created_at, updated_at FROM matchmaking_tickets WHERE id = :tid"),
        {"tid": ticket_id},
    )
    ticket = ticket_res.mappings().first()

    # Put into queue table (idempotent)
    await db.execute(
        text("INSERT INTO matchmaking_queue (player_id) VALUES (:pid) ON CONFLICT (player_id) DO NOTHING"),
        {"pid": payload.player_id},
    )

    # Try to match with another player
    other_res = await db.execute(
        text(
            "SELECT player_id FROM matchmaking_queue "
            "WHERE player_id <> :pid "
            "ORDER BY created_at ASC "
            "LIMIT 1"
        ),
        {"pid": payload.player_id},
    )
    other = other_res.mappings().first()

    if other:
        other_pid = str(_as_mapping(other)["player_id"])

        # Remove both from queue (SQLite doesn't support binding tuples into IN reliably;
        # do two deletes instead to keep behavior consistent across DBs).
        await db.execute(text("DELETE FROM matchmaking_queue WHERE player_id = :pid"), {"pid": payload.player_id})
        await db.execute(text("DELETE FROM matchmaking_queue WHERE player_id = :pid"), {"pid": other_pid})

        # Assign colors by deterministic ordering (avoid bias by time alone)
        p1 = payload.player_id
        p2 = other_pid
        white_id, black_id = (p1, p2) if p1 < p2 else (p2, p1)

        game_id = str(uuid.uuid4())
        await db.execute(
            text(
                "INSERT INTO games (id, white_player_id, black_player_id, status, winner_color, initial_fen, current_fen, pgn) "
                "VALUES (:gid, :w, :b, 'active', NULL, :fen, :fen, '')"
            ),
            {"gid": game_id, "w": white_id, "b": black_id, "fen": STARTING_FEN},
        )

        # Mark both players' queued tickets as matched (best effort: latest queued ticket)
        await db.execute(
            text(
                "UPDATE matchmaking_tickets SET status='matched', game_id=:gid "
                "WHERE id IN ("
                "  SELECT id FROM matchmaking_tickets WHERE player_id IN (:p1, :p2) AND status='queued' "
                "  ORDER BY created_at DESC LIMIT 2"
                ")"
            ),
            {"gid": game_id, "p1": payload.player_id, "p2": other_pid},
        )

        # Refresh ticket row
        ticket_ref = await db.execute(
            text("SELECT id, status, game_id, created_at, updated_at FROM matchmaking_tickets WHERE id = :tid"),
            {"tid": ticket_id},
        )
        await db.commit()
        ticket = ticket_ref.mappings().first()
    else:
        await db.commit()

    if not ticket:  # pragma: no cover (defensive)
        raise HTTPException(status_code=500, detail="Ticket creation failed")

    tm = _as_mapping(ticket)
    return MatchmakingTicketResponse(
        ticket_id=str(tm["id"]),
        status=tm["status"],
        game_id=str(tm["game_id"]) if tm["game_id"] else None,
        created_at=tm["created_at"],
        updated_at=tm["updated_at"],
    )


@app.get(
    "/matchmaking/tickets/{ticket_id}",
    tags=["Matchmaking"],
    summary="Get matchmaking ticket status",
    response_model=MatchmakingTicketResponse,
)
async def get_ticket(ticket_id: str, db: AsyncSession = Depends(get_db_session)):
    """
    PUBLIC_INTERFACE
    Poll a matchmaking ticket.
    """
    res = await db.execute(
        text("SELECT id, status, game_id, created_at, updated_at FROM matchmaking_tickets WHERE id = :tid"),
        {"tid": ticket_id},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Ticket not found")

    m = _as_mapping(row)
    return MatchmakingTicketResponse(
        ticket_id=str(m["id"]),
        status=m["status"],
        game_id=str(m["game_id"]) if m["game_id"] else None,
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


@app.get(
    "/games/{game_id}",
    tags=["Games"],
    summary="Get game state",
    response_model=GameResponse,
)
async def get_game(game_id: str, db: AsyncSession = Depends(get_db_session)):
    """
    PUBLIC_INTERFACE
    Fetch current game state (FEN, status, players).
    """
    res = await db.execute(text("SELECT * FROM games WHERE id = :gid"), {"gid": game_id})
    row = res.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    return _row_to_game(row)


@app.get(
    "/games/{game_id}/with-moves",
    tags=["Games"],
    summary="Get game state including move list",
    response_model=GameWithMovesResponse,
)
async def get_game_with_moves(game_id: str, db: AsyncSession = Depends(get_db_session)):
    """
    PUBLIC_INTERFACE
    Fetch game state and full move list (chronological).
    """
    game_res = await db.execute(text("SELECT * FROM games WHERE id = :gid"), {"gid": game_id})
    game_row = game_res.mappings().first()
    if not game_row:
        raise HTTPException(status_code=404, detail="Game not found")

    moves_res = await db.execute(
        text(
            "SELECT id, game_id, move_number, color, uci, san, fen_after, created_at "
            "FROM moves WHERE game_id = :gid ORDER BY created_at ASC"
        ),
        {"gid": game_id},
    )
    moves = []
    for m in moves_res.mappings().all():
        mm = _as_mapping(m)
        moves.append(
            MoveResponse(
                id=mm["id"],
                game_id=str(mm["game_id"]),
                move_number=mm["move_number"],
                color=mm["color"],
                uci=mm["uci"],
                san=mm["san"],
                fen_after=mm["fen_after"],
                created_at=mm["created_at"],
            )
        )

    base = _row_to_game(game_row)
    return GameWithMovesResponse(**base.model_dump(), moves=moves)


@app.post(
    "/games/{game_id}/moves",
    tags=["Games"],
    summary="Submit a move (validated server-side)",
    response_model=MoveResponse,
)
async def submit_move(game_id: str, payload: MoveCreateRequest, db: AsyncSession = Depends(get_db_session)):
    """
    PUBLIC_INTERFACE
    Submit a move for a game.

    Validations:
    - game exists and is active
    - player is in the game
    - it's player's turn
    - move is legal per current FEN

    Persists move and updates game current_fen and status if game ends.
    """
    game_res = await db.execute(text("SELECT * FROM games WHERE id = :gid"), {"gid": game_id})
    game = game_res.mappings().first()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    gm = _as_mapping(game)
    if gm["status"] != "active":
        raise HTTPException(status_code=409, detail="Game is not active")

    pid = payload.player_id
    if str(gm["white_player_id"]) != pid and str(gm["black_player_id"]) != pid:
        raise HTTPException(status_code=403, detail="Player not in this game")

    expected_color = side_to_move(gm["current_fen"])
    player_color = "white" if str(gm["white_player_id"]) == pid else "black"
    if expected_color != player_color:
        raise HTTPException(status_code=409, detail=f"It is {expected_color}'s turn")

    # Apply move
    try:
        applied = apply_uci_move(gm["current_fen"], payload.uci)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Determine next move number: count moves for this game
    count_res = await db.execute(text("SELECT COUNT(*) AS c FROM moves WHERE game_id = :gid"), {"gid": game_id})
    count = int(_as_mapping(count_res.mappings().first())["c"])
    move_number = (count // 2) + 1

    # Insert move
    move_row_res = await db.execute(
        text(
            "INSERT INTO moves (game_id, move_number, color, uci, san, fen_after) "
            "VALUES (:gid, :mn, :color, :uci, :san, :fen) "
            "RETURNING id, game_id, move_number, color, uci, san, fen_after, created_at"
        ),
        {"gid": game_id, "mn": move_number, "color": player_color, "uci": applied.uci, "san": applied.san, "fen": applied.fen_after},
    )
    move_row = move_row_res.mappings().first()
    if not move_row:  # pragma: no cover (defensive)
        raise HTTPException(status_code=500, detail="Move insert failed")
    mm = _as_mapping(move_row)

    # Update game
    winner = winner_if_game_over(applied.fen_after)
    if winner is not None or applied.is_stalemate or applied.is_insufficient_material or applied.is_draw_claimable:
        # If game-over with draw, winner stays NULL
        await db.execute(
            text(
                "UPDATE games SET current_fen=:fen, status='finished', winner_color=:winner, ended_at=now() "
                "WHERE id=:gid"
            ),
            {"fen": applied.fen_after, "winner": winner, "gid": game_id},
        )
    else:
        await db.execute(text("UPDATE games SET current_fen=:fen WHERE id=:gid"), {"fen": applied.fen_after, "gid": game_id})

    await db.commit()

    return MoveResponse(
        id=mm["id"],
        game_id=str(mm["game_id"]),
        move_number=mm["move_number"],
        color=mm["color"],
        uci=mm["uci"],
        san=mm["san"],
        fen_after=mm["fen_after"],
        created_at=mm["created_at"],
    )


@app.get(
    "/players/{player_id}/history",
    tags=["Games"],
    summary="Get recent games for a player",
    response_model=HistoryResponse,
)
async def player_history(
    player_id: str,
    limit: int = Query(20, ge=1, le=100, description="Max number of games to return."),
    db: AsyncSession = Depends(get_db_session),
):
    """
    PUBLIC_INTERFACE
    Return recent games involving the specified player.
    """
    # Verify player exists (use mappings so SQLite returns are read correctly)
    player = await db.execute(text("SELECT id FROM players WHERE id = :pid"), {"pid": player_id})
    if not player.mappings().first():
        raise HTTPException(status_code=404, detail="Player not found")

    games_res = await db.execute(
        text(
            "SELECT * FROM games "
            "WHERE white_player_id=:pid OR black_player_id=:pid "
            "ORDER BY created_at DESC "
            "LIMIT :lim"
        ),
        {"pid": player_id, "lim": limit},
    )
    games = [_row_to_game(r) for r in games_res.mappings().all()]
    return HistoryResponse(games=games)
