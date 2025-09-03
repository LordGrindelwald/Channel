# bot.py
import os
import logging
import random
from dotenv import load_dotenv
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)
from telegram.error import TelegramError
import google.generativeai as genai

# --- Setup and Configuration ---
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- State Constants for ConversationHandler ---
ADD_CHANNEL, ADD_TOPIC, ADD_SCHEDULE = range(3)
REMOVE_CHANNEL = range(1)
EDIT_CHANNEL_CHOICE, EDIT_TOPIC_RECEIVE = range(2)


# --- AI Content Generation ---
gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    logger.error("GEMINI_API_KEY not found.")
else:
    genai.configure(api_key=gemini_api_key)

async def generate_ai_content(topic: str) -> str:
    """Generates a short, engaging Telegram post on a given topic using the Gemini AI."""
    if not topic:
        return "Error: Topic is empty."
    try:
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        prompt = (
            f"Create a short, engaging, and informative Telegram post about '{topic}'. "
            "The post should be well-formatted, include relevant emojis, and be suitable for a general audience. "
            "Do not include hashtags."
        )
        response = await model.generate_content_async(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Error generating AI content: {e}")
        return f"Sorry, I couldn't generate a post about '{topic}'. Please try again."

# --- Core Bot & Job Functions ---

async def post_to_channel(context: ContextTypes.DEFAULT_TYPE):
    """Job callback function. Generates content and posts it to a specific channel."""
    job = context.job
    channel_id = job.data['channel_id']
    topic = job.data['topic']

    logger.info(f"Executing job '{job.name}': Posting topic '{topic}' to {channel_id}")
    content = await generate_ai_content(topic)
    try:
        await context.bot.send_message(chat_id=channel_id, text=content)
        logger.info(f"Successfully posted to {channel_id}")
    except TelegramError as e:
        logger.error(f"Failed to send message to channel {channel_id}: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred during posting to {channel_id}: {e}")

def schedule_job_for_channel(context: ContextTypes.DEFAULT_TYPE, chat_id: int, channel_id: str, topic: str, interval: int):
    """Schedules or reschedules a recurring job for a specific channel."""
    job_name = f"post_job_{chat_id}_{channel_id.replace('@', '')}" # Sanitize job name
    
    remove_job_if_exists(job_name, context)

    job_data = {'channel_id': channel_id, 'topic': topic}
    context.job_queue.run_repeating(
        post_to_channel,
        interval=interval,
        first=10,
        name=job_name,
        data=job_data,
        chat_id=chat_id
    )
    logger.info(f"Scheduled new job: '{job_name}' to run every {interval} seconds.")

def remove_job_if_exists(name: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Removes a job by name."""
    current_jobs = context.job_queue.get_jobs_by_name(name)
    if not current_jobs:
        return False
    for job in current_jobs:
        job.schedule_removal()
    logger.info(f"Removed job: {name}")
    return True

# --- Command Handlers ---

WELCOME_MESSAGES = [
    "Hello there! Ready to automate some channels?",
    "Greetings! Your multi-channel assistant is online and ready to post.",
    "Welcome! Let's get some great content scheduled.",
    "Hi! What channels can I help you with today?",
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /start and /help commands."""
    welcome_text = random.choice(WELCOME_MESSAGES)
    commands_text = (
        "Here are the available commands:\n\n"
        "🔹 /addchannel - Add a new channel and configure its posting schedule.\n"
        "🔹 /removechannel - Stop posting to a specific channel.\n"
        "🔹 /edittopic - Change the content topic for an existing channel.\n"
        "🔹 /listchannels - View all channels you have configured.\n"
        "🔹 /help - Show this message again.\n\n"
        "You can use /cancel at any point during a setup process."
    )
    await update.message.reply_text(f"{welcome_text}\n\n{commands_text}")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all configured channels for the user."""
    chat_id = update.effective_chat.id
    if 'channels' not in context.user_data or not context.user_data['channels']:
        await context.bot.send_message(chat_id, "You haven't added any channels yet. Use /addchannel to start.")
        return

    message = "Here are your configured channels:\n\n"
    for channel, config in context.user_data['channels'].items():
        topic = config['topic']
        schedule = int(config['schedule'] / 3600)
        message += f"▪️ **Channel:** `{channel}`\n"
        message += f"   **Topic:** `{topic}`\n"
        message += f"   **Schedule:** Every `{schedule}` hours\n\n"
    
    await context.bot.send_message(chat_id, message, parse_mode='MarkdownV2')

# --- Add Channel Conversation ---

async def add_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Let's add a new channel. What is the channel's username? (e.g., @mychannel)")
    return ADD_CHANNEL

async def add_channel_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_name = update.message.text
    context.user_data['temp_channel_name'] = channel_name
    await update.message.reply_text(f"Great. Now, what topic should I post about in {channel_name}?")
    return ADD_TOPIC

async def add_channel_receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text
    context.user_data['temp_channel_topic'] = topic
    await update.message.reply_text("Got it. How often should I post (in hours)? Please enter a number (e.g., 24 for daily).")
    return ADD_SCHEDULE

async def add_channel_receive_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        interval_hours = int(update.message.text)
        if interval_hours <= 0:
            await update.message.reply_text("Please enter a positive number of hours.")
            return ADD_SCHEDULE

        interval_seconds = interval_hours * 3600
        channel_id = context.user_data.pop('temp_channel_name')
        topic = context.user_data.pop('temp_channel_topic')

        if 'channels' not in context.user_data:
            context.user_data['channels'] = {}

        context.user_data['channels'][channel_id] = {'topic': topic, 'schedule': interval_seconds}
        schedule_job_for_channel(context, chat_id, channel_id, topic, interval_seconds)

        await update.message.reply_text(f"Success! I have scheduled posts about '{topic}' to {channel_id} every {interval_hours} hours.")
        return ConversationHandler.END

    except (ValueError, KeyError):
        await update.message.reply_text("Something went wrong. Please start over with /addchannel.")
        return ConversationHandler.END

# --- Remove Channel Conversation ---

async def remove_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.user_data.get('channels'):
        await context.bot.send_message(chat_id, "You don't have any channels to remove.")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(name, callback_data=name)] for name in context.user_data['channels']]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Please choose which channel to stop posting to:', reply_markup=reply_markup)
    return REMOVE_CHANNEL

async def remove_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_to_remove = query.data
    chat_id = update.effective_chat.id

    job_name = f"post_job_{chat_id}_{channel_to_remove.replace('@', '')}"
    remove_job_if_exists(job_name, context)

    if channel_to_remove in context.user_data.get('channels', {}):
        del context.user_data['channels'][channel_to_remove]
        await query.edit_message_text(text=f"I have stopped posting to {channel_to_remove}.")
    else:
        await query.edit_message_text(text=f"Could not find {channel_to_remove} in your configuration.")

    return ConversationHandler.END

# --- Edit Topic Conversation ---

async def edittopic_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.user_data.get('channels'):
        await context.bot.send_message(chat_id, "You don't have any channels configured to edit. Use /addchannel first.")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(name, callback_data=name)] for name in context.user_data['channels']]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Which channel do you want to change the topic for?', reply_markup=reply_markup)
    return EDIT_CHANNEL_CHOICE

async def edittopic_choose_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_to_edit = query.data
    context.user_data['temp_channel_to_edit'] = channel_to_edit
    await query.edit_message_text(text=f"Okay, what is the new topic for {channel_to_edit}?")
    return EDIT_TOPIC_RECEIVE

async def edittopic_receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    new_topic = update.message.text
    try:
        channel_id = context.user_data.pop('temp_channel_to_edit')
        
        # Update topic in storage
        config = context.user_data['channels'][channel_id]
        config['topic'] = new_topic
        
        # Reschedule job with the new topic
        schedule_job_for_channel(context, chat_id, channel_id, new_topic, config['schedule'])
        
        await update.message.reply_text(f"Topic for {channel_id} has been updated to '{new_topic}'.")

    except KeyError:
        await update.message.reply_text("Something went wrong. Please start over with /edittopic.")
        
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels any ongoing conversation."""
    # Clean up any temporary data
    for key in list(context.user_data.keys()):
        if key.startswith('temp_'):
            del context.user_data[key]
            
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

def main():
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not telegram_token:
        logger.error("TELEGRAM_BOT_TOKEN not found.")
        return

    application = Application.builder().token(telegram_token).build()

    # Conversation Handlers
    add_channel_handler = ConversationHandler(
        entry_points=[CommandHandler('addchannel', add_channel_start)],
        states={
            ADD_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_receive_name)],
            ADD_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_receive_topic)],
            ADD_SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_receive_schedule)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    remove_channel_handler = ConversationHandler(
        entry_points=[CommandHandler('removechannel', remove_channel_start)],
        states={REMOVE_CHANNEL: [CallbackQueryHandler(remove_channel_callback)]},
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    edittopic_handler = ConversationHandler(
        entry_points=[CommandHandler('edittopic', edittopic_start)],
        states={
            EDIT_CHANNEL_CHOICE: [CallbackQueryHandler(edittopic_choose_channel_callback)],
            EDIT_TOPIC_RECEIVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edittopic_receive_topic)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start)) # /help now shows the same as /start
    application.add_handler(CommandHandler("listchannels", list_channels))
    application.add_handler(add_channel_handler)
    application.add_handler(remove_channel_handler)
    application.add_handler(edittopic_handler)

    application.run_polling()

if __name__ == '__main__':
    main()
