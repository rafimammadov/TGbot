import os
import csv
import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set!")

BASE_DIR = Path("/var/local/tg_voice_dataset")
VOICE_DIR = BASE_DIR / "voices"
TEMP_DIR = BASE_DIR / "temp"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "recordings.db"
SENTENCES_CSV = BASE_DIR / "sentences.csv"

for directory in [BASE_DIR, VOICE_DIR, TEMP_DIR, LOG_DIR]:
    directory.mkdir(parents=True, exist_ok=True)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    filename=LOG_DIR / "bot.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# CONVERSATION STATES
# ============================================================

CONSENT, CHOOSE_AUTH, GET_PHONE, GET_USERNAME = range(4)


# ============================================================
# CONSENT TEXT
# ============================================================

CONSENT_TEXT = (
    "📋 *Razılıq və Məlumatların İstifadəsi Şərtləri*\n\n"
    "Bu bot Azərbaycan dili üçün səs məlumatlarının toplanması məqsədilə hazırlanıb. "
    "Davam etməzdən əvvəl aşağıdakı şərtləri oxuyub təsdiq etməyiniz tələb olunur:\n\n"

    "1️⃣ *Toplanan məlumatlar:*\n"
    "Sizin səs yazılarınız, Telegram ID-niz, istifadəçi adınız və ya telefon nömrəniz, "
    "ad/soyad məlumatınız və oxuduğunuz mətnlər saxlanıla bilər.\n\n"

    "2️⃣ *Məlumatların istifadə məqsədi:*\n"
    "Göndərdiyiniz səs yazıları yalnız süni intellekt əsaslı nitqin tanınması "
    "və mətnin səsə çevrilməsi modellərinin hazırlanması, öyrədilməsi və keyfiyyətinin "
    "yaxşılaşdırılması üçün istifadə olunacaq.\n\n"

    "3️⃣ *Məxfilik:*\n"
    "Şəxsi məlumatlarınız məxfi saxlanılacaq. Məlumatlar üçüncü tərəflərlə paylaşılmayacaq "
    "və yalnız layihənin texniki məqsədləri üçün istifadə ediləcək.\n\n"

    "4️⃣ *Könüllülük:*\n"
    "İştirak tamamilə könüllüdür. İstənilən vaxt botdan istifadəni dayandıra bilərsiniz.\n\n"

    "5️⃣ *Məlumatların silinməsi:*\n"
    "İstədiyiniz zaman administratorla əlaqə saxlayaraq məlumatlarınızın silinməsini tələb edə bilərsiniz.\n\n"

    "✅ *Təsdiq:*\n"
    "“✅ Razıyam” düyməsinə basmaqla siz təsdiq edirsiniz ki, səs məlumatlarınızın "
    "AI modellərinin öyrədilməsi və təkmilləşdirilməsi üçün istifadə olunmasına razısınız "
    "və məxfilik şərtləri ilə tanış olmusunuz."
)


WELCOME_TEXT = (
    "👋 Salam!\n\n"
    "Bu bot Azərbaycan dili üçün səs məlumatlarının toplanması məqsədilə hazırlanıb.\n\n"
    "Qaydalar:\n"
    "1. Sizə verilən cümləni sakit yerdə oxuyun.\n"
    "2. Səs yazısını göndərin.\n"
    "3. Bot sizdən təsdiq istəyəcək.\n"
    "4. Təsdiq etməzdən əvvəl səs yazınızı mütləq dinləyin.\n"
    "5. Səs düzgün və aydındırsa, təsdiq edin.\n"
    "6. Səs zəifdirsə, yarımçıqdırsa və ya səhv oxunubsa, yenidən yazın.\n\n"
    "Qeyd: Aksent problem deyil. Əsas odur ki, səs aydın və tam olsun."
)


