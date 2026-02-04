"""
Chess rules and validation helpers built on python-chess.

Server is the source of truth: validates moves against current FEN and updates
game state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import chess


STARTING_FEN = chess.STARTING_FEN


@dataclass(frozen=True)
class AppliedMove:
    """Result of applying a move to a board."""
    uci: str
    san: str
    fen_after: str
    is_checkmate: bool
    is_stalemate: bool
    is_insufficient_material: bool
    is_draw_claimable: bool


def _board_from_fen(fen: str) -> chess.Board:
    board = chess.Board(fen)
    return board


# PUBLIC_INTERFACE
def legal_moves_uci(fen: str) -> List[str]:
    """Return all legal moves in UCI format for the given FEN."""
    board = _board_from_fen(fen)
    return [m.uci() for m in board.legal_moves]


# PUBLIC_INTERFACE
def apply_uci_move(fen: str, uci: str) -> AppliedMove:
    """
    Apply a UCI move on a given FEN, validating legality.

    Raises:
        ValueError: if move is invalid or illegal.
    """
    board = _board_from_fen(fen)
    try:
        move = chess.Move.from_uci(uci)
    except Exception as exc:  # pragma: no cover
        raise ValueError("Invalid UCI format") from exc

    if move not in board.legal_moves:
        raise ValueError("Illegal move")

    san = board.san(move)
    board.push(move)

    return AppliedMove(
        uci=uci,
        san=san,
        fen_after=board.fen(),
        is_checkmate=board.is_checkmate(),
        is_stalemate=board.is_stalemate(),
        is_insufficient_material=board.is_insufficient_material(),
        is_draw_claimable=board.can_claim_draw(),
    )


# PUBLIC_INTERFACE
def side_to_move(fen: str) -> str:
    """Return 'white' or 'black' based on side to move for given FEN."""
    board = _board_from_fen(fen)
    return "white" if board.turn == chess.WHITE else "black"


# PUBLIC_INTERFACE
def winner_if_game_over(fen: str) -> Optional[str]:
    """
    If the position is game-over, return winner color ('white'/'black') or None if draw/not over.
    """
    board = _board_from_fen(fen)
    if not board.is_game_over(claim_draw=True):
        return None

    result = board.result(claim_draw=True)  # "1-0", "0-1", "1/2-1/2"
    if result == "1-0":
        return "white"
    if result == "0-1":
        return "black"
    return None
