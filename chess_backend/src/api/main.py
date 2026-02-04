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


def _row_to_player(row) -> PlayerResponse:
    return PlayerResponse(id=str(row.id), nickname=row.nickname, created_at=row.created_at)


def _row_to_game(row) -> GameResponse:
    return GameResponse(
        id=str(row.id),
        white_player_id=str(row.white_player_id),
        black_player_id=str(row.black_player_id),
        status=row.status,
        winner_color=row.winner_color,
        current_fen=row.current_fen,
        created_at=row.created_at,
        updated_at=row.updated_at,
        ended_at=row.ended_at,
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
    return _row_to_player(created.mappings().first())


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
    # Verify player exists
    player = await db.execute(text("SELECT id FROM players WHERE id = :pid"), {"pid": payload.player_id})
    if not player.first():
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
        # Remove both from queue
        await db.execute(text("DELETE FROM matchmaking_queue WHERE player_id IN (:p1, :p2)"), {"p1": payload.player_id, "p2": other.player_id})

        # Assign colors by deterministic ordering (avoid bias by time alone)
        p1 = payload.player_id
        p2 = str(other.player_id)
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
            {"gid": game_id, "p1": payload.player_id, "p2": other.player_id},
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

    return MatchmakingTicketResponse(
        ticket_id=str(ticket.id),
        status=ticket.status,
        game_id=str(ticket.game_id) if ticket.game_id else None,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
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

    return MatchmakingTicketResponse(
        ticket_id=str(row.id),
        status=row.status,
        game_id=str(row.game_id) if row.game_id else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
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
        text("SELECT id, game_id, move_number, color, uci, san, fen_after, created_at FROM moves WHERE game_id = :gid ORDER BY created_at ASC"),
        {"gid": game_id},
    )
    moves = []
    for m in moves_res.mappings().all():
        moves.append(
            MoveResponse(
                id=m.id,
                game_id=str(m.game_id),
                move_number=m.move_number,
                color=m.color,
                uci=m.uci,
                san=m.san,
                fen_after=m.fen_after,
                created_at=m.created_at,
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
    if game.status != "active":
        raise HTTPException(status_code=409, detail="Game is not active")

    pid = payload.player_id
    if str(game.white_player_id) != pid and str(game.black_player_id) != pid:
        raise HTTPException(status_code=403, detail="Player not in this game")

    expected_color = side_to_move(game.current_fen)
    player_color = "white" if str(game.white_player_id) == pid else "black"
    if expected_color != player_color:
        raise HTTPException(status_code=409, detail=f"It is {expected_color}'s turn")

    # Apply move
    try:
        applied = apply_uci_move(game.current_fen, payload.uci)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Determine next move number: count moves for this game
    count_res = await db.execute(text("SELECT COUNT(*) AS c FROM moves WHERE game_id = :gid"), {"gid": game_id})
    count = int(count_res.mappings().first().c)
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
        id=move_row.id,
        game_id=str(move_row.game_id),
        move_number=move_row.move_number,
        color=move_row.color,
        uci=move_row.uci,
        san=move_row.san,
        fen_after=move_row.fen_after,
        created_at=move_row.created_at,
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
    # Verify player exists
    player = await db.execute(text("SELECT id FROM players WHERE id = :pid"), {"pid": player_id})
    if not player.first():
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