# ============================================================
# DATABASE
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


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
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                phone TEXT,
                auth_method TEXT NOT NULL,
                first_name TEXT,
                last_name TEXT,
                consented INTEGER NOT NULL DEFAULT 0,
                consented_at TEXT,
                joined_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sentences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                text TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                added_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(text, normalized_text)
            );

            CREATE TABLE IF NOT EXISTS user_progress (
                telegram_id INTEGER NOT NULL,
                sentence_id INTEGER NOT NULL,
                cycle INTEGER NOT NULL DEFAULT 1,
                recorded INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (telegram_id, sentence_id, cycle),
                FOREIGN KEY (sentence_id) REFERENCES sentences(id)
            );

            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                sentence_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                telegram_file_id TEXT NOT NULL,
                file_path TEXT,
                duration_sec INTEGER,
                file_size_bytes INTEGER,
                mime_type TEXT,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (sentence_id) REFERENCES sentences(id)
            );

            CREATE INDEX IF NOT EXISTS idx_users_telegram_id
            ON users(telegram_id);

            CREATE INDEX IF NOT EXISTS idx_recordings_telegram_id
            ON recordings(telegram_id);

            CREATE INDEX IF NOT EXISTS idx_progress_user_recorded
            ON user_progress(telegram_id, recorded);
        """)

    logger.info("Database initialized at %s", DB_PATH)


# ============================================================
# SENTENCE IMPORT / SEED
# ============================================================

DEFAULT_SENTENCES = [
    (
        "Salam, mən ASAN könüllüsüyəm.",
        "Salam, mən ASAN könüllüsüyəm."
    ),
    (
        "Zəhmət olmasa, səs yazısını aydın şəkildə oxuyun.",
        "Zəhmət olmasa, səs yazısını aydın şəkildə oxuyun."
    ),
    (
        "Aksent problem deyil, əsas məsələ səsin aydın olmasıdır.",
        "Aksent problem deyil, əsas məsələ səsin aydın olmasıdır."
    ),
    (
        "Bu səs yazısı Azərbaycan dili üçün süni intellekt layihəsində istifadə olunacaq.",
        "Bu səs yazısı Azərbaycan dili üçün süni intellekt layihəsində istifadə olunacaq."
    ),
    (
        "Təsdiq etməzdən əvvəl səs yazısını diqqətlə dinləyin.",
        "Təsdiq etməzdən əvvəl səs yazısını diqqətlə dinləyin."
    ),
]


def count_sentences():
    with get_con() as con:
        row = con.execute(
            "SELECT COUNT(*) AS cnt FROM sentences WHERE is_active = 1"
        ).fetchone()
        return row["cnt"]


def seed_default_sentences():
    with get_con() as con:
        existing = con.execute(
            "SELECT COUNT(*) AS cnt FROM sentences"
        ).fetchone()["cnt"]

        if existing > 0:
            return

        for text, normalized_text in DEFAULT_SENTENCES:
            con.execute(
                """
                INSERT OR IGNORE INTO sentences
                    (text, normalized_text, added_at, is_active)
                VALUES (?, ?, ?, 1)
                """,
                (text, normalized_text, now_iso())
            )

    logger.info("Default sentences inserted.")


def import_sentences_from_csv():
    """
    Optional:
    Put /var/local/tg_voice_dataset/sentences.csv with columns:

    text,normalized_text

    Example:
    Salam, mən könüllüyəm.,Salam, mən könüllüyəm.
    """

    if not SENTENCES_CSV.exists():
        return

    imported = 0

    with open(SENTENCES_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if "text" not in reader.fieldnames:
            logger.warning("sentences.csv must contain 'text' column.")
            return

        with get_con() as con:
            for row in reader:
                text = (row.get("text") or "").strip()
                normalized_text = (row.get("normalized_text") or text).strip()

                if not text:
                    continue

                con.execute(
                    """
                    INSERT OR IGNORE INTO sentences
                        (text, normalized_text, added_at, is_active)
                    VALUES (?, ?, ?, 1)
                    """,
                    (text, normalized_text, now_iso())
                )

                imported += 1

    logger.info("Imported %s sentences from CSV.", imported)


# ============================================================
# USER HELPERS
# ============================================================

def upsert_user(telegram_id, username, phone, auth_method, first_name, last_name):
    current_time = now_iso()

    with get_con() as con:
        con.execute(
            """
            INSERT INTO users
                (
                    telegram_id,
                    username,
                    phone,
                    auth_method,
                    first_name,
                    last_name,
                    consented,
                    consented_at,
                    joined_at
                )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                phone = excluded.phone,
                auth_method = excluded.auth_method,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                consented = 1,
                consented_at = excluded.consented_at
            """,
            (
                telegram_id,
                username,
                phone,
                auth_method,
                first_name,
                last_name,
                current_time,
                current_time,
            )
        )

        row = con.execute(
            "SELECT id FROM users WHERE telegram_id = ?",
            (telegram_id,)
        ).fetchone()

        return row["id"]


def get_user_db_id(telegram_id):
    with get_con() as con:
        row = con.execute(
            """
            SELECT id
            FROM users
            WHERE telegram_id = ?
              AND consented = 1
            """,
            (telegram_id,)
        ).fetchone()

        return row["id"] if row else None


def get_user_stats(telegram_id):
    with get_con() as con:
        row = con.execute(
            """
            SELECT
                COUNT(*) AS total_recordings,
                COALESCE(SUM(duration_sec), 0) AS total_seconds
            FROM recordings
            WHERE telegram_id = ?
            """,
            (telegram_id,)
        ).fetchone()

        return row["total_recordings"], row["total_seconds"]


# ============================================================
# SENTENCE QUEUE HELPERS
# ============================================================

def sync_user_sentences(con, telegram_id):
    """
    Add all active sentences to this user's progress for the current cycle.
    """

    row = con.execute(
        """
        SELECT COALESCE(MAX(cycle), 1) AS current_cycle
        FROM user_progress
        WHERE telegram_id = ?
        """,
        (telegram_id,)
    ).fetchone()

    current_cycle = row["current_cycle"] or 1

    con.execute(
        """
        INSERT OR IGNORE INTO user_progress
            (telegram_id, sentence_id, cycle, recorded)
        SELECT ?, s.id, ?, 0
        FROM sentences s
        WHERE s.is_active = 1
        """,
        (telegram_id, current_cycle)
    )


def get_current_cycle(con, telegram_id):
    row = con.execute(
        """
        SELECT COALESCE(MAX(cycle), 1) AS current_cycle
        FROM user_progress
        WHERE telegram_id = ?
        """,
        (telegram_id,)
    ).fetchone()

    return row["current_cycle"] or 1


def get_next_sentence(telegram_id, skip_set=None):
    if count_sentences() == 0:
        return None

    skip_set = set(skip_set or [])

    with get_con() as con:
        sync_user_sentences(con, telegram_id)

        cycle = get_current_cycle(con, telegram_id)

        remaining = con.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM user_progress
            WHERE telegram_id = ?
              AND cycle = ?
              AND recorded = 0
            """,
            (telegram_id, cycle)
        ).fetchone()["cnt"]

        if remaining == 0:
            cycle += 1

            con.execute(
                """
                INSERT OR IGNORE INTO user_progress
                    (telegram_id, sentence_id, cycle, recorded)
                SELECT ?, s.id, ?, 0
                FROM sentences s
                WHERE s.is_active = 1
                """,
                (telegram_id, cycle)
            )

            logger.info("User %s started cycle %s", telegram_id, cycle)

        base_params = [telegram_id, cycle]

        if skip_set:
            placeholders = ",".join(["?"] * len(skip_set))

            query = f"""
                SELECT
                    up.sentence_id,
                    up.cycle,
                    s.text,
                    s.normalized_text
                FROM user_progress up
                JOIN sentences s ON s.id = up.sentence_id
                WHERE up.telegram_id = ?
                  AND up.cycle = ?
                  AND up.recorded = 0
                  AND s.is_active = 1
                  AND up.sentence_id NOT IN ({placeholders})
                ORDER BY RANDOM()
                LIMIT 1
            """

            row = con.execute(
                query,
                base_params + list(skip_set)
            ).fetchone()

            if row is None:
                skip_set = set()

        if not skip_set:
            row = con.execute(
                """
                SELECT
                    up.sentence_id,
                    up.cycle,
                    s.text,
                    s.normalized_text
                FROM user_progress up
                JOIN sentences s ON s.id = up.sentence_id
                WHERE up.telegram_id = ?
                  AND up.cycle = ?
                  AND up.recorded = 0
                  AND s.is_active = 1
                ORDER BY RANDOM()
                LIMIT 1
                """,
                (telegram_id, cycle)
            ).fetchone()

        if row is None:
            return None

        total = con.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM sentences
            WHERE is_active = 1
            """
        ).fetchone()["cnt"]

        remaining_now = con.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM user_progress
            WHERE telegram_id = ?
              AND cycle = ?
              AND recorded = 0
            """,
            (telegram_id, row["cycle"])
        ).fetchone()["cnt"]

        done = total - remaining_now

        return {
            "sentence_id": row["sentence_id"],
            "cycle": row["cycle"],
            "text": row["text"],
            "normalized_text": row["normalized_text"],
            "total": total,
            "remaining": remaining_now,
            "done": done,
        }


def mark_recorded(telegram_id, sentence_id):
    with get_con() as con:
        cycle = get_current_cycle(con, telegram_id)

        con.execute(
            """
            UPDATE user_progress
            SET recorded = 1
            WHERE telegram_id = ?
              AND sentence_id = ?
              AND cycle = ?
            """,
            (telegram_id, sentence_id, cycle)
        )


# ============================================================
# RECORDING HELPERS
# ============================================================

def save_recording(
    user_db_id,
    telegram_id,
    sentence_id,
    text,
    normalized_text,
    telegram_file_id,
    file_path,
    duration_sec,
    file_size_bytes,
    mime_type,
):
    with get_con() as con:
        con.execute(
            """
            INSERT INTO recordings
                (
                    user_id,
                    telegram_id,
                    sentence_id,
                    text,
                    normalized_text,
                    telegram_file_id,
                    file_path,
                    duration_sec,
                    file_size_bytes,
                    mime_type,
                    recorded_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_db_id,
                telegram_id,
                sentence_id,
                text,
                normalized_text,
                telegram_file_id,
                file_path,
                duration_sec,
                file_size_bytes,
                mime_type,
                now_iso(),
            )
        )


def write_sidecar_json(
    file_path,
    telegram_id,
    username,
    sentence_id,
    text,
    normalized_text,
    duration_sec,
    file_size_bytes,
):
    try:
        sidecar_path = Path(file_path).with_suffix(".json")

        payload = {
            "telegram_id": telegram_id,
            "username": username,
            "sentence_id": sentence_id,
            "text": text,
            "normalized_text": normalized_text,
            "file_path": file_path,
            "duration_sec": duration_sec,
            "file_size_bytes": file_size_bytes,
            "recorded_at": now_iso(),
            "consent": {
                "model_training_allowed": True,
                "privacy_promised": True,
            }
        }

        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.warning("Could not write sidecar JSON: %s", e)


# ============================================================
# KEYBOARDS
# ============================================================

def consent_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Razıyam — Davam et", callback_data="consent_accept")],
        [InlineKeyboardButton("❌ Razı deyiləm", callback_data="consent_decline")],
    ])


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎙️ Cümlə yaz", callback_data="record")],
        [InlineKeyboardButton("📊 Mənim statistikalarım", callback_data="stats")],
        [InlineKeyboardButton("ℹ️ Bot haqqında", callback_data="about")],
    ])


def sentence_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔀 Bu cümləni keç", callback_data="skip")],
        [InlineKeyboardButton("🏠 Əsas menyu", callback_data="menu")],
    ])


def confirm_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Təsdiq et və saxla", callback_data="confirm"),
            InlineKeyboardButton("🔄 Yenidən yaz", callback_data="rerecord"),
        ]
    ])


# ============================================================
# DISPLAY SENTENCE
# ============================================================

async def show_sentence(message, telegram_id, ctx, is_skip=False):
    if count_sentences() == 0:
        await message.reply_text(
            "⚠️ Bazada hələ cümlə yoxdur. Administrator cümlələri əlavə etməlidir.",
            reply_markup=main_menu()
        )
        return

    if is_skip:
        current_sentence_id = ctx.user_data.get("sentence_id")
        skip_set = ctx.user_data.get("skip_set", set())

        if current_sentence_id:
            skip_set.add(current_sentence_id)

        ctx.user_data["skip_set"] = skip_set
    else:
        skip_set = ctx.user_data.get("skip_set", set())

    info = get_next_sentence(telegram_id, skip_set=skip_set)

    if not info:
        await message.reply_text(
            "⚠️ Hazırda uyğun cümlə tapılmadı.",
            reply_markup=main_menu()
        )
        return

    ctx.user_data.update({
        "sentence_id": info["sentence_id"],
        "text": info["text"],
        "normalized_text": info["normalized_text"],
    })

    for key in [
        "pending_file_id",
        "pending_file_path",
        "pending_duration",
        "pending_file_size",
        "pending_mime_type",
    ]:
        ctx.user_data.pop(key, None)

    await message.reply_text(
        f"📖 *Original mətn:*\n"
        f"`{info['text']}`\n\n"
        f"🎙️ *Oxunacaq mətn:*\n\n"
        f"_{info['normalized_text']}_\n\n"
        f"📋 Proqres: *{info['done']}/{info['total']}* tamamlanıb | Dövr: *{info['cycle']}*\n\n"
        f"Səs mesajı göndərin. Göndərdikdən sonra mütləq dinləyib təsdiq edin.",
        parse_mode="Markdown",
        reply_markup=sentence_kb(),
    )


# ============================================================
# START / CONSENT / REGISTRATION
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if get_user_db_id(user.id):
        await update.message.reply_text(
            f"👋 Xoş gəlmisiniz, *{user.first_name}*!\n\n"
            "Davam etmək üçün menyudan seçim edin.",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        CONSENT_TEXT,
        parse_mode="Markdown",
        reply_markup=consent_kb(),
    )

    return CONSENT


async def handle_consent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "consent_decline":
        await query.message.reply_text(
            "❌ Siz razılıq şərtlərini qəbul etmədiniz.\n\n"
            "Bu şərtlər qəbul edilmədən botdan istifadə mümkün deyil.\n\n"
            "Fikrinizi dəyişsəniz, /start yazaraq yenidən başlaya bilərsiniz.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    await query.message.reply_text(
        "✅ *Təşəkkür edirik! Razılığınız qeydə alınacaq.*\n\n"
        "Davam etmək üçün zəhmət olmasa özünüzü identifikasiya edin:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 Telefon nömrəsini paylaş", callback_data="auth_phone")],
            [InlineKeyboardButton("👤 Telegram istifadəçi adından istifadə et", callback_data="auth_username")],
        ]),
    )

    return CHOOSE_AUTH


async def choose_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "auth_phone":
        await query.message.reply_text(
            "Zəhmət olmasa aşağıdakı düyməyə basaraq telefon nömrənizi paylaşın:",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("📱 Telefon nömrəmi paylaş", request_contact=True)]],
                one_time_keyboard=True,
                resize_keyboard=True,
            ),
        )
        return GET_PHONE

    await query.message.reply_text(
        "Zəhmət olmasa Telegram istifadəçi adınızı yazın.\n\n"
        "Məsələn: `rafi_mammadov`\n\n"
        "`@` işarəsi yazmağa ehtiyac yoxdur.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

    return GET_USERNAME


async def get_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    user = update.effective_user

    if not contact:
        await update.message.reply_text(
            "Zəhmət olmasa telefon nömrəsini paylaşmaq üçün düymədən istifadə edin."
        )
        return GET_PHONE

    upsert_user(
        telegram_id=user.id,
        username=user.username,
        phone=contact.phone_number,
        auth_method="phone",
        first_name=user.first_name,
        last_name=user.last_name,
    )

    await update.message.reply_text(
        f"✅ *Qeydiyyat tamamlandı!*\n\n"
        f"Telefon: `{contact.phone_number}`",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=main_menu(),
    )

    return ConversationHandler.END


async def get_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = update.message.text.strip().lstrip("@")

    if not username:
        await update.message.reply_text(
            "İstifadəçi adı boş ola bilməz. Zəhmət olmasa yenidən yazın:"
        )
        return GET_USERNAME

    upsert_user(
        telegram_id=user.id,
        username=username,
        phone=None,
        auth_method="username",
        first_name=user.first_name,
        last_name=user.last_name,
    )

    await update.message.reply_text(
        f"✅ *Qeydiyyat tamamlandı!*\n\n"
        f"İstifadəçi adı: `@{username}`",
        parse_mode="Markdown",
    )

    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=main_menu(),
    )

    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()

    await update.message.reply_text(
        "Əməliyyat ləğv edildi.",
        reply_markup=ReplyKeyboardRemove(),
    )

    return ConversationHandler.END


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    user_db_id = get_user_db_id(user.id)

    if not user_db_id:
        await query.message.reply_text(
            "⚠️ Zəhmət olmasa əvvəlcə /start yazaraq qeydiyyatdan keçin."
        )
        return

    if query.data == "menu":
        ctx.user_data.clear()

        await query.message.reply_text(
            "Əsas menyu:",
            reply_markup=main_menu(),
        )

    elif query.data == "record":
        await show_sentence(query.message, user.id, ctx)

    elif query.data == "skip":
        await show_sentence(query.message, user.id, ctx, is_skip=True)

    elif query.data == "rerecord":
        pending_path = ctx.user_data.get("pending_file_path")

        if pending_path:
            try:
                Path(pending_path).unlink(missing_ok=True)
            except Exception as e:
                logger.warning("Could not delete temp file: %s", e)

        for key in [
            "pending_file_id",
            "pending_file_path",
            "pending_duration",
            "pending_file_size",
            "pending_mime_type",
        ]:
            ctx.user_data.pop(key, None)

        text = ctx.user_data.get("text")
        normalized_text = ctx.user_data.get("normalized_text")

        await query.message.reply_text(
            f"🔄 *Yenidən cəhd edək.*\n\n"
            f"📖 *Original mətn:*\n"
            f"`{text}`\n\n"
            f"🎙️ *Oxunacaq mətn:*\n\n"
            f"_{normalized_text}_\n\n"
            f"Yeni səs mesajı göndərin.",
            parse_mode="Markdown",
            reply_markup=sentence_kb(),
        )

    elif query.data == "confirm":
        pending_file_id = ctx.user_data.get("pending_file_id")
        pending_file_path = ctx.user_data.get("pending_file_path")
        duration = ctx.user_data.get("pending_duration")
        file_size = ctx.user_data.get("pending_file_size")
        mime_type = ctx.user_data.get("pending_mime_type") or "audio/ogg"

        sentence_id = ctx.user_data.get("sentence_id")
        text = ctx.user_data.get("text")
        normalized_text = ctx.user_data.get("normalized_text")

        if not pending_file_id or not pending_file_path or not sentence_id:
            await query.message.reply_text(
                "⚠️ Saxlanılacaq səs yazısı tapılmadı. Zəhmət olmasa yenidən səs göndərin.",
                reply_markup=main_menu(),
            )
            return

        src = Path(pending_file_path)

        if not src.exists():
            await query.message.reply_text(
                "⚠️ Müvəqqəti səs faylı tapılmadı. Zəhmət olmasa yenidən səs göndərin.",
                reply_markup=main_menu(),
            )
            return

        final_filename = src.name
        final_path = VOICE_DIR / final_filename

        src.rename(final_path)

        save_recording(
            user_db_id=user_db_id,
            telegram_id=user.id,
            sentence_id=sentence_id,
            text=text,
            normalized_text=normalized_text,
            telegram_file_id=pending_file_id,
            file_path=str(final_path),
            duration_sec=duration,
            file_size_bytes=file_size,
            mime_type=mime_type,
        )

        mark_recorded(user.id, sentence_id)

        write_sidecar_json(
            file_path=str(final_path),
            telegram_id=user.id,
            username=user.username,
            sentence_id=sentence_id,
            text=text,
            normalized_text=normalized_text,
            duration_sec=duration,
            file_size_bytes=file_size,
        )

        ctx.user_data.clear()

        count, total_seconds = get_user_stats(user.id)
        total_sentences = count_sentences()

        await query.message.reply_text(
            f"✅ *Səs yazısı saxlanıldı!*\n\n"
            f"📖 Cümlə:\n"
            f"_{normalized_text}_\n\n"
            f"⏱ Müddət: *{duration} saniyə*\n"
            f"📊 Sizin ümumi yazılarınız: *{count}*\n"
            f"📚 Bazadakı cümlələr: *{total_sentences}*\n\n"
            f"Növbəti yazıya hazırsınız?",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )

    elif query.data == "stats":
        count, seconds = get_user_stats(user.id)
        minutes, sec = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)

        total_sentences = count_sentences()
        info = get_next_sentence(user.id)
        cycle = info["cycle"] if info else 1

        await query.message.reply_text(
            f"📊 *Sizin statistikalarınız:*\n\n"
            f"• Göndərilən səs yazıları: *{count}*\n"
            f"• Ümumi audio müddəti: *{hours} saat {minutes} dəqiqə {sec} saniyə*\n"
            f"• Bazadakı aktiv cümlələr: *{total_sentences}*\n"
            f"• Cari dövr: *{cycle}*",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )

    elif query.data == "about":
        await query.message.reply_text(
            "ℹ️ *Bot haqqında:*\n\n"
            "Bu bot Azərbaycan dili üçün səs məlumatlarının toplanması məqsədilə hazırlanıb.\n\n"
            "Siz verilən cümləni oxuyub səs mesajı göndərirsiniz. "
            "Səs yazısı yalnız siz təsdiq etdikdən sonra yadda saxlanılır.\n\n"
            "Toplanan səs məlumatları süni intellekt əsaslı nitqin tanınması və "
            "mətnin səsə çevrilməsi modellərinin öyrədilməsi və təkmilləşdirilməsi üçün istifadə olunur.\n\n"
            "Məlumatlarınız məxfi saxlanılır və üçüncü tərəflərlə paylaşılmır.\n\n"
            "Qeyd: Aksent problem deyil. Əsas odur ki, səs aydın və düzgün olsun.",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )


# ============================================================
# VOICE HANDLER
# ============================================================

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    voice = update.message.voice

    user_db_id = get_user_db_id(user.id)

    if not user_db_id:
        await update.message.reply_text(
            "⚠️ Zəhmət olmasa əvvəlcə /start yazaraq razılıq verin və qeydiyyatdan keçin."
        )
        return

    sentence_id = ctx.user_data.get("sentence_id")
    text = ctx.user_data.get("text")
    normalized_text = ctx.user_data.get("normalized_text")

    if not sentence_id:
        info = get_next_sentence(user.id)

        if not info:
            await update.message.reply_text(
                "⚠️ Hazırda oxumaq üçün cümlə yoxdur."
            )
            return

        sentence_id = info["sentence_id"]
        text = info["text"]
        normalized_text = info["normalized_text"]

        ctx.user_data.update({
            "sentence_id": sentence_id,
            "text": text,
            "normalized_text": normalized_text,
        })

    telegram_file = await ctx.bot.get_file(voice.file_id)

    timestamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")
    filename = f"{user.id}_{timestamp}_sentence_{sentence_id}.ogg"
    temp_path = TEMP_DIR / filename

    await telegram_file.download_to_drive(custom_path=str(temp_path))

    ctx.user_data.update({
        "pending_file_id": voice.file_id,
        "pending_file_path": str(temp_path),
        "pending_duration": voice.duration,
        "pending_file_size": voice.file_size,
        "pending_mime_type": voice.mime_type or "audio/ogg",
    })

    logger.info(
        "Pending voice received: user=%s sentence=%s file=%s",
        user.id,
        sentence_id,
        temp_path,
    )

    await update.message.reply_text(
        f"🎧 *Səs yazısı qəbul edildi.*\n\n"
        f"Zəhmət olmasa təsdiq etməzdən əvvəl səsinizi dinləyin.\n\n"
        f"📖 Cümlə:\n"
        f"_{normalized_text}_\n\n"
        f"⏱ Müddət: *{voice.duration} saniyə*\n\n"
        f"Səs aydın və düzgündür?",
        parse_mode="Markdown",
        reply_markup=confirm_kb(),
    )


# ============================================================
# ADMIN COMMANDS
# ============================================================

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_con() as con:
        users = con.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE consented = 1"
        ).fetchone()["cnt"]

        recordings = con.execute(
            "SELECT COUNT(*) AS cnt FROM recordings"
        ).fetchone()["cnt"]

        seconds = con.execute(
            "SELECT COALESCE(SUM(duration_sec), 0) AS total FROM recordings"
        ).fetchone()["total"]

        sentences = con.execute(
            "SELECT COUNT(*) AS cnt FROM sentences WHERE is_active = 1"
        ).fetchone()["cnt"]

    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)

    await update.message.reply_text(
        f"🔧 *Admin statistikası:*\n\n"
        f"• Razılıq vermiş istifadəçilər: *{users}*\n"
        f"• Aktiv cümlələr: *{sentences}*\n"
        f"• Ümumi səs yazıları: *{recordings}*\n"
        f"• Ümumi audio müddəti: *{hours} saat {minutes} dəqiqə {sec} saniyə*\n\n"
        f"📁 Data qovluğu:\n"
        f"`{BASE_DIR}`",
        parse_mode="Markdown",
    )


async def cmd_export_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Dataset serverdə bu qovluqda saxlanılır:\n\n"
        f"`{BASE_DIR}`\n\n"
        f"Audio fayllar:\n"
        f"`{VOICE_DIR}`\n\n"
        f"SQLite DB:\n"
        f"`{DB_PATH}`\n\n"
        f"Export etmək üçün serverdə:\n\n"
        f"`cd /var/local && tar -czf tg_voice_dataset.tar.gz tg_voice_dataset`",
        parse_mode="Markdown",
    )


# ============================================================
# MAIN
# ============================================================

def main():
    init_db()
    import_sentences_from_csv()
    seed_default_sentences()

    app = Application.builder().token(BOT_TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            CONSENT: [
                CallbackQueryHandler(handle_consent, pattern="^consent_")
            ],
            CHOOSE_AUTH: [
                CallbackQueryHandler(choose_auth, pattern="^auth_")
            ],
            GET_PHONE: [
                MessageHandler(filters.CONTACT, get_phone)
            ],
            GET_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_username)
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conversation)

    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("export", cmd_export_info))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info("Telegram voice collection bot started.")
    print("Bot running...")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()