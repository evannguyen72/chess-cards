"""A small chess engine whose strength scales with its Elo rating.

Low-rated bots search shallowly, add a lot of noise to their evaluation and
sometimes play a random move. High-rated bots search deeper, look at capture
sequences to the end (quiescence search) and play almost noise-free.
"""
import random

import chess

MATE = 100_000
INF = 10 ** 9

VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}

# Piece-square tables, written as seen from White with rank 8 on top.
PST = {
    chess.PAWN: [
        0, 0, 0, 0, 0, 0, 0, 0,
        50, 50, 50, 50, 50, 50, 50, 50,
        10, 10, 20, 30, 30, 20, 10, 10,
        5, 5, 10, 25, 25, 10, 5, 5,
        0, 0, 0, 20, 20, 0, 0, 0,
        5, -5, -10, 0, 0, -10, -5, 5,
        5, 10, 10, -20, -20, 10, 10, 5,
        0, 0, 0, 0, 0, 0, 0, 0],
    chess.KNIGHT: [
        -50, -40, -30, -30, -30, -30, -40, -50,
        -40, -20, 0, 0, 0, 0, -20, -40,
        -30, 0, 10, 15, 15, 10, 0, -30,
        -30, 5, 15, 20, 20, 15, 5, -30,
        -30, 0, 15, 20, 20, 15, 0, -30,
        -30, 5, 10, 15, 15, 10, 5, -30,
        -40, -20, 0, 5, 5, 0, -20, -40,
        -50, -40, -30, -30, -30, -30, -40, -50],
    chess.BISHOP: [
        -20, -10, -10, -10, -10, -10, -10, -20,
        -10, 0, 0, 0, 0, 0, 0, -10,
        -10, 0, 5, 10, 10, 5, 0, -10,
        -10, 5, 5, 10, 10, 5, 5, -10,
        -10, 0, 10, 10, 10, 10, 0, -10,
        -10, 10, 10, 10, 10, 10, 10, -10,
        -10, 5, 0, 0, 0, 0, 5, -10,
        -20, -10, -10, -10, -10, -10, -10, -20],
    chess.ROOK: [
        0, 0, 0, 0, 0, 0, 0, 0,
        5, 10, 10, 10, 10, 10, 10, 5,
        -5, 0, 0, 0, 0, 0, 0, -5,
        -5, 0, 0, 0, 0, 0, 0, -5,
        -5, 0, 0, 0, 0, 0, 0, -5,
        -5, 0, 0, 0, 0, 0, 0, -5,
        -5, 0, 0, 0, 0, 0, 0, -5,
        0, 0, 0, 5, 5, 0, 0, 0],
    chess.QUEEN: [
        -20, -10, -10, -5, -5, -10, -10, -20,
        -10, 0, 0, 0, 0, 0, 0, -10,
        -10, 0, 5, 5, 5, 5, 0, -10,
        -5, 0, 5, 5, 5, 5, 0, -5,
        0, 0, 5, 5, 5, 5, 0, -5,
        -10, 5, 5, 5, 5, 5, 0, -10,
        -10, 0, 5, 0, 0, 0, 0, -10,
        -20, -10, -10, -5, -5, -10, -10, -20],
    chess.KING: [
        -30, -40, -40, -50, -50, -40, -40, -30,
        -30, -40, -40, -50, -50, -40, -40, -30,
        -30, -40, -40, -50, -50, -40, -40, -30,
        -30, -40, -40, -50, -50, -40, -40, -30,
        -20, -30, -30, -40, -40, -30, -30, -20,
        -10, -20, -20, -20, -20, -20, -20, -10,
        20, 20, 0, 0, 0, 0, 20, 20,
        20, 30, 10, 0, 0, 10, 30, 20],
}
KING_ENDGAME = [
    -50, -40, -30, -20, -20, -30, -40, -50,
    -30, -20, -10, 0, 0, -10, -20, -30,
    -30, -10, 20, 30, 30, 20, -10, -30,
    -30, -10, 30, 40, 40, 30, -10, -30,
    -30, -10, 30, 40, 40, 30, -10, -30,
    -30, -10, 20, 30, 30, 20, -10, -30,
    -30, -30, 0, 0, 0, 0, -30, -30,
    -50, -30, -30, -30, -30, -30, -30, -50]


