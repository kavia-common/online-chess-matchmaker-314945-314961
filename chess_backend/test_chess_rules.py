import pytest

from src.api.chess_rules import (
    STARTING_FEN,
    apply_uci_move,
    legal_moves_uci,
    side_to_move,
    winner_if_game_over,
)


def test_legal_moves_uci_starting_position_has_20_moves():
    moves = legal_moves_uci(STARTING_FEN)
    # Starting chess position has 20 legal moves.
    assert len(moves) == 20
    assert "e2e4" in moves
    assert "g1f3" in moves


def test_apply_uci_move_applies_and_returns_san_and_new_fen():
    applied = apply_uci_move(STARTING_FEN, "e2e4")

    assert applied.uci == "e2e4"
    assert applied.san == "e4"
    assert applied.fen_after.startswith("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b")
    assert applied.is_checkmate is False
    assert applied.is_stalemate is False


def test_apply_uci_move_rejects_illegal_move():
    with pytest.raises(ValueError, match="Illegal move"):
        # Pawn can't jump from e2 to e5 on first move.
        apply_uci_move(STARTING_FEN, "e2e5")


def test_side_to_move_reports_correct_side():
    assert side_to_move(STARTING_FEN) == "white"
    after = apply_uci_move(STARTING_FEN, "e2e4")
    assert side_to_move(after.fen_after) == "black"


def test_winner_if_game_over_checkmate_returns_winner():
    # Black to move and checkmated => result 1-0, so winner is white.
    fen = "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1"
    assert winner_if_game_over(fen) == "white"


def test_winner_if_game_over_stalemate_returns_none():
    # Stalemate => draw
    fen = "8/5Q2/6K1/8/8/8/8/7k b - - 0 1"
    assert winner_if_game_over(fen) is None


def test_apply_uci_move_supports_promotion():
    # White pawn promotes on a8; simple + check position.
    fen = "8/P7/8/8/8/8/8/k6K w - - 0 1"
    applied = apply_uci_move(fen, "a7a8q")
    assert applied.san == "a8=Q+"
    assert applied.fen_after.startswith("Q7/8/8/8/8/8/8/k6K b")
