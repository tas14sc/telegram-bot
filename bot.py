import os
import anthropic
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Store recent messages per chat for context
chat_history = {}
MAX_HISTORY = 50  # how many messages to remember

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    chat_id = message.chat_id
    bot_username = context.bot.username
    text = message.text
    sender = message.from_user.first_name or "User"

    # Always store the message in history
    if chat_id not in chat_history:
        chat_history[chat_id] = []

    chat_history[chat_id].append(f"{sender}: {text}")

    # Trim history to max size
    if len(chat_history[chat_id]) > MAX_HISTORY:
        chat_history[chat_id] = chat_history[chat_id][-MAX_HISTORY:]

    # Only respond if mentioned or replied to
    is_mentioned = f"@{bot_username}" in text
    is_reply_to_bot = (
        message.reply_to_message and
        message.reply_to_message.from_user and
        message.reply_to_message.from_user.username == bot_username
    )
    is_private = message.chat.type == "private"

    if not (is_mentioned or is_reply_to_bot or is_private):
        return  # Read but don't respond

    # Build prompt with conversation context
    history_text = "\n".join(chat_history[chat_id][:-1])  # everything except current message
    user_text = text.replace(f"@{bot_username}", "").strip()

    prompt = f"""You are a helpful assistant in a group chat. Here is the recent conversation for context:

{history_text}

Now respond to this message from {sender}: {user_text}"""

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        reply = response.content[0].text
        await message.reply_text(reply)
    except Exception as e:
        await message.reply_text(f"Error: {str(e)}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()