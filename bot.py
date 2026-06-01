"""
Telegram Voice Recording Bot
- Consent agreement before registration
- text + normalized_text sentence display
- Confirm / re-record flow before saving
"""

import os, json, sqlite3, logging
from datetime import datetime, timezone
from pathlib import Path

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes,
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set!")

DATA_DIR  = Path("data")
VOICE_DIR = DATA_DIR / "voices"
TEMP_DIR  = DATA_DIR / "temp"
DB_PATH   = DATA_DIR / "recordings.db"
for d in (DATA_DIR, VOICE_DIR, TEMP_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ─── Conversation states ──────────────────────────────────────────────────────
CONSENT, CHOOSE_AUTH, GET_PHONE, GET_USERNAME = range(4)

CONSENT_TEXT = (
    "📋 *Data Collection & Usage Agreement*\n\n"
    "Before using this bot, please read and accept the following:\n\n"
    "1️⃣  *What we collect:* Your voice recordings, Telegram ID, username or phone number, "
    "and the text you read aloud.\n\n"
    "2️⃣  *How it is used:* Your recordings will be used exclusively to *train and improve "
    "speech recognition and text-to-speech AI models*.\n\n"
    "3️⃣  *Storage:* Data is stored securely and is not shared with third parties.\n\n"
    "4️⃣  *Your rights:* You may request deletion of your data at any time by contacting "
    "the administrator.\n\n"
    "5️⃣  *Voluntary:* Participation is entirely voluntary. You may stop at any time.\n\n"
    "By tapping *✅ I Agree* you confirm that you have read and accepted these terms."
)

# ─── DB ───────────────────────────────────────────────────────────────────────

def get_con():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    with get_con() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username    TEXT,
                phone       TEXT,
                auth_method TEXT NOT NULL,
                first_name  TEXT,
                last_name   TEXT,
                consented   INTEGER NOT NULL DEFAULT 0,
                consented_at TEXT,
                joined_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sentences (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id       INTEGER,
                text            TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                added_at        TEXT NOT NULL,
                is_active       INTEGER NOT NULL DEFAULT 1,
                UNIQUE(text, normalized_text)
            );

            CREATE TABLE IF NOT EXISTS user_progress (
                telegram_id INTEGER NOT NULL,
                sentence_id INTEGER NOT NULL REFERENCES sentences(id),
                cycle       INTEGER NOT NULL DEFAULT 1,
                recorded    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (telegram_id, sentence_id)
            );

            CREATE TABLE IF NOT EXISTS recordings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(id),
                telegram_id     INTEGER NOT NULL,
                sentence_id     INTEGER NOT NULL REFERENCES sentences(id),
                text            TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                file_id         TEXT NOT NULL,
                file_path       TEXT,
                duration_sec    INTEGER,
                file_size_bytes INTEGER,
                mime_type       TEXT,
                recorded_at     TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_prog_user ON user_progress(telegram_id, recorded);
            CREATE INDEX IF NOT EXISTS idx_rec_user  ON recordings(telegram_id);
        """)
        # Migrations for existing DBs
        for col, definition in [
            ("normalized_text", "TEXT NOT NULL DEFAULT ''"),
            ("source_id",       "INTEGER"),
            ("consented",       "INTEGER NOT NULL DEFAULT 0"),
            ("consented_at",    "TEXT"),
        ]:
            cols = {r[1] for r in con.execute("PRAGMA table_info(sentences)").fetchall()}
            ucols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
            if col not in cols and col not in ucols:
                table = "sentences" if col in ("normalized_text", "source_id") else "users"
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
                except Exception:
                    pass
    logger.info("DB ready at %s", DB_PATH)

# ─── Queue helpers ────────────────────────────────────────────────────────────

def count_sentences():
    with get_con() as con:
        return con.execute("SELECT COUNT(*) FROM sentences WHERE is_active=1").fetchone()[0]

def _sync_user_sentences(con, telegram_id):
    con.execute("""
        INSERT OR IGNORE INTO user_progress (telegram_id, sentence_id, cycle, recorded)
        SELECT ?, s.id, 1, 0 FROM sentences s WHERE s.is_active = 1
    """, (telegram_id,))

def _get_current_cycle(con, telegram_id):
    row = con.execute(
        "SELECT MAX(cycle) FROM user_progress WHERE telegram_id=?", (telegram_id,)
    ).fetchone()
    return row[0] or 1

def get_next_sentence(telegram_id, skip_set=None):
    if count_sentences() == 0:
        return None
    with get_con() as con:
        _sync_user_sentences(con, telegram_id)
        cycle = _get_current_cycle(con, telegram_id)

        remaining = con.execute("""
            SELECT COUNT(*) FROM user_progress
            WHERE telegram_id=? AND cycle=? AND recorded=0
        """, (telegram_id, cycle)).fetchone()[0]

        if remaining == 0:
            cycle += 1
            con.execute(
                "UPDATE user_progress SET cycle=?, recorded=0 WHERE telegram_id=?",
                (cycle, telegram_id)
            )
            logger.info("User %s starting cycle %d", telegram_id, cycle)

        effective_skip = set(skip_set) if skip_set else set()
        if effective_skip:
            non_skipped = con.execute("""
                SELECT COUNT(*) FROM user_progress
                WHERE telegram_id=? AND cycle=? AND recorded=0
                  AND sentence_id NOT IN ({})
            """.format(",".join("?" * len(effective_skip))),
                [telegram_id, cycle] + list(effective_skip)
            ).fetchone()[0]
            if non_skipped == 0:
                effective_skip = set()

        if effective_skip:
            ph = ",".join("?" * len(effective_skip))
            row = con.execute(f"""
                SELECT up.sentence_id, up.cycle, s.text, s.normalized_text
                FROM user_progress up JOIN sentences s ON s.id = up.sentence_id
                WHERE up.telegram_id=? AND up.cycle=? AND up.recorded=0
                  AND up.sentence_id NOT IN ({ph})
                ORDER BY RANDOM() LIMIT 1
            """, [telegram_id, cycle] + list(effective_skip)).fetchone()
        else:
            row = con.execute("""
                SELECT up.sentence_id, up.cycle, s.text, s.normalized_text
                FROM user_progress up JOIN sentences s ON s.id = up.sentence_id
                WHERE up.telegram_id=? AND up.cycle=? AND up.recorded=0
                ORDER BY RANDOM() LIMIT 1
            """, (telegram_id, cycle)).fetchone()

        if not row:
            return None

        remaining_now = con.execute("""
            SELECT COUNT(*) FROM user_progress
            WHERE telegram_id=? AND cycle=? AND recorded=0
        """, (telegram_id, cycle)).fetchone()[0]

        total = con.execute(
            "SELECT COUNT(*) FROM sentences WHERE is_active=1"
        ).fetchone()[0]

    return {
        "sentence_id":     row["sentence_id"],
        "text":            row["text"],
        "normalized_text": row["normalized_text"],
        "cycle":           row["cycle"],
        "remaining":       remaining_now,
        "total":           total,
        "done":            total - remaining_now,
    }

def mark_recorded(telegram_id, sentence_id):
    with get_con() as con:
        cycle = _get_current_cycle(con, telegram_id)
        con.execute("""
            UPDATE user_progress SET recorded=1
            WHERE telegram_id=? AND sentence_id=? AND cycle=?
        """, (telegram_id, sentence_id, cycle))

# ─── User helpers ─────────────────────────────────────────────────────────────

def record_consent(telegram_id):
    with get_con() as con:
        con.execute(
            "UPDATE users SET consented=1, consented_at=? WHERE telegram_id=?",
            (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), telegram_id)
        )

def upsert_user(telegram_id, username, phone, auth_method, first_name, last_name):
    with get_con() as con:
        con.execute("""
            INSERT INTO users
                (telegram_id, username, phone, auth_method, first_name, last_name,
                 consented, joined_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                phone=excluded.phone,
                auth_method=excluded.auth_method,
                first_name=excluded.first_name,
                last_name=excluded.last_name
        """, (telegram_id, username, phone, auth_method,
              first_name, last_name, datetime.now(timezone.utc).replace(tzinfo=None).isoformat()))
        return con.execute(
            "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
        ).fetchone()["id"]

def get_user_db_id(telegram_id):
    with get_con() as con:
        row = con.execute(
            "SELECT id FROM users WHERE telegram_id=? AND consented=1", (telegram_id,)
        ).fetchone()
        return row["id"] if row else None

def get_user_stats(telegram_id):
    with get_con() as con:
        row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(duration_sec),0) FROM recordings WHERE telegram_id=?",
            (telegram_id,)
        ).fetchone()
        return row[0], row[1]

def save_recording(user_db_id, telegram_id, sentence_id, text, normalized_text,
                   file_id, file_path, duration, file_size, mime_type):
    with get_con() as con:
        con.execute("""
            INSERT INTO recordings
                (user_id, telegram_id, sentence_id, text, normalized_text, file_id,
                 file_path, duration_sec, file_size_bytes, mime_type, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_db_id, telegram_id, sentence_id, text, normalized_text, file_id,
              file_path, duration, file_size, mime_type, datetime.now(timezone.utc).replace(tzinfo=None).isoformat()))

# ─── Keyboards ────────────────────────────────────────────────────────────────

def consent_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I Agree — Continue", callback_data="consent_accept")],
        [InlineKeyboardButton("❌ I Decline",           callback_data="consent_decline")],
    ])

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎙️ Record a sentence", callback_data="record")],
        [InlineKeyboardButton("📊 My stats",           callback_data="stats")],
        [InlineKeyboardButton("ℹ️  About",              callback_data="about")],
    ])

def sentence_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔀 Skip this sentence", callback_data="skip")],
        [InlineKeyboardButton("🏠 Main menu",          callback_data="menu")],
    ])

def confirm_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm & save", callback_data="confirm"),
        InlineKeyboardButton("🔄 Record again",   callback_data="rerecord"),
    ]])

# ─── Sentence display ─────────────────────────────────────────────────────────

async def _show_sentence(message, telegram_id, ctx, is_skip=False):
    if count_sentences() == 0:
        await message.reply_text(
            "⚠️ No sentences in DB yet. Ask admin to import.",
            reply_markup=main_menu()
        )
        return

    if is_skip:
        current_id = ctx.user_data.get("sentence_id")
        skip_set   = ctx.user_data.get("skip_set", set())
        if current_id:
            skip_set.add(current_id)
        ctx.user_data["skip_set"] = skip_set
    else:
        skip_set = ctx.user_data.get("skip_set", set())

    info = get_next_sentence(telegram_id, skip_set=skip_set)
    if not info:
        await message.reply_text("No sentences available.", reply_markup=main_menu())
        return

    ctx.user_data.update({
        "sentence_id":     info["sentence_id"],
        "text":            info["text"],
        "normalized_text": info["normalized_text"],
    })
    for key in ("pending_file_id", "pending_file_path", "pending_duration",
                "pending_file_size", "pending_mime_type"):
        ctx.user_data.pop(key, None)

    await message.reply_text(
        f"📖 *Original text:*\n`{info['text']}`\n\n"
        f"🎙️ *Read this aloud and send a voice message:*\n\n"
        f"_{info['normalized_text']}_\n\n"
        f"📋 Progress: *{info['done']}/{info['total']}* complete  |  Cycle {info['cycle']}",
        parse_mode="Markdown",
        reply_markup=sentence_kb(),
    )

