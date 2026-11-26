"""
Chess Cards - play chess against other people (or rated bots), but your
opponent's rating is decided by the card you pull from your daily pack.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000
"""
import os
import random
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import chess
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, or_, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

import bot as engine

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
STARTING_ELO = 700
K_FACTOR = 32
BOT_WAIT_SECONDS = 15   # how long to search for a human before a bot steps in
BOT_PREFIX = "Bot_"     # reserved username prefix for bots
GM_ELO = 2000


def load_secret_key():
    """Use SECRET_KEY env var, otherwise create one and keep it in a local file."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(BASE_DIR, ".secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    return key


app = Flask(__name__)
app.config["SECRET_KEY"] = load_secret_key()
# SQLite by default; set DATABASE_URL to use Postgres/MySQL instead.
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "chess_cards.db")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["BOT_SYNC"] = False  # tests set this so bot moves happen inside the request
db = SQLAlchemy(app)

# ---------------------------------------------------------------------------
# The 7 cards. min/max are offsets added to YOUR elo when you play the card.
# The Grandmaster card is different: it always summons a 2000-rated opponent.
# ---------------------------------------------------------------------------
CARD_TIERS = [
    {"tier": 1, "name": "Mirror Match",         "rarity": "Common",      "min": -10, "max": 10,  "weight": 35, "color": "#9ca3af"},
    {"tier": 2, "name": "Rising Challenger",    "rarity": "Uncommon",    "min": 10,  "max": 40,  "weight": 25, "color": "#22c55e"},
    {"tier": 3, "name": "Club Veteran",         "rarity": "Rare",        "min": 40,  "max": 80,  "weight": 16, "color": "#3b82f6"},
    {"tier": 4, "name": "Expert",               "rarity": "Epic",        "min": 80,  "max": 120, "weight": 11, "color": "#a855f7"},
    {"tier": 5, "name": "Candidate Master",     "rarity": "Legendary",   "min": 120, "max": 160, "weight": 7,  "color": "#f59e0b"},
    {"tier": 6, "name": "International Master", "rarity": "Mythic",      "min": 160, "max": 200, "weight": 4,  "color": "#ef4444"},
    {"tier": 7, "name": "Grandmaster",          "rarity": "Grandmaster", "fixed": GM_ELO,         "weight": 2,  "color": "#facc15"},
]
for _t in CARD_TIERS:
    _t["label"] = f"{_t['fixed']} Elo" if "fixed" in _t else f"{_t['min']:+d} to {_t['max']:+d}"
TIERS = {t["tier"]: t for t in CARD_TIERS}


def card_range(tier, elo):
    """(lowest, highest) opponent Elo this card can match you with."""
    t = TIERS[tier]
    if "fixed" in t:
        return t["fixed"], t["fixed"] + 200  # 2000-rated bot, or any human 2000-2200
    return elo + t["min"], elo + t["max"]


def range_label(tier, elo):
    lo, hi = card_range(tier, elo)
    return f"{lo}+" if "fixed" in TIERS[tier] else f"{lo}–{hi}"


app.jinja_env.globals["range_label"] = range_label


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def seconds_until_next_pack():
    now = utcnow()
    tomorrow = datetime(now.year, now.month, now.day) + timedelta(days=1)
    return int((tomorrow - now).total_seconds())


# ---------------------------------------------------------------------------
# Database models
# ---------------------------------------------------------------------------
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    elo = db.Column(db.Integer, nullable=False, default=STARTING_ELO)
    wins = db.Column(db.Integer, nullable=False, default=0)
    losses = db.Column(db.Integer, nullable=False, default=0)
    draws = db.Column(db.Integer, nullable=False, default=0)
    is_bot = db.Column(db.Boolean, nullable=False, default=False)
    last_pack_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    # Usernames are unique ignoring case ("Evan" and "evan" can't both exist).
    __table_args__ = (db.Index("uq_users_username_lower", db.func.lower(username), unique=True),)

    cards = db.relationship("Card", backref="owner", lazy=True)

    def to_dict(self):
        return {"id": self.id, "username": self.username, "elo": self.elo,
                "wins": self.wins, "losses": self.losses, "draws": self.draws}


class Card(db.Model):
    __tablename__ = "cards"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    tier = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(10), nullable=False, default="owned")  # owned | queued | used
    obtained_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    used_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self, elo=None):
        t = TIERS[self.tier]
        d = {"id": self.id, "tier": self.tier, "name": t["name"], "rarity": t["rarity"],
             "label": t["label"], "color": t["color"], "status": self.status,
             "obtained_at": self.obtained_at.isoformat() + "Z"}
        if elo is not None:
            d["opponent"] = range_label(self.tier, elo)
        return d


class QueueEntry(db.Model):
    __tablename__ = "queue"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    card_id = db.Column(db.Integer, db.ForeignKey("cards.id"), nullable=False)
    elo_at_queue = db.Column(db.Integer, nullable=False)
    min_elo = db.Column(db.Integer, nullable=False)
    max_elo = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User")
    card = db.relationship("Card")


class Game(db.Model):
    __tablename__ = "games"
    id = db.Column(db.Integer, primary_key=True)
    white_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    black_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    white_card_tier = db.Column(db.Integer, nullable=False, default=0)  # 0 = bot (no card)
    black_card_tier = db.Column(db.Integer, nullable=False, default=0)
    white_elo_before = db.Column(db.Integer, nullable=False)
    black_elo_before = db.Column(db.Integer, nullable=False)
    white_elo_change = db.Column(db.Integer, nullable=True)
    black_elo_change = db.Column(db.Integer, nullable=True)
    moves = db.Column(db.Text, nullable=False, default="")  # space separated UCI moves
    fen = db.Column(db.String(100), nullable=False, default=chess.STARTING_FEN)
    status = db.Column(db.String(10), nullable=False, default="active")  # active | finished
    result = db.Column(db.String(8), nullable=True)         # 1-0 | 0-1 | 1/2-1/2
    termination = db.Column(db.String(30), nullable=True)
    draw_offered_by = db.Column(db.String(5), nullable=True)  # white | black
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    white = db.relationship("User", foreign_keys=[white_id])
    black = db.relationship("User", foreign_keys=[black_id])

    def move_list(self):
        return self.moves.split() if self.moves else []

    def board(self):
        board = chess.Board()
        for uci in self.move_list():
            board.push_uci(uci)
        return board

    def color_of(self, user_id):
        if user_id == self.white_id:
            return "white"
        if user_id == self.black_id:
            return "black"
        return None

    def bot_color(self):
        if self.white.is_bot:
            return "white"
        if self.black.is_bot:
            return "black"
        return None


def migrate():
    """Bring databases created by older versions up to date."""
    with db.engine.begin() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("users")}
        if "is_bot" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_bot BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_lower "
                          "ON users (lower(username))"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def current_user():
    uid = session.get("user_id")
    return db.session.get(User, uid) if uid else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify(error="Not logged in"), 401
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def username_error(name, user_id=None):
    """Return a message explaining why `name` can't be used, or None if it's fine."""
    if not (3 <= len(name) <= 32) or not name.replace("_", "").isalnum() or not name.isascii():
        return "Username must be 3-32 letters, numbers or underscores."
    if name.lower().startswith(BOT_PREFIX.lower()):
        return f'Usernames starting with "{BOT_PREFIX}" are reserved for bots.'
    taken = User.query.filter(db.func.lower(User.username) == name.lower())
    if user_id is not None:
        taken = taken.filter(User.id != user_id)
    if taken.first():
        return "That username is taken."
    return None


def active_game_for(user_id):
    return (Game.query.filter(Game.status == "active",
                              or_(Game.white_id == user_id, Game.black_id == user_id))
            .order_by(Game.created_at.desc()).first())


def use_cards(*entries):
    now = utcnow()
    for e in entries:
        e.card.status = "used"
        e.card.used_at = now
        db.session.delete(e)


def try_match(entry):
    """Look for another queued player this entry can be paired with.

    A pair is valid if EITHER player's card range contains the other player's
    elo. That lets a high-tier card "summon" a stronger opponent who is waiting
    in the queue.
    """
    others = (QueueEntry.query.filter(QueueEntry.user_id != entry.user_id)
              .order_by(QueueEntry.created_at).all())
    for other in others:
        mine_fits = entry.min_elo <= other.elo_at_queue <= entry.max_elo
        theirs_fits = other.min_elo <= entry.elo_at_queue <= other.max_elo
        if not (mine_fits or theirs_fits):
            continue

        a, b = (entry, other) if random.random() < 0.5 else (other, entry)
        game = Game(white_id=a.user_id, black_id=b.user_id,
                    white_card_tier=a.card.tier, black_card_tier=b.card.tier,
                    white_elo_before=a.user.elo, black_elo_before=b.user.elo)
        db.session.add(game)
        use_cards(entry, other)
        db.session.flush()
        return game
    return None


def get_bot(elo):
    """Find (or create) the bot with exactly this rating."""
    name = f"{BOT_PREFIX}{elo}"
    bot_user = User.query.filter_by(username=name).first()
    if not bot_user:
        bot_user = User(username=name, password_hash="!", elo=elo, is_bot=True)
        db.session.add(bot_user)
        db.session.flush()
    return bot_user


def match_with_bot(entry):
    """Pair a queued player with a bot whose rating fits their card."""
    if "fixed" in TIERS[entry.card.tier]:
        target = TIERS[entry.card.tier]["fixed"]
    else:
        target = random.randint(entry.min_elo, entry.max_elo)
    bot_user = get_bot(max(100, target))
    human, tier = entry.user, entry.card.tier
    if random.random() < 0.5:
        game = Game(white_id=human.id, black_id=bot_user.id, white_card_tier=tier, black_card_tier=0,
                    white_elo_before=human.elo, black_elo_before=bot_user.elo)
    else:
        game = Game(white_id=bot_user.id, black_id=human.id, white_card_tier=0, black_card_tier=tier,
                    white_elo_before=bot_user.elo, black_elo_before=human.elo)
    db.session.add(game)
    use_cards(entry)
    db.session.flush()
    return game


def expected_score(rating, opponent):
    return 1 / (1 + 10 ** ((opponent - rating) / 400))


def finish_game(game, result, termination):
    if game.status == "finished":
        return
    white, black = game.white, game.black
    white_score = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}[result]
    change = round(K_FACTOR * (white_score - expected_score(white.elo, black.elo)))

    # Bots have fixed ratings and no record; only the human's Elo moves.
    for player, delta, score in ((white, change, white_score), (black, -change, 1 - white_score)):
        if player.is_bot:
            delta = 0
        else:
            player.elo += delta
            if score == 1:
                player.wins += 1
            elif score == 0:
                player.losses += 1
            else:
                player.draws += 1
        if player is white:
            game.white_elo_change = delta
        else:
            game.black_elo_change = delta

    game.status = "finished"
    game.result = result
    game.termination = termination
    game.draw_offered_by = None


def apply_move(game, board, move):
    board.push(move)
    game.moves = (game.moves + " " + move.uci()).strip()
    game.fen = board.fen()
    game.draw_offered_by = None
    outcome = board.outcome(claim_draw=True)
    if outcome:
        finish_game(game, outcome.result(), outcome.termination.name.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Bot move scheduling
# ---------------------------------------------------------------------------
_bot_lock = threading.Lock()
_bot_busy = set()


def bot_to_move(game):
    if game.status != "active":
        return False
    color = game.bot_color()
    return color is not None and (game.board().turn == chess.WHITE) == (color == "white")


def kick_bot(game):
    """If it's the bot's turn, start it thinking (in the background)."""
    if not bot_to_move(game):
        return
    with _bot_lock:
        if game.id in _bot_busy:
            return
        _bot_busy.add(game.id)
    if app.config["BOT_SYNC"]:
        run_bot_move(game.id)
        db.session.expire_all()
    else:
        threading.Thread(target=run_bot_move, args=(game.id,), daemon=True).start()


def run_bot_move(game_id):
    try:
        with app.app_context():
            game = db.session.get(Game, game_id)
            if not bot_to_move(game):
                return
            started = time.time()
            board = game.board()
            seen = len(game.move_list())
            bot_user = game.white if game.bot_color() == "white" else game.black
            move = engine.choose_move(board, bot_user.elo)
            if not app.config["BOT_SYNC"]:
                time.sleep(max(0.0, 0.7 - (time.time() - started)))  # feel less instant
            db.session.refresh(game)
            if game.status != "active" or len(game.move_list()) != seen:
                return  # game changed while thinking (e.g. the human resigned)
            apply_move(game, board, move)
            db.session.commit()
    finally:
        with _bot_lock:
            _bot_busy.discard(game_id)


def game_state(game, user):
    board = chess.Board()
    san_moves = []
    for uci in game.move_list():
        move = chess.Move.from_uci(uci)
        san_moves.append(board.san(move))
        board.push(move)

    my_color = game.color_of(user.id)
    turn = "white" if board.turn == chess.WHITE else "black"
    legal = []
    if game.status == "active" and my_color == turn:
        legal = [m.uci() for m in board.legal_moves]

    def player(u, tier, before, change):
        card = TIERS.get(tier)
        return {"username": u.username, "elo": u.elo, "elo_before": before,
                "elo_change": change, "is_bot": u.is_bot,
                "card": card["name"] if card else "Bot",
                "card_color": card["color"] if card else "#8b93a3"}

    moves = game.move_list()
    return {
        "id": game.id,
        "fen": board.fen(),
        "turn": turn,
        "my_color": my_color,
        "legal_moves": legal,
        "san_moves": san_moves,
        "last_move": moves[-1] if moves else None,
        "in_check": board.is_check(),
        "status": game.status,
        "result": game.result,
        "termination": game.termination,
        "draw_offered_by": game.draw_offered_by,
        "white": player(game.white, game.white_card_tier, game.white_elo_before, game.white_elo_change),
        "black": player(game.black, game.black_card_tier, game.black_elo_before, game.black_elo_change),
    }


def load_game_for_player(game_id):
    user = current_user()
    game = db.session.get(Game, game_id)
    if not game:
        abort(404)
    color = game.color_of(user.id)
    if not color:
        abort(403)
    return user, game, color


# ---------------------------------------------------------------------------
# Pages & auth
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("index.html", tiers=CARD_TIERS, starting_elo=STARTING_ELO)


@app.post("/register")
def register():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    error = username_error(username)
    if not error and len(password) < 6:
        error = "Password must be at least 6 characters."
    if error:
        flash(error)
        return redirect(url_for("index"))
    user = User(username=username, password_hash=generate_password_hash(password))
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:  # someone grabbed the name at the same moment
        db.session.rollback()
        flash("That username is taken.")
        return redirect(url_for("index"))
    session.clear()
    session["user_id"] = user.id
    return redirect(url_for("dashboard"))


@app.post("/login")
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = User.query.filter(db.func.lower(User.username) == username.lower()).first()
    if not user or user.is_bot or not check_password_hash(user.password_hash, password):
        flash("Invalid username or password.")
        return redirect(url_for("index"))
    session.clear()
    session["user_id"] = user.id
    return redirect(url_for("dashboard"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = current_user()
    if request.method == "POST":
        new_name = request.form.get("username", "").strip()
        if new_name == user.username:
            return redirect(url_for("settings"))
        error = username_error(new_name, user_id=user.id)
        if error:
            flash(error)
            return redirect(url_for("settings"))
        user.username = new_name
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("That username is taken.")
            return redirect(url_for("settings"))
        flash(f"Your username is now {new_name}.", "ok")
        return redirect(url_for("settings"))
    return render_template("settings.html", user=user)


@app.get("/dashboard")
@login_required
def dashboard():
    user = current_user()
    leaders = User.query.filter_by(is_bot=False).order_by(User.elo.desc()).limit(10).all()
    recent = (Game.query.filter(Game.status == "finished",
                                or_(Game.white_id == user.id, Game.black_id == user.id))
              .order_by(Game.updated_at.desc()).limit(10).all())
    return render_template("dashboard.html", user=user, tiers=CARD_TIERS,
                           leaders=leaders, recent=recent, bot_wait=BOT_WAIT_SECONDS)


@app.get("/game/<int:game_id>")
@login_required
def game_page(game_id):
    user, game, color = load_game_for_player(game_id)
    return render_template("game.html", user=user, game=game)


# ---------------------------------------------------------------------------
# JSON API: packs, cards, queue
# ---------------------------------------------------------------------------
@app.get("/api/me")
@login_required
def api_me():
    user = current_user()
    today = utcnow().date()
    cards = (Card.query.filter(Card.user_id == user.id, Card.status != "used")
             .order_by(Card.tier.desc(), Card.obtained_at).all())
    entry = QueueEntry.query.filter_by(user_id=user.id).first()
    game = active_game_for(user.id)
    return jsonify(
        user=user.to_dict(),
        pack_available=user.last_pack_date != today,
        next_pack_seconds=seconds_until_next_pack(),
        cards=[c.to_dict(user.elo) for c in cards],
        queue=({"card": entry.card.to_dict(entry.elo_at_queue),
                "since": entry.created_at.isoformat() + "Z"} if entry else None),
        active_game=game.id if game else None,
        bot_wait_seconds=BOT_WAIT_SECONDS,
    )


@app.post("/api/pack/open")
@login_required
def api_open_pack():
    user = current_user()
    today = utcnow().date()
    # Atomic "claim today's pack" so double clicks can't open two packs.
    claimed = (User.query.filter(User.id == user.id,
                                 or_(User.last_pack_date.is_(None), User.last_pack_date != today))
               .update({User.last_pack_date: today}, synchronize_session=False))
    if not claimed:
        db.session.rollback()
        return jsonify(error="You already opened today's pack.",
                       next_pack_seconds=seconds_until_next_pack()), 400

    tier = random.choices([t["tier"] for t in CARD_TIERS],
                          weights=[t["weight"] for t in CARD_TIERS])[0]
    card = Card(user_id=user.id, tier=tier)
    db.session.add(card)
    db.session.commit()
    db.session.refresh(user)
    return jsonify(card=card.to_dict(user.elo), next_pack_seconds=seconds_until_next_pack())


@app.post("/api/queue/join")
@login_required
def api_queue_join():
    user = current_user()
    data = request.get_json(silent=True) or {}
    if active_game_for(user.id):
        return jsonify(error="Finish your current game first."), 400
    if QueueEntry.query.filter_by(user_id=user.id).first():
        return jsonify(error="You are already searching for a match."), 400

    card = Card.query.filter_by(id=data.get("card_id"), user_id=user.id, status="owned").first()
    if not card:
        return jsonify(error="You don't have that card."), 400

    lo, hi = card_range(card.tier, user.elo)
    entry = QueueEntry(user_id=user.id, card_id=card.id, elo_at_queue=user.elo,
                       min_elo=lo, max_elo=hi)
    card.status = "queued"
    db.session.add(entry)
    db.session.flush()
    game = try_match(entry)
    db.session.commit()
    return jsonify(queued=game is None, game_id=game.id if game else None)


@app.post("/api/queue/leave")
@login_required
def api_queue_leave():
    user = current_user()
    entry = QueueEntry.query.filter_by(user_id=user.id).first()
    if entry:
        entry.card.status = "owned"  # card goes back to your hand
        db.session.delete(entry)
        db.session.commit()
    return jsonify(ok=True)


@app.post("/api/queue/bot")
@login_required
def api_queue_bot():
    """Stop waiting for a human and play a bot rated for your card right away."""
    user = current_user()
    entry = QueueEntry.query.filter_by(user_id=user.id).first()
    if not entry:
        return jsonify(error="You aren't searching for a match."), 400
    game = match_with_bot(entry)
    db.session.commit()
    return jsonify(queued=False, game_id=game.id)


@app.get("/api/queue/status")
@login_required
def api_queue_status():
    user = current_user()
    entry = QueueEntry.query.filter_by(user_id=user.id).first()
    if entry:
        game = try_match(entry)
        if not game and utcnow() - entry.created_at >= timedelta(seconds=BOT_WAIT_SECONDS):
            game = match_with_bot(entry)
        db.session.commit()
        if game:
            return jsonify(queued=False, game_id=game.id)
        waiting = QueueEntry.query.count() - 1
        return jsonify(queued=True, game_id=None, others_waiting=waiting)
    game = active_game_for(user.id)
    return jsonify(queued=False, game_id=game.id if game else None)


# ---------------------------------------------------------------------------
# JSON API: games
# ---------------------------------------------------------------------------
@app.get("/api/game/<int:game_id>")
@login_required
def api_game(game_id):
    user, game, _ = load_game_for_player(game_id)
    kick_bot(game)  # resumes a bot that was interrupted (e.g. by a server restart)
    return jsonify(game_state(game, user))


@app.post("/api/game/<int:game_id>/move")
@login_required
def api_move(game_id):
    user, game, color = load_game_for_player(game_id)
    if game.status != "active":
        return jsonify(error="Game is over."), 400
    board = game.board()
    if (board.turn == chess.WHITE) != (color == "white"):
        return jsonify(error="It's not your turn."), 400

    uci = (request.get_json(silent=True) or {}).get("move", "")
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return jsonify(error="Bad move format."), 400
    if move not in board.legal_moves:
        return jsonify(error="Illegal move."), 400

    apply_move(game, board, move)
    db.session.commit()
    kick_bot(game)
    return jsonify(game_state(game, user))


@app.post("/api/game/<int:game_id>/resign")
@login_required
def api_resign(game_id):
    user, game, color = load_game_for_player(game_id)
    if game.status == "active":
        finish_game(game, "0-1" if color == "white" else "1-0", "Resignation")
        db.session.commit()
    return jsonify(game_state(game, user))


@app.post("/api/game/<int:game_id>/draw")
@login_required
def api_draw(game_id):
    user, game, color = load_game_for_player(game_id)
    if game.status != "active":
        return jsonify(error="Game is over."), 400
    action = (request.get_json(silent=True) or {}).get("action")
    bot_color = game.bot_color()
    if action == "offer" and bot_color:
        chess_color = chess.WHITE if bot_color == "white" else chess.BLACK
        if not engine.wants_draw(game.board(), chess_color):
            return jsonify(error="The bot declined your draw offer."), 400
        finish_game(game, "1/2-1/2", "Draw Agreed")
    elif action == "offer":
        game.draw_offered_by = color
    elif action == "accept" and game.draw_offered_by and game.draw_offered_by != color:
        finish_game(game, "1/2-1/2", "Draw Agreed")
    elif action == "decline" and game.draw_offered_by and game.draw_offered_by != color:
        game.draw_offered_by = None
    else:
        return jsonify(error="Invalid draw action."), 400
    db.session.commit()
    return jsonify(game_state(game, user))


with app.app_context():
    db.create_all()
    migrate()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG", "1") == "1",
        threaded=True,
    )
