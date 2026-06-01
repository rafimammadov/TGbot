# Voice Recording Bot

A Telegram bot for collecting voice recordings from consenting users to train speech AI models.

## Quick Start

### 1. Get a bot token
Create a bot via [@BotFather](https://t.me/BotFather) and copy the token.

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env and set BOT_TOKEN=your_token_here
```

### 3. Run locally
```bash
pip install -r requirements.txt
export BOT_TOKEN=your_token_here   # or use a .env loader
python bot.py
```

### 4. Run with Docker
```bash
docker build -t voice-bot .
docker run -d \
  --name voice-bot \
  --restart unless-stopped \
  -e BOT_TOKEN=your_token_here \
  -v $(pwd)/data:/app/data \
  voice-bot
```

## Import sentences
```bash
# From a text file (one sentence per line)
python import_sentences.py sentences.txt

# From a JSON file
python import_sentences.py sentences.json
```

JSON format:
```json
[
  {"text": "Hello world.", "normalized_text": "hello world"},
  {"text": "How are you?", "normalized_text": "how are you"}
]
```

## Admin command
Send `/admin` to the bot to see overall statistics (total users, recordings, audio duration).

## Data layout
```
data/
  recordings.db     ← SQLite database
  voices/           ← Confirmed .ogg recordings + .json sidecars
  temp/             ← Pending (unconfirmed) recordings — auto-cleaned on re-record
```

## User flow
1. `/start` → consent agreement shown
2. User accepts → phone or username registration
3. User taps **Record a sentence** → sees original + normalized text
4. User sends a voice message → listens back → confirms or re-records
5. Confirmed recordings are saved to `data/voices/`