# ─── /start & consent ────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Already registered and consented → straight to menu
    if get_user_db_id(user.id):
        await update.message.reply_text(
            f"👋 Welcome back, *{user.first_name}*!",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
        return ConversationHandler.END

    # Show consent agreement first
    await update.message.reply_text(
        CONSENT_TEXT,
        parse_mode="Markdown",
        reply_markup=consent_kb(),
    )
    return CONSENT

async def handle_consent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "consent_decline":
        await q.message.reply_text(
            "❌ You declined the agreement. You cannot use this bot without accepting.\n\n"
            "Type /start if you change your mind.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    # Accepted — proceed to auth choice
    await q.message.reply_text(
        "✅ *Thank you for agreeing!*\n\nPlease identify yourself to continue:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 Share phone number",    callback_data="auth_phone")],
            [InlineKeyboardButton("👤 Use Telegram username", callback_data="auth_username")],
        ])
    )
    return CHOOSE_AUTH

async def choose_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "auth_phone":
        await q.message.reply_text(
            "Tap the button below to share your number:",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("📱 Share my phone number", request_contact=True)]],
                one_time_keyboard=True, resize_keyboard=True
            )
        )
        return GET_PHONE
    await q.message.reply_text(
        "Type your username (no @ needed):",
        reply_markup=ReplyKeyboardRemove()
    )
    return GET_USERNAME

