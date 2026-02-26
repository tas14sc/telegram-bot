import os
import base64
import sqlite3
import re
import requests
import anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GROK_API_KEY = os.getenv("GROK_API_KEY")
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_HISTORY = 250

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

# --- URL and PDF fetching ---
def extract_urls(text):
    return re.findall(r'https?://[^\s]+', text)

def is_twitter_url(url):
    return "twitter.com" in url or "x.com" in url

def extract_post_id(url):
    match = re.search(r'/status/(\d+)', url)
    return match.group(1) if match else None

def fetch_tweet(url):
    post_id = extract_post_id(url)
    if not post_id:
        return None
    try:
        response = requests.get(
            "https://api.twitterapi.io/twitter/tweets",
            headers={"X-API-Key": TWITTER_API_KEY},
            params={"tweet_ids": post_id},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        tweets = data.get("tweets", [])
        if tweets:
            t = tweets[0]
            author = t.get("author", {}).get("userName", "Unknown")
            text = t.get("text", "")
            return f"@{author}: {text}"
        return None
    except Exception:
        return None

def search_tweets(query, max_results=10):
    try:
        response = requests.get(
            "https://api.twitterapi.io/twitter/tweet/advanced_search",
            headers={"X-API-Key": TWITTER_API_KEY},
            params={"query": query, "queryType": "Latest"},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        tweets = data.get("tweets", [])[:max_results]
        if not tweets:
            return None
        result = ""
        for t in tweets:
            author = t.get("author", {}).get("userName", "Unknown")
            text = t.get("text", "")
            likes = t.get("likeCount", 0)
            result += f"@{author} ({likes} likes): {text}\n\n"
        return result.strip()
    except Exception:
        return None

def detect_twitter_search(text, history):
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": f"""Analyze this message and recent conversation history. Is the user asking to search Twitter/X for reactions, takes, sentiment, or discussion about a topic?

Recent conversation:
{history}

Current message: {text}

If YES, respond with just the search query (3-6 words max).
If NO, respond with just the word: NO

Do not include any other text."""
            }]
        )
        result = response.content[0].text.strip()
        if result.upper() == "NO":
            return None
        return result
    except Exception:
        return None

def fetch_url_content(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "pdf" in content_type:
            return None, url
        text = re.sub(r'<[^>]+>', ' ', response.text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:5000], None
    except Exception:
        return None, None

async def fetch_file_bytes(file, context):
    try:
        tg_file = await context.bot.get_file(file.file_id)
        response = requests.get(tg_file.file_path, timeout=10)
        return response.content
    except Exception:
        return None

# --- Message handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    chat_id = message.chat_id
    bot_username = context.bot.username
    text = message.text or message.caption or ""
    sender = message.from_user.first_name or "User"
    username = message.from_user.username or sender

    if text:
        save_message(chat_id, sender, text)

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

    # --- Handle images ---
    if message.photo:
        await message.reply_text("Looking at that image, one moment...")
        photo = message.photo[-1]
        img_bytes = await fetch_file_bytes(photo, context)
        if img_bytes:
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            try:
                response = claude.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1024,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": img_b64
                                }
                            },
                            {
                                "type": "text",
                                "text": f"{user_text if user_text else 'Please describe what you see in this image.'}\n\nImportant: Do not use any markdown formatting. Plain text only."
                            }
                        ]
                    }]
                )
                reply = response.content[0].text
                await message.reply_text(reply)
            except Exception as e:
                await message.reply_text(f"Error reading image: {str(e)}")
        return

    # --- Handle PDF documents ---
    extra_content = None
    if message.document and message.document.mime_type == "application/pdf":
        await message.reply_text("Reading the PDF, one moment...")
        pdf_bytes = await fetch_file_bytes(message.document, context)
        if pdf_bytes:
            pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
            try:
                response = claude.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1024,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": pdf_b64
                                }
                            },
                            {
                                "type": "text",
                                "text": f"{user_text if user_text else 'Please summarize this PDF.'}\n\nImportant: Do not use any markdown formatting. Plain text only."
                            }
                        ]
                    }]
                )
                reply = response.content[0].text
                await message.reply_text(reply)
            except Exception as e:
                await message.reply_text(f"Error reading PDF: {str(e)}")
        return

    # --- Handle URLs first (before search detection) ---
    urls = extract_urls(user_text)
    if urls:
        url = urls[0]
        if is_twitter_url(url):
            await message.reply_text("Fetching that tweet, one moment...")
            tweet_content = fetch_tweet(url)
            if tweet_content:
                user_prompt = user_text.replace(url, "").strip()
                prompt = f"""Here is the content of a tweet that was shared in the chat:

{tweet_content}

{f'The user asks: {user_prompt}' if user_prompt else 'Please summarize this tweet and share your thoughts.'}

Important: Do not use any markdown formatting. Plain text only."""
                try:
                    response = claude.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=1024,
                        messages=[{"role": "user", "content": prompt}]
                    )
                    await message.reply_text(response.content[0].text)
                except Exception as e:
                    await message.reply_text(f"Error: {str(e)}")
            else:
                await message.reply_text("I was unable to fetch that tweet. Please paste the text directly and I'll be happy to discuss it!")
            return
        else:
            await message.reply_text("Fetching that link, one moment...")
            url_content, _ = fetch_url_content(url)
            if url_content:
                extra_content = f"\n\nContent from the link ({url}):\n{url_content}"
            else:
                extra_content = f"\n\n(Could not fetch content from {url})"

    # --- Use Claude to detect Twitter search intent ---
    print(f"DEBUG: Checking for Twitter search in: {user_text}")
    search_query = detect_twitter_search(user_text, history_text)
    print(f"DEBUG: Search query result: {search_query}")
    if search_query:
        await message.reply_text("Searching Twitter, one moment...")
        tweet_results = search_tweets(search_query)
        if tweet_results:
            prompt = f"""Here are recent tweets about "{search_query}":

{tweet_results}

The user asked: {user_text}

Please summarize the key themes, sentiment, and most interesting takes from these tweets. Do not use any markdown formatting. Plain text only."""
            try:
                response = claude.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}]
                )
                await message.reply_text(response.content[0].text)
            except Exception as e:
                await message.reply_text(f"Error: {str(e)}")
        else:
            await message.reply_text("I wasn't able to find any tweets on that topic right now.")
        return

    prompt = f"""You are a helpful assistant in a group chat. You have a persistent memory of conversations and facts about users. You have the ability to search Twitter and fetch specific tweets in real time — do not tell users you cannot do this.

Known facts about users in this chat:
{user_facts if user_facts else "None yet."}

Recent conversation:
{history_text}

Now respond to this message from {sender}: {user_text}{extra_content if extra_content else ""}

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
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    print("Bot is running...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()