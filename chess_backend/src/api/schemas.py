"""Pydantic schemas for the chess backend API."""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


PlayerColor = Literal["white", "black"]
GameStatus = Literal["active", "finished"]


class PlayerCreateRequest(BaseModel):
    nickname: str = Field(..., min_length=2, max_length=24, description="Public nickname (unique).")


class PlayerResponse(BaseModel):
    id: str = Field(..., description="Player UUID.")
    nickname: str = Field(..., description="Public nickname.")
    created_at: datetime = Field(..., description="Creation timestamp.")


class MatchmakingEnqueueRequest(BaseModel):
    player_id: str = Field(..., description="Player UUID.")


class MatchmakingTicketResponse(BaseModel):
    ticket_id: str = Field(..., description="Matchmaking ticket UUID.")
    status: Literal["queued", "matched", "cancelled"] = Field(..., description="Current ticket status.")
    game_id: Optional[str] = Field(None, description="Game UUID when matched.")
    created_at: datetime = Field(..., description="Ticket creation time.")
    updated_at: datetime = Field(..., description="Last update time.")


class GameResponse(BaseModel):
    id: str = Field(..., description="Game UUID.")
    white_player_id: str = Field(..., description="White player UUID.")
    black_player_id: str = Field(..., description="Black player UUID.")
    status: GameStatus = Field(..., description="Game status.")
    winner_color: Optional[PlayerColor] = Field(None, description="Winner if finished; null for draw/active.")
    current_fen: str = Field(..., description="Current board state as FEN.")
    created_at: datetime = Field(..., description="Creation timestamp.")
    updated_at: datetime = Field(..., description="Last update timestamp.")
    ended_at: Optional[datetime] = Field(None, description="End timestamp (if finished).")


class MoveCreateRequest(BaseModel):
    player_id: str = Field(..., description="Player UUID making the move.")
    uci: str = Field(..., min_length=4, max_length=5, description="Move in UCI, e.g. e2e4, e7e8q.")


class MoveResponse(BaseModel):
    id: int = Field(..., description="Move row id.")
    game_id: str = Field(..., description="Game UUID.")
    move_number: int = Field(..., description="Move number (1-based).")
    color: PlayerColor = Field(..., description="Color that moved.")
    uci: str = Field(..., description="UCI string.")
    san: str = Field(..., description="SAN notation.")
    fen_after: str = Field(..., description="FEN after move.")
    created_at: datetime = Field(..., description="Creation timestamp.")


class GameWithMovesResponse(GameResponse):
    moves: List[MoveResponse] = Field(..., description="Moves in chronological order.")


class HistoryResponse(BaseModel):
    games: List[GameResponse] = Field(..., description="Recent games for player.")
