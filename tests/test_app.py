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
