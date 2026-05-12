# 🎙️ Telegram Voice Recording Bot

A Telegram bot that collects voice recordings of people reading sentences.  
Each recording is saved with full metadata (user, timestamp, duration, file path).

---

## ✨ Features

| Feature | Details |
|---|---|
| Sign-in methods | Phone number (Telegram contact share) **or** Telegram username |
| Sentence display | Shows one sentence at a time from a configurable pool |
| Voice recording | User sends a voice message; bot downloads and stores the `.ogg` file |
| Metadata storage | SQLite database + sidecar `.json` file per recording |
| Stats command | Users can see how many recordings they've made |
| Admin command | `/admin` shows global totals (users, recordings, total audio time) |

---

## 📁 File Structure

```
telegram_voice_bot/
├── bot.py               ← main bot code
├── requirements.txt
├── .env.example         ← copy to .env and add your token
└── data/                ← created automatically on first run
    ├── recordings.db    ← SQLite database
    └── voices/
        ├── 123_20240101_120000_0.ogg   ← voice file
        └── 123_20240101_120000_0.json  ← sidecar metadata
```

---

## 🚀 Quick Start

### 1. Get a Bot Token

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the token you receive (looks like `1234567890:ABCdef...`)

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set your token

**Option A — environment variable (recommended):**
```bash
export BOT_TOKEN="your_token_here"
```

**Option B — edit bot.py directly:**
```python
BOT_TOKEN = "your_token_here"   # line ~20
```

**Option C — .env file with python-dotenv:**
```bash
cp .env.example .env
# edit .env and fill in your token
pip install python-dotenv
```
Then add at the top of `bot.py`:
```python
from dotenv import load_dotenv
load_dotenv()
```

### 4. Run the bot

```bash
python bot.py
```

---

## 🗄️ Database Schema

### `users` table

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Auto-increment primary key |
| `telegram_id` | INTEGER | Telegram user ID (unique) |
| `username` | TEXT | Telegram or chosen username |
| `phone` | TEXT | Phone number (if shared) |
| `auth_method` | TEXT | `"phone"` or `"username"` |
| `first_name` | TEXT | From Telegram profile |
| `last_name` | TEXT | From Telegram profile |
| `joined_at` | TEXT | ISO-8601 timestamp |

### `recordings` table

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Auto-increment primary key |
| `user_id` | INTEGER | FK → users.id |
| `telegram_id` | INTEGER | Telegram user ID |
| `sentence` | TEXT | The sentence that was read |
| `sentence_index` | INTEGER | Index in SENTENCES list |
| `file_id` | TEXT | Telegram file ID |
| `file_path` | TEXT | Local path to `.ogg` file |
| `duration_sec` | INTEGER | Recording length in seconds |
| `file_size_bytes` | INTEGER | File size |
| `mime_type` | TEXT | Audio MIME type |
| `recorded_at` | TEXT | ISO-8601 timestamp |

---

## 📊 Querying the data

```bash
sqlite3 data/recordings.db
```

```sql
-- All recordings with user info
SELECT u.username, u.phone, r.sentence, r.duration_sec, r.recorded_at
FROM recordings r JOIN users u ON r.user_id = u.id
ORDER BY r.recorded_at DESC;

-- Total audio per user
SELECT u.username, COUNT(*) AS clips, SUM(r.duration_sec) AS total_sec
FROM recordings r JOIN users u ON r.user_id = u.id
GROUP BY u.id ORDER BY total_sec DESC;

-- Export to CSV
.mode csv
.output export.csv
SELECT * FROM recordings;
.quit
```

---

## ➕ Adding / editing sentences

Edit the `SENTENCES` list in `bot.py`:

```python
SENTENCES = [
    "Your first sentence here.",
    "Another sentence to read aloud.",
    # add as many as you like
]
```

---

## 🐳 Running with Docker (optional)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py .
CMD ["python", "bot.py"]
```

```bash
docker build -t voice-bot .
docker run -e BOT_TOKEN=your_token -v $(pwd)/data:/app/data voice-bot
```

---

## 📜 License

MIT — use freely.