async def get_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = update.message.contact
    u = update.effective_user
    if not c:
        await update.message.reply_text("Please use the button to share your number.")
        return GET_PHONE
    upsert_user(u.id, u.username, c.phone_number, "phone", u.first_name, u.last_name)
    await update.message.reply_text(
        f"✅ *Registered!* Phone: `{c.phone_number}`",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await update.message.reply_text("What would you like to do?", reply_markup=main_menu())
    return ConversationHandler.END

async def get_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    username = update.message.text.strip().lstrip("@")
    if not username:
        await update.message.reply_text("Username cannot be empty. Try again:")
        return GET_USERNAME
    upsert_user(u.id, username, None, "username", u.first_name, u.last_name)
    await update.message.reply_text(
        f"✅ *Registered!* Username: `@{username}`",
        parse_mode="Markdown"
    )
    await update.message.reply_text("What would you like to do?", reply_markup=main_menu())
    return ConversationHandler.END

# ─── Main callback handler ────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = update.effective_user

    if q.data == "menu":
        ctx.user_data.clear()
        await q.message.reply_text("Main menu:", reply_markup=main_menu())

    elif q.data == "record":
        await _show_sentence(q.message, u.id, ctx)

    elif q.data == "skip":
        await _show_sentence(q.message, u.id, ctx, is_skip=True)

    elif q.data == "confirm":
        file_id         = ctx.user_data.get("pending_file_id")
        file_path       = ctx.user_data.get("pending_file_path")
        duration        = ctx.user_data.get("pending_duration")
        file_size       = ctx.user_data.get("pending_file_size")
        mime_type       = ctx.user_data.get("pending_mime_type")
        sentence_id     = ctx.user_data.get("sentence_id")
        text            = ctx.user_data.get("text")
        normalized_text = ctx.user_data.get("normalized_text")

        if not file_id or not sentence_id:
            await q.message.reply_text(
                "⚠️ Nothing to save. Please record a voice message first.",
                reply_markup=main_menu()
            )
            return

        # Move file from temp to permanent storage
        if file_path:
            src = Path(file_path)
            if src.exists():
                final_path = VOICE_DIR / src.name
                src.rename(final_path)
                file_path = str(final_path)

        uid = get_user_db_id(u.id)
        save_recording(uid, u.id, sentence_id, text, normalized_text,
                       file_id, file_path, duration, file_size,
                       mime_type or "audio/ogg")
        mark_recorded(u.id, sentence_id)

        # Write sidecar JSON
        if file_path:
            try:
                with open(Path(file_path).with_suffix(".json"), "w", encoding="utf-8") as f:
                    json.dump({
                        "telegram_id":     u.id,
                        "username":        u.username,
                        "sentence_id":     sentence_id,
                        "text":            text,
                        "normalized_text": normalized_text,
                        "file_path":       file_path,
                        "duration_sec":    duration,
                        "file_size_bytes": file_size,
                        "recorded_at":     datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                    }, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning("Could not write sidecar JSON: %s", e)

        ctx.user_data.clear()
        count, _ = get_user_stats(u.id)
        total = count_sentences()
        await q.message.reply_text(
            f"✅ *Recording saved!* ({count}/{total} total)\n\n"
            f"_{normalized_text}_\n⏱ {duration}s\n\nReady for another?",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )

    elif q.data == "rerecord":
        pending_path = ctx.user_data.get("pending_file_path")
        if pending_path:
            try:
                Path(pending_path).unlink(missing_ok=True)
            except Exception:
                pass
        for key in ("pending_file_id", "pending_file_path", "pending_duration",
                    "pending_file_size", "pending_mime_type"):
            ctx.user_data.pop(key, None)

        text            = ctx.user_data.get("text")
        normalized_text = ctx.user_data.get("normalized_text")
        info_done       = get_next_sentence(u.id, skip_set=ctx.user_data.get("skip_set"))
        done  = info_done["done"]  if info_done else 0
        total = info_done["total"] if info_done else count_sentences()
        cycle = info_done["cycle"] if info_done else 1

        await q.message.reply_text(
            f"🔄 *Let's try again — read this aloud:*\n\n"
            f"📖 *Original:*\n`{text}`\n\n"
            f"🎙️ *Read aloud:*\n_{normalized_text}_\n\n"
            f"📋 Progress: *{done}/{total}* complete  |  Cycle {cycle}",
            parse_mode="Markdown",
            reply_markup=sentence_kb(),
        )

    elif q.data == "stats":
        count, secs = get_user_stats(u.id)
        mins, s = divmod(int(secs), 60)
        total = count_sentences()
        info  = get_next_sentence(u.id)
        cycle = info["cycle"] if info else 1
        await q.message.reply_text(
            f"📊 *Your stats:*\n\n"
            f"• Recordings submitted: *{count}*\n"
            f"• Total sentences:      *{total}*\n"
            f"• Total audio:          *{mins}m {s}s*\n"
            f"• Current cycle:        *{cycle}*",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )

    elif q.data == "about":
        await q.message.reply_text(
            "ℹ️ *How it works:*\n\n"
            "You see the original text for reference and read the *normalized version* aloud.\n"
            "Every sentence is shown once before any repeats (cycles).\n\n"
            "Your voice data is used to train speech AI models as per the agreement you accepted.",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )

# ─── Voice handler ─────────────────────────────────────────────────────────────

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    v = update.message.voice
    uid = get_user_db_id(u.id)

    if not uid:
        await update.message.reply_text(
            "⚠️ Please type /start to register before sending recordings."
        )
        return

    sentence_id     = ctx.user_data.get("sentence_id")
    text            = ctx.user_data.get("text")
    normalized_text = ctx.user_data.get("normalized_text")

    if not sentence_id:
        info = get_next_sentence(u.id)
        if not info:
            await update.message.reply_text("⚠️ No sentences available.")
            return
        sentence_id     = info["sentence_id"]
        text            = info["text"]
        normalized_text = info["normalized_text"]
        ctx.user_data.update({
            "sentence_id":     sentence_id,
            "text":            text,
            "normalized_text": normalized_text,
        })

    # Download to temp (not permanent until confirmed)
    tg_file  = await ctx.bot.get_file(v.file_id)
    filename = f"{u.id}_{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y%m%d_%H%M%S')}_{sentence_id}.ogg"
    dest     = TEMP_DIR / filename
    await tg_file.download_to_drive(dest)

    ctx.user_data.update({
        "pending_file_id":   v.file_id,
        "pending_file_path": str(dest),
        "pending_duration":  v.duration,
        "pending_file_size": v.file_size,
        "pending_mime_type": v.mime_type or "audio/ogg",
    })

    logger.info("Pending recording %s for user %s — awaiting confirmation", filename, u.id)

    await update.message.reply_text(
        f"🎧 *Got it! Please listen and confirm:*\n\n"
        f"📖 Sentence: _{normalized_text}_\n"
        f"⏱ Duration: {v.duration}s\n\n"
        f"Happy with this recording?",
        parse_mode="Markdown",
        reply_markup=confirm_kb(),
    )

# ─── Admin command ─────────────────────────────────────────────────────────────

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_con() as con:
        users = con.execute("SELECT COUNT(*) FROM users WHERE consented=1").fetchone()[0]
        recs  = con.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
        secs  = con.execute(
            "SELECT COALESCE(SUM(duration_sec),0) FROM recordings"
        ).fetchone()[0]
        sents = con.execute(
            "SELECT COUNT(*) FROM sentences WHERE is_active=1"
        ).fetchone()[0]
    mins, s = divmod(int(secs), 60)
    hrs,  m = divmod(mins, 60)
    await update.message.reply_text(
        f"🔧 *Admin Stats*\n\n"
        f"• Sentences in pool:   *{sents}*\n"
        f"• Consented users:     *{users}*\n"
        f"• Total recordings:    *{recs}*\n"
        f"• Total audio:         *{hrs}h {m}m {s}s*",
        parse_mode="Markdown"
    )

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            CONSENT:      [CallbackQueryHandler(handle_consent, pattern="^consent_")],
            CHOOSE_AUTH:  [CallbackQueryHandler(choose_auth,    pattern="^auth_")],
            GET_PHONE:    [MessageHandler(filters.CONTACT, get_phone)],
            GET_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_username)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CommandHandler("admin", cmd_admin))

    logger.info("Bot running …")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()