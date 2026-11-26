# Chess Cards

Play chess against other people. Your opponent's rating is decided by the card you pull from your daily pack, from a Mirror Match (within 10 points of your Elo) up to a Grandmaster card that matches you with a 2000-rated opponent. New players start at 700 Elo.

If no player in your card's range is found within 15 seconds, or you click **Play a bot now**, a bot rated for your card plays you instead. Bots are built in (see `bot.py`). Higher-rated bots look further ahead and make fewer mistakes. Bot ratings never change, and bot games count toward your Elo.

Built with Python (Flask), and all user data is stored in a SQL database (SQLite by default).

## Run it on GitHub (Codespaces)

1. On this repository's GitHub page, click **Code → Codespaces → Create codespace on main**.
2. Wait for it to finish setting up. The server starts on its own, and the site opens in a new tab.
3. To let friends play: open the **Ports** tab, right-click port **5000**, choose **Port Visibility → Public**, and send them the address.

The database is a file inside the codespace. It's kept when the codespace stops and deleted when the codespace is deleted.

## Run it on your computer

```
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000.

## Tests

```
pip install pytest
pytest
```

GitHub Actions runs these tests on every push.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_URL` | `sqlite:///chess_cards.db` | Database to use (for example, a PostgreSQL address) |
| `SECRET_KEY` | generated into `.secret_key` | Signs login cookies |
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Where the server listens |
| `FLASK_DEBUG` | `1` | Set to `0` when other people can reach the server |