def strength(elo):
    """How a bot of this rating plays: (depth, quiescence, random-move chance, noise in centipawns)."""
    if elo < 850:
        return 1, False, 0.22, 160
    if elo < 1050:
        return 1, True, 0.14, 110
    if elo < 1250:
        return 2, False, 0.09, 80
    if elo < 1450:
        return 2, True, 0.05, 55
    if elo < 1650:
        return 2, True, 0.025, 35
    if elo < 1850:
        return 3, True, 0.01, 20
    return 3, True, 0.0, 8


def evaluate(board):
    """Static evaluation in centipawns from the side to move's point of view."""
    pieces = board.piece_map()
    heavy = sum(VALUES[p.piece_type] for p in pieces.values()
                if p.piece_type not in (chess.PAWN, chess.KING))
    endgame = heavy <= 1300
    score = 0
    for sq, p in pieces.items():
        idx = sq ^ 56 if p.color == chess.WHITE else sq
        table = KING_ENDGAME if (p.piece_type == chess.KING and endgame) else PST[p.piece_type]
        v = VALUES[p.piece_type] + table[idx]
        score += v if p.color == chess.WHITE else -v
    return score if board.turn == chess.WHITE else -score


def order_key(board, move):
    key = 0
    if board.is_capture(move):
        victim = board.piece_at(move.to_square)
        attacker = board.piece_at(move.from_square)
        victim_value = VALUES[victim.piece_type] if victim else VALUES[chess.PAWN]
        key += 10 * victim_value - VALUES[attacker.piece_type]
    if move.promotion:
        key += VALUES[move.promotion]
    return -key


class Search:
    def __init__(self, quiescence, node_limit):
        self.quiescence = quiescence
        self.node_limit = node_limit
        self.nodes = 0

    def quiesce(self, board, alpha, beta, depth=4):
        self.nodes += 1
        stand = evaluate(board)
        if stand >= beta or depth == 0 or self.nodes > self.node_limit:
            return stand
        alpha = max(alpha, stand)
        captures = sorted((m for m in board.legal_moves if board.is_capture(m)),
                          key=lambda m: order_key(board, m))
        for m in captures:
            board.push(m)
            score = -self.quiesce(board, -beta, -alpha, depth - 1)
            board.pop()
            if score >= beta:
                return score
            alpha = max(alpha, score)
        return alpha

    def negamax(self, board, depth, alpha, beta):
        self.nodes += 1
        moves = list(board.legal_moves)
        if not moves:
            return -(MATE + depth) if board.is_check() else 0
        if board.is_insufficient_material() or board.halfmove_clock >= 100:
            return 0
        if depth == 0 or self.nodes > self.node_limit:
            return self.quiesce(board, alpha, beta) if self.quiescence else evaluate(board)
        moves.sort(key=lambda m: order_key(board, m))
        best = -INF
        for m in moves:
            board.push(m)
            score = -self.negamax(board, depth - 1, -beta, -alpha)
            board.pop()
            best = max(best, score)
            alpha = max(alpha, score)
            if alpha >= beta:
                break
        return best


def choose_move(board, elo, rng=random):
    """Pick a move for a bot with the given Elo."""
    moves = list(board.legal_moves)
    if len(moves) == 1:
        return moves[0]
    depth, quiescence, random_chance, noise = strength(elo)
    if rng.random() < random_chance:
        return rng.choice(moves)

    search = Search(quiescence, node_limit=40_000)
    moves.sort(key=lambda m: order_key(board, m))
    margin = 4 * noise + 1  # moves worse than this behind the best can't win even with noise
    best_raw = -INF
    scored = []
    for m in moves:
        board.push(m)
        alpha = best_raw - margin if best_raw > -INF else -INF
        raw = -search.negamax(board, depth - 1, -INF, -alpha)
        board.pop()
        best_raw = max(best_raw, raw)
        scored.append((raw + rng.gauss(0, noise) if noise else raw, m))
    return max(scored, key=lambda s: s[0])[1]


def wants_draw(board, bot_color):
    """Bots accept a draw offer only when they are clearly worse."""
    score = evaluate(board)
    if board.turn != bot_color:
        score = -score
    return score < -200
