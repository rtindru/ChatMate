# Add these imports at the top
import chess.svg
from fastapi.responses import Response, StreamingResponse
import io


# server.py

import requests
import urllib
from fastapi.responses import JSONResponse
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from typing import Optional
import chess
import chess.engine
import os
import re
import json
import logging

logger = logging.getLogger("uvicorn")
logger.setLevel(logging.INFO)

from fastapi.middleware.cors import CORSMiddleware

# Optional: for PNG conversion
try:
    import cairosvg

    CAIROSVG_AVAILABLE = True
except ImportError:
    CAIROSVG_AVAILABLE = False

# Environment variables
LICHESS_TOKEN = os.environ.get("LICHESS_TOKEN")
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "./stockfish")
STATIC_PATH = os.environ.get("STAIC_PATH", "static")
FEN2PNG_BASE = "https://fen2png.com/api/"

os.makedirs(STATIC_PATH, exist_ok=True)

app = FastAPI(title="Chess Analysis Server")
app.mount(
    "/static",
    StaticFiles(directory=STATIC_PATH, html=False),
    name="static",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def analyze_position(fen: str, depth: int = 18, multipv: int = 1, plies: int = 5):
    with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as engine:
        root_board = chess.Board(fen)

        # ask for multiple lines
        info = engine.analyse(
            root_board,
            chess.engine.Limit(depth=depth),
            multipv=multipv,
        )

        # engine.analyse can return a dict if multipv==1
        if isinstance(info, dict):
            info = [info]

        enriched = []
        for line in info:
            pv = line.get("pv", [])
            if not pv:
                enriched.append(
                    {
                        "depth": line.get("depth"),
                        "score_cp": line["score"].white().score(mate_score=10000),
                        "pv_san": [],
                        "nps": line.get("nps"),
                        "nodes": line.get("nodes"),
                    }
                )
                continue

            # walk the PV from the same root FEN, pushing as we go
            temp_board = chess.Board(fen)
            pv_san = []
            for mv in pv[:plies]:  # return up to 10 plies if you like
                if mv not in temp_board.legal_moves:
                    raise
                pv_san.append(temp_board.san(mv))
                temp_board.push(mv)

            enriched.append(
                {
                    "depth": line.get("depth"),
                    "score_cp": line["score"].white().score(mate_score=10000),
                    "pv_san": pv_san,
                    "nps": line.get("nps"),
                    "nodes": line.get("nodes"),
                    "multipv": line.get("multipv"),
                }
            )

    return enriched


# -------------------------------
# 1️⃣ Fetch last N games
# -------------------------------


@app.get("/lichess/last_games")
def get_last_games(
    username: str = Query(...),
    max: int = Query(5),
    since: Optional[int] = Query(
        None, description="Fetch games since Unix timestamp (ms)"
    ),
    until: Optional[int] = Query(
        None, description="Fetch games until Unix timestamp (ms)"
    ),
    color: Optional[str] = Query(
        None, description="Filter by color: 'white' or 'black'"
    ),
):
    url = f"https://lichess.org/api/games/user/{username}"
    headers = {
        "Authorization": f"Bearer {LICHESS_TOKEN}",
        "Accept": "application/x-ndjson",
    }
    query_params = {
        "max": max,
        "analysed": True,
        "pgnInJson": True,
        "evals": True,
        "since": since,
        "until": until,
        "color": color if color in ("white", "black") else None,
    }

    # Filter out None values automatically
    params = {k: v for k, v in query_params.items() if v is not None}
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    try:
        resp = requests.get(url, headers=headers, params=params, stream=True)
        resp.raise_for_status()
        games = [json.loads(line) for line in resp.iter_lines() if line]
        return {"games": games}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------
# 3️⃣ Identify top N critical positions (blunders/missed wins)
# -------------------------------
def _eval_to_pawns(item):
    """Return (eval_in_pawns, mate_in) from a Lichess analysis entry."""
    if "eval" in item and item["eval"] is not None:
        return float(item["eval"]) / 100.0, None  # cp -> pawns
    if "mate" in item and item["mate"] is not None:
        m = int(item["mate"])
        # map mate to large +/- value for swing detection; keep mate count
        return (100.0 if m > 0 else -100.0), m
    return 0.0, None


def extract_critical_positions_from_lichess_analysis(
    data: dict,
    centipawn_threshold: float = 50,
):
    """
    Uses Lichess 'moves' (SAN) + 'analysis' (eval/mate/judgment) to find critical moments,
    separated for White and Black.
    threshold is in pawns (e.g., 2.0).
    """
    moves_san = (data.get("moves") or "").split()
    analysis = data.get("analysis") or []
    n = min(len(moves_san), len(analysis))
    board = chess.Board()

    # Instead of a single list, separate by color
    critical_white = []
    critical_black = []

    prev_eval = 0.0
    prev_mate_in = None

    for i in range(n):
        prev_fen = board.fen()
        san = moves_san[i]
        item = analysis[i]
        cur_eval, mate_in = _eval_to_pawns(item)

        # Determine whose move this is
        is_white_move = i % 2 == 0

        # advance board with SAN
        try:
            move = board.parse_san(san)
        except Exception:
            continue
        board.push(move)
        new_fen = board.fen()

        delta = abs(cur_eval - prev_eval)
        judgment = (item.get("judgment") or {}).get("name", "")
        flagged = judgment in {"Mistake", "Blunder"}
        missed_or_allowed_mate = (prev_mate_in is not None) ^ (mate_in is not None)

        if delta >= centipawn_threshold or flagged or missed_or_allowed_mate:
            entry = {
                "ply": i + 1,
                "move_number": board.fullmove_number,  # after pushing move
                "move": san,
                "fen": prev_fen,  # position *before* the move
                "eval_after_move": round(cur_eval, 2),
                "eval": round(prev_eval, 2),
                "eval_delta": round(delta, 2),
                "mate_in": mate_in,  # e.g., 2, -1, or None
                "judgment": judgment or None,
                "comment": (item.get("judgment") or {}).get("comment"),
                "fen_image_url": get_fen_url(prev_fen),
                "post_move_fen": new_fen,
                "post_move_fen_image_url": get_fen_url(new_fen),
            }

            if is_white_move:
                critical_white.append(entry)
            else:
                critical_black.append(entry)

        prev_eval, prev_mate_in = cur_eval, mate_in

    # sort & limit separately
    for crit_list in (critical_white, critical_black):
        crit_list.sort(
            key=lambda x: (x["mate_in"] is not None, x["eval_delta"]), reverse=True
        )

    return {"white": critical_white, "black": critical_black}


# -------------------------------
# API Endpoint: Get critical positions by game ID
# -------------------------------
@app.get("/lichess/critical_positions")
def get_critical_positions(
    gameID: str = Query(..., description="Lichess game ID"), threshold: float = 50
):
    url = f"https://lichess.org/game/export/{gameID}"
    headers = {
        "Authorization": f"Bearer {LICHESS_TOKEN}",
        "Accept": "application/json",
    }
    params = {
        "moves": True,
        "analysed": True,
        "pgnInJson": True,
        "clocks": False,
    }

    try:
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching game: {str(e)}")
    critical_positions = extract_critical_positions_from_lichess_analysis(
        data, centipawn_threshold=threshold
    )
    return {
        "game_id": gameID,
        "threshold": threshold,
        "critical_positions": critical_positions,
    }


@app.get("/stockfish/analyze")
def evaluate_position(fen: str, depth: int = 18, multipv: int = 3):
    analysis = analyze_position(fen, depth, multipv)
    return {
        "fen": fen,
        "analysis": analysis,
        "fen_image_url": get_fen_url(fen),
    }


def get_fen_url(fen: str):
    # Build the external API request
    encoded_fen = maybe_encode_fen(fen)
    # Construct URL manually since fen2png uses query params directly in path
    image_url = f"{FEN2PNG_BASE}?raw=true&fen={encoded_fen}"
    return image_url
    # resp = requests.get(image_url, timeout=10)
    # resp.raise_for_status()
    # return resp.url


def maybe_encode_fen(fen: str) -> str:
    # Detect existing URL-encoding patterns
    if re.search(r"%[0-9A-Fa-f]{2}", fen):
        # Already encoded → return as-is
        return fen
    else:
        # Not encoded → encode safely
        return urllib.parse.quote(fen)


@app.get("/render/fen")
def render_fen(fen: str, perspective: str = "white", size: int = 400):
    """
    Render a chess position locally (300px width PNG, compressed for speed).
    Optionally fix the board perspective for 'white' or 'black'.
    """
    return {"fen": fen, "fen_image_url": render_fen(fen)}


@app.get("/chess/play_san")
def play_san_moves(fen: str, moves: str):
    """
    Given a FEN and a space-separated list of SAN moves,
    return the resulting FEN and FEN image URLs after each move.
    """
    board = chess.Board(fen)
    san_moves = moves.split()
    positions = []

    for san in san_moves:
        try:
            old_fen = board.fen()
            move = board.parse_san(san)
            board.push(move)
            new_fen = board.fen()
            analysis = analyze_position(new_fen, depth=18, multipv=1, plies=5)
            positions.append(
                {
                    "move": san,
                    "fen": old_fen,
                    "fen_image_url": get_fen_url(old_fen),
                    "post_move_fen": new_fen,
                    "post_move_fen_image_url": get_fen_url(new_fen),
                    "analysis": analysis,
                }
            )
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid or illegal move '{san}': {e}"
            )

    return {
        "starting_fen": fen,
        "positions": positions,
    }
