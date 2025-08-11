#!/usr/bin/env python3
import os
import re
import psycopg2
from psycopg2.extras import DictCursor
from datetime import datetime
from dotenv import load_dotenv
import instaloader
from urllib.parse import urlparse
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ====== Load env ======
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]

if not BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")

# ====== Database (PostgreSQL) ======
DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise SystemExit("DATABASE_URL not set in .env")

def init_db():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        username TEXT,
        first_name TEXT,
        link TEXT,
        media_pk TEXT,
        created_at TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

def add_log(user_id, username, first_name, link, media_pk):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO logs (user_id, username, first_name, link, media_pk, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, username, first_name, link, media_pk, datetime.utcnow())
    )
    conn.commit()
    conn.close()

def get_stats():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT COUNT(*) FROM logs")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM logs")
    unique_users = cur.fetchone()[0]
    cur.execute("""
        SELECT user_id, username, first_name, COUNT(*) as cnt 
        FROM logs 
        GROUP BY user_id, username, first_name 
        ORDER BY cnt DESC LIMIT 10
    """)
    top = cur.fetchall()
    conn.close()
    return {"total": total, "unique_users": unique_users, "top": top}

def get_logs(limit=100):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, username, first_name, link, media_pk, created_at FROM logs ORDER BY id DESC LIMIT %s", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_links(limit=100):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute("""
        SELECT link FROM logs
        GROUP BY link
        ORDER BY MAX(id) DESC
        LIMIT %s
    """, (limit,))
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows

# ====== Helpers ======
URL_RE = re.compile(r"(https?://[^\s]+)")

def extract_url(text: str):
    m = URL_RE.search(text)
    return m.group(1) if m else None

def extract_tags_and_mentions(caption: str):
    hashtags = re.findall(r"#\w+", caption or "")
    mentions = re.findall(r"@\w+", caption or "")
    return hashtags, mentions

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_shortcode_from_url(url):
    path = urlparse(url).path
    parts = path.strip('/').split('/')
    if len(parts) >= 2:
        return parts[1]
    return None

def get_instagram_post_info(url):
    L = instaloader.Instaloader()
    shortcode = get_shortcode_from_url(url)
    if not shortcode:
        raise Exception("URL dan shortcode olinmadi")

    post = instaloader.Post.from_shortcode(L.context, shortcode)

    caption = post.caption or ""
    hashtags, mentions = extract_tags_and_mentions(caption)

    media_type = "photo"
    media_urls = []

    if post.is_video:
        media_type = "video"
        media_urls.append(post.video_url)
    elif post.typename == "GraphSidecar":
        media_type = "album"
        for node in post.get_sidecar_nodes():
            if node.is_video:
                media_urls.append(node.video_url)
            else:
                media_urls.append(node.display_url)
    else:
        media_urls.append(post.url)

    return {
        "media_type": media_type,
        "media_urls": media_urls,
        "caption": caption,
        "hashtags": hashtags,
        "mentions": mentions,
    }

# ====== Handlers ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Salom! Instagram link yuboring (post yoki reel). Men media va captionni yuboraman."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    help_text = (
        "/start - Botni boshlash\n"
        "/help - Yordam\n"
    )
    if is_admin(user_id):
        help_text += (
            "\n--- Admin komandalar ---\n"
            "/stats - Umumiy statistika\n"
            "/logs - Oxirgi loglar\n"
            "/links - Oxirgi linklar\n"
        )
    await update.message.reply_text(help_text)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Bu buyruq faqat adminlarga mo‘ljallangan.")
        return
    s = get_stats()
    text = f"📊 Statistika\nUmumiy linklar: {s['total']}\nFoydalanuvchi soni: {s['unique_users']}\n\nTop yuboruvchilar:\n"
    for row in s["top"]:
        uid, uname, fname, cnt = row
        text += f"- {fname or uname or uid} ({uname or ''}) : {cnt}\n"
    await update.message.reply_text(text)

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Bu buyruq faqat adminlarga mo‘ljallangan.")
        return
    limit = 50
    if context.args and context.args[0].isdigit():
        limit = min(500, int(context.args[0]))
    rows = get_logs(limit)
    if not rows:
        await update.message.reply_text("Logs mavjud emas.")
        return
    text_lines = []
    for r in rows:
        _id, uid, uname, fname, link, media_pk, created_at = r
        text_lines.append(f"[{created_at}] {fname or ''} ({uname or uid}) → {link}")
    await update.message.reply_text("\n".join(text_lines))

async def links_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Bu buyruq faqat adminlarga mo‘ljallangan.")
        return
    rows = get_links(200)
    if not rows:
        await update.message.reply_text("Linklar mavjud emas.")
        return
    await update.message.reply_text("\n".join(rows))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    url = extract_url(text)
    if not url:
        await update.message.reply_text("Iltimos, Instagram post yoki reel linkini yuboring.")
        return

    msg = await update.message.reply_text("🔎 Instagram ma'lumot olinayapti... iltimos kuting")
    try:
        info = get_instagram_post_info(url)

        add_log(user.id, user.username or "", user.first_name or "", url, "")

        caption_text = (
            f"📄 Caption:\n{info['caption'] or '(yo‘q)'}\n\n"
            f"🏷 Hashtags: {' '.join(info['hashtags']) if info['hashtags'] else 'Yo‘q'}\n"
            f"👤 Mentions: {' '.join(info['mentions']) if info['mentions'] else 'Yo‘q'}"
        )

        if info["media_type"] == "photo":
            await update.message.reply_photo(photo=info["media_urls"][0])
        elif info["media_type"] == "video":
            await update.message.reply_video(video=info["media_urls"][0])
        elif info["media_type"] == "album":
            for media_url in info["media_urls"]:
                if media_url.endswith(('.mp4', '.mov')):
                    await update.message.reply_video(video=media_url)
                else:
                    await update.message.reply_photo(photo=media_url)
        else:
            await update.message.reply_text("❌ Media topilmadi yoki qo‘llab-quvvatlanmaydi.")

        await update.message.reply_text(caption_text)
        await msg.delete()

    except Exception as e:
        try:
            await msg.edit_text(f"❌ Xatolik: {e}")
        except:
            await update.message.reply_text(f"❌ Xatolik: {e}")

# ====== Main ======
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("logs", logs_cmd))
    app.add_handler(CommandHandler("links", links_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 Bot ishga tushdi...")
    app.run_polling()

if __name__ == "__main__":
    main()
