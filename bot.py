import os
import sqlite3
import anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_HISTORY = 200

# --- Database setup ---
def init_db():
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            sender TEXT,
            text TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_facts (
            chat_id INTEGER,
            username TEXT,
            facts TEXT,
            PRIMARY KEY (chat_id, username)
        )
    """)
    conn.commit()
    conn.close()

def save_message(chat_id, sender, text):
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("INSERT INTO messages (chat_id, sender, text) VALUES (?, ?, ?)", (chat_id, sender, text))
    conn.commit()
    conn.close()

def get_history(chat_id):
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("SELECT sender, text FROM messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?", (chat_id, MAX_HISTORY))
    rows = c.fetchall()
    conn.close()
    rows.reverse()
    return "\n".join([f"{sender}: {text}" for sender, text in rows])

def get_user_facts(chat_id):
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("SELECT username, facts FROM user_facts WHERE chat_id = ?", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return "\n".join([f"{username}: {facts}" for username, facts in rows])

def save_user_facts(chat_id, username, facts):
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_facts (chat_id, username, facts) VALUES (?, ?, ?)", (chat_id, username, facts))
    conn.commit()
    conn.close()

# --- Message handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    chat_id = message.chat_id
    bot_username = context.bot.username
    text = message.text
    sender = message.from_user.first_name or "User"
    username = message.from_user.username or sender

    # Always save the message
    save_message(chat_id, sender, text)

    # Only respond if mentioned or replied to
    is_mentioned = f"@{bot_username}" in text
    is_reply_to_bot = (
        message.reply_to_message and
        message.reply_to_message.from_user and
        message.reply_to_message.from_user.username == bot_username
    )
    is_private = message.chat.type == "private"

    if not (is_mentioned or is_reply_to_bot or is_private):
        return

    history_text = get_history(chat_id)
    user_facts = get_user_facts(chat_id)
    user_text = text.replace(f"@{bot_username}", "").strip()

    prompt = f"""You are a helpful assistant in a group chat. You have a persistent memory of conversations and facts about users.

Known facts about users in this chat:
{user_facts if user_facts else "None yet."}

Recent conversation:
{history_text}

Now respond to this message from {sender}: {user_text}

Important: Do not use any markdown formatting in your response. Plain text only, no bold, no bullet points, no headers.

If you learn any new facts about a user from this message (name, preferences, job, etc.), include them at the very end of your response in this exact format:
FACTS: {username} | fact1, fact2, fact3"""

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        reply = response.content[0].text

        # Extract and save any new facts
        if "FACTS:" in reply:
            parts = reply.split("FACTS:")
            reply = parts[0].strip()
            fact_part = parts[1].strip()
            if "|" in fact_part:
                fact_username, facts = fact_part.split("|", 1)
                save_user_facts(chat_id, fact_username.strip(), facts.strip())

        await message.reply_text(reply)
    except Exception as e:
        await message.reply_text(f"Error: {str(e)}")

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()