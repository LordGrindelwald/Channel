# bot.py
import os
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Bot
from telegram.ext import Application, CommandHandler, ConversationHandler, MessageHandler, filters
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import google.generativeai as genai

# --- Setup and Configuration ---

# Load environment variables from a .env file for security
load_dotenv()

# Set up logging to monitor the bot's activity and troubleshoot issues
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- State Constants for ConversationHandler ---
# These states help manage the flow of conversation with the user
SET_TOPIC, SET_SCHEDULE, SET_CHANNEL = range(3)

# --- AI Content Generation ---

# Configure the Gemini API with your key
gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    logger.error("GEMINI_API_KEY not found in environment variables.")
else:
    genai.configure(api_key=gemini_api_key)

async def generate_ai_content(topic: str) -> str:
    """
    Generates a short, engaging Telegram post on a given topic using the Gemini AI.

    Args:
        topic: The subject for the AI-generated post.

    Returns:
        The generated content as a string, ready to be posted.
    """
    if not topic:
        logger.warning("AI content generation called with an empty topic.")
        return "Please set a topic first using /settopic."

    try:
        model = genai.GenerativeModel('gemini-pro')
        # Crafting a prompt that guides the AI to produce suitable content for a Telegram channel
        prompt = (
            f"Create a short, engaging, and informative Telegram post about '{topic}'. "
            "The post should be easy to read, well-formatted, and interesting for a general audience. "
            "Include relevant emojis to make it visually appealing. Do not include any hashtags."
        )
        response = await model.generate_content_async(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Error generating AI content: {e}")
        return f"Sorry, I couldn't generate a post about '{topic}'. Please try again later."

# --- Core Bot Functions ---

async def post_to_channel(bot: Bot, channel_id: str, topic: str):
    """
    Generates content and posts it to the specified Telegram channel.

    Args:
        bot: The Telegram bot instance.
        channel_id: The ID of the target channel.
        topic: The topic for the content to be generated.
    """
    if not channel_id or not topic:
        logger.error("Channel ID or topic not set for scheduled post.")
        return

    logger.info(f"Generating content for topic: {topic}")
    content = await generate_ai_content(topic)

    try:
        logger.info(f"Posting to channel: {channel_id}")
        await bot.send_message(chat_id=channel_id, text=content)
        logger.info("Post successfully sent to the channel.")
    except TelegramError as e:
        logger.error(f"Failed to send message to channel {channel_id}: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred during posting: {e}")

# --- Command Handlers ---

async def start(update, context):
    """Handler for the /start command."""
    await update.message.reply_text(
        "Welcome! I'm your AI-powered channel assistant.\n\n"
        "Here's how to get started:\n"
        "1. Add me to your channel as an administrator.\n"
        "2. Use /setchannel to link me to your channel.\n"
        "3. Use /settopic to give me a topic to post about.\n"
        "4. Use /setschedule to choose how often I should post.\n\n"
        "You can use /help to see all available commands."
    )

async def set_channel(update, context):
    """Starts the conversation to set the target channel."""
    await update.message.reply_text("Please provide your channel's username (e.g., @mychannel).")
    return SET_CHANNEL

async def receive_channel(update, context):
    """Receives and stores the channel username."""
    channel = update.message.text
    context.user_data['channel_id'] = channel
    await update.message.reply_text(f"Great! I will now post to {channel}.")
    return ConversationHandler.END

async def set_topic(update, context):
    """Starts the conversation to set the post topic."""
    await update.message.reply_text("What topic should I post about?")
    return SET_TOPIC

async def receive_topic(update, context):
    """Receives and stores the topic."""
    topic = update.message.text
    context.user_data['topic'] = topic
    await update.message.reply_text(f"Topic set to: '{topic}'.")
    # Automatically start posting if schedule and channel are already set
    if 'schedule_interval' in context.user_data and 'channel_id' in context.user_data:
        await schedule_posts(update, context)
    return ConversationHandler.END

async def set_schedule(update, context):
    """Starts the conversation to set the posting schedule."""
    await update.message.reply_text("How often should I post (in hours)? For example, enter '24' for daily posts.")
    return SET_SCHEDULE

async def receive_schedule(update, context):
    """Receives and stores the schedule interval."""
    try:
        interval = int(update.message.text)
        if interval <= 0:
            await update.message.reply_text("Please enter a positive number of hours.")
            return SET_SCHEDULE
        context.user_data['schedule_interval'] = interval
        await update.message.reply_text(f"I will post every {interval} hours.")
        # Automatically start posting if topic and channel are already set
        if 'topic' in context.user_data and 'channel_id' in context.user_data:
            await schedule_posts(update, context)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid number. Please enter a number of hours.")
        return SET_SCHEDULE

async def schedule_posts(update, context):
    """Schedules the recurring post job."""
    scheduler = context.application.job_queue._scheduler
    job_name = f"post_job_{update.effective_chat.id}"

    # Remove any existing job with the same name before starting a new one
    existing_jobs = [job for job in scheduler.get_jobs() if job.name == job_name]
    if existing_jobs:
        for job in existing_jobs:
            job.remove()
        logger.info(f"Removed existing job: {job_name}")

    channel_id = context.user_data.get('channel_id')
    topic = context.user_data.get('topic')
    interval = context.user_data.get('schedule_interval')

    if not all([channel_id, topic, interval]):
        await update.message.reply_text("I can't start posting yet. Please make sure you have set the channel, topic, and schedule.")
        return

    scheduler.add_job(
        post_to_channel,
        'interval',
        hours=interval,
        args=[context.bot, channel_id, topic],
        name=job_name
    )
    await update.message.reply_text("I have started the posting schedule for your channel!")
    logger.info(f"Scheduled posts for channel {channel_id} with topic '{topic}' every {interval} hours.")

async def stop(update, context):
    """Stops the scheduled posting."""
    scheduler = context.application.job_queue._scheduler
    job_name = f"post_job_{update.effective_chat.id}"
    
    jobs_removed = False
    for job in scheduler.get_jobs():
        if job.name == job_name:
            job.remove()
            jobs_removed = True
            
    if jobs_removed:
        await update.message.reply_text("I have stopped the posting schedule.")
        logger.info(f"Stopped posting schedule for job: {job_name}")
    else:
        await update.message.reply_text("There was no active schedule to stop.")

async def cancel(update, context):
    """Cancels the current conversation."""
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

def main():
    """The main function to run the bot."""
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not telegram_token:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables.")
        return

    # Initialize the bot application
    application = Application.builder().token(telegram_token).build()

    # Conversation handlers for setting up the bot
    conv_handler_topic = ConversationHandler(
        entry_points=[CommandHandler('settopic', set_topic)],
        states={SET_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)]},
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    conv_handler_schedule = ConversationHandler(
        entry_points=[CommandHandler('setschedule', set_schedule)],
        states={SET_SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule)]},
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    conv_handler_channel = ConversationHandler(
        entry_points=[CommandHandler('setchannel', set_channel)],
        states={SET_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel)]},
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # Register handlers with the application
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(conv_handler_topic)
    application.add_handler(conv_handler_schedule)
    application.add_handler(conv_handler_channel)

    # Start the scheduler
    scheduler = AsyncIOScheduler()
    scheduler.start()
    
    # Keep the scheduler in the application context to access it in handlers
    application.job_queue._scheduler = scheduler

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()
