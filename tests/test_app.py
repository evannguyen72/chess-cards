import os
import sys
import tempfile

# Point the app at a throwaway database before importing it.
_db = os.path.join(tempfile.mkdtemp(), "test.db").replace("\\", "/")
os.environ["DATABASE_URL"] = "sqlite:///" + _db
os.environ.setdefault("SECRET_KEY", "test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import Card, app, db  # noqa: E402


def make_player(name):
    client = app.test_client()
    r = client.post("/register", data={"username": name, "password": "secret123"})
    assert r.status_code == 302
    return client


def test_pack_queue_and_full_game():
    a, b = make_player("alice"), make_player("bob")

    card_a = a.post("/api/pack/open").get_json()["card"]
    assert a.post("/api/pack/open").status_code == 400  # one pack per day
    card_b = b.post("/api/pack/open").get_json()["card"]

    # Alice holds a Grandmaster card, Bob a Mirror Match card.
    with app.app_context():
        db.session.get(Card, card_a["id"]).tier = 7
        db.session.get(Card, card_b["id"]).tier = 1
        db.session.commit()

    # Cancelling a search returns the card.
    assert a.post("/api/queue/join", json={"card_id": card_a["id"]}).get_json()["queued"]
    a.post("/api/queue/leave")
    assert [c["status"] for c in a.get("/api/me").get_json()["cards"]] == ["owned"]

    a.post("/api/queue/join", json={"card_id": card_a["id"]})
    game_id = b.post("/api/queue/join", json={"card_id": card_b["id"]}).get_json()["game_id"]
    assert game_id
    assert a.get("/api/queue/status").get_json()["game_id"] == game_id

    state = a.get(f"/api/game/{game_id}").get_json()
    white, black = (a, b) if state["my_color"] == "white" else (b, a)
    assert black.post(f"/api/game/{game_id}/move", json={"move": "e2e4"}).status_code == 400
    assert white.post(f"/api/game/{game_id}/move", json={"move": "e2e5"}).status_code == 400

    for move, player in [("f2f3", white), ("e7e5", black), ("g2g4", white), ("d8h4", black)]:
        assert player.post(f"/api/game/{game_id}/move", json={"move": move}).status_code == 200

    final = white.get(f"/api/game/{game_id}").get_json()
    assert final["result"] == "0-1"
    assert final["termination"] == "Checkmate"
    assert final["white"]["elo_change"] == -final["black"]["elo_change"]
    assert a.get("/api/me").get_json()["cards"] == []  # cards are used up
    assert a.get("/dashboard").status_code == 200
    assert a.get(f"/game/{game_id}").status_code == 200


def test_new_players_start_at_700():
    c = make_player("carol")
    assert c.get("/api/me").get_json()["user"]["elo"] == 700


def test_grandmaster_card_plays_2000_bot():
    app.config["BOT_SYNC"] = True
    c = make_player("dave")
    card = c.post("/api/pack/open").get_json()["card"]
    with app.app_context():
        db.session.get(Card, card["id"]).tier = 7
        db.session.commit()
    assert c.post("/api/queue/join", json={"card_id": card["id"]}).get_json()["queued"]
    game_id = c.post("/api/queue/bot").get_json()["game_id"]

    state = c.get(f"/api/game/{game_id}").get_json()
    me = state["my_color"]
    bot = state["black" if me == "white" else "white"]
    assert bot["is_bot"] and bot["elo"] == 2000
    # The bot always replies (or opens, if it's white) before control returns to us.
    assert state["turn"] == me
    move = state["legal_moves"][0]
    after = c.post(f"/api/game/{game_id}/move", json={"move": move}).get_json()
    assert after["status"] != "active" or after["turn"] == me
    assert len(after["san_moves"]) == len(state["san_moves"]) + 2

    final = c.post(f"/api/game/{game_id}/resign").get_json()
    assert final["status"] == "finished"
    # Losing to a much stronger opponent costs (almost) nothing, but never gains.
    assert final[me]["elo_change"] <= 0 and bot["elo"] == 2000
    assert final["black" if me == "white" else "white"]["elo_change"] == 0


def test_bots_cannot_log_in_and_names_are_reserved():
    c = app.test_client()
    r = c.post("/register", data={"username": "Bot_999", "password": "secret123"}, follow_redirects=True)
    assert b"reserved for bots" in r.data
    r = c.post("/login", data={"username": "Bot_2000", "password": "!"}, follow_redirects=True)
    assert b"Invalid username or password" in r.data


def test_change_username_must_be_unique():
    e = make_player("erin")
    make_player("frank")

    r = e.post("/settings", data={"username": "FRANK"}, follow_redirects=True)
    assert b"That username is taken" in r.data

    r = e.post("/settings", data={"username": "erin_the_great"}, follow_redirects=True)
    assert b"Your username is now erin_the_great" in r.data
    assert e.get("/api/me").get_json()["user"]["username"] == "erin_the_great"

    # Changing only the capitalization of your own name is allowed.
    r = e.post("/settings", data={"username": "Erin_The_Great"}, follow_redirects=True)
    assert b"Your username is now Erin_The_Great" in r.data

    # Registering with a name that differs only by case is refused.
    other = app.test_client()
    r = other.post("/register", data={"username": "erin_the_GREAT", "password": "secret123"},
                   follow_redirects=True)
    assert b"That username is taken" in r.data
