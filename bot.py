# bot.py
import os
import logging
from dotenv import load_dotenv
from telegram import Bot
from telegram.ext import Application, CommandHandler, ConversationHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
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
SET_TOPIC, SET_SCHEDULE, SET_CHANNEL = range(3)

# --- AI Content Generation ---

# Configure the Gemini API with your key
gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    logger.error("GEMINI_API_KEY not found in environment variables.")
else:
    genai.configure(api_key=gemini_api_key)

async def generate_ai_content(topic: str) -> str:
    """Generates a short, engaging Telegram post on a given topic using the Gemini AI."""
    if not topic:
        logger.warning("AI content generation called with an empty topic.")
        return "Please set a topic first using /settopic."

    try:
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        prompt = (
            f"Create a short, engaging, and informative Telegram post about '{topic}'. "
            "The post should be easy to read, well-formatted, and interesting for a general audience. "
            "Include relevant emojis to make it visually appealing. Do not include any hashtags."
        )
        # Using the async method for generation
        response = await model.generate_content_async(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Error generating AI content: {e}")
        return f"Sorry, I couldn't generate a post about '{topic}'. Please try again later."

# --- Core Bot Functions ---

async def post_to_channel(context: ContextTypes.DEFAULT_TYPE):
    """
    Job callback function. Generates content and posts it to the channel.
    This function is called by the JobQueue.
    """
    job = context.job
    channel_id = job.data['channel_id']
    topic = job.data['topic']

    if not channel_id or not topic:
        logger.error("Channel ID or topic not found in job data for scheduled post.")
        return

    logger.info(f"Generating content for topic: {topic}")
    content = await generate_ai_content(topic)

    try:
        logger.info(f"Posting to channel: {channel_id}")
        await context.bot.send_message(chat_id=channel_id, text=content)
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
        "You can use /stop to halt the posting schedule."
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
    # Attempt to start schedule if other info is present
    await schedule_posts(update, context)
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
    # Attempt to start schedule if other info is present
    await schedule_posts(update, context)
    return ConversationHandler.END

async def set_schedule(update, context):
    """Starts the conversation to set the posting schedule."""
    await update.message.reply_text("How often should I post (in hours)? For example, enter '24' for daily posts.")
    return SET_SCHEDULE

async def receive_schedule(update, context):
    """Receives and stores the schedule interval."""
    try:
        interval_hours = int(update.message.text)
        if interval_hours <= 0:
            await update.message.reply_text("Please enter a positive number of hours.")
            return SET_SCHEDULE
        
        # Convert hours to seconds for the job queue
        context.user_data['schedule_interval_seconds'] = interval_hours * 3600
        await update.message.reply_text(f"I will post every {interval_hours} hours.")
        # Attempt to start schedule if other info is present
        await schedule_posts(update, context)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid number. Please enter a number of hours.")
        return SET_SCHEDULE

async def schedule_posts(update, context: ContextTypes.DEFAULT_TYPE):
    """Schedules the recurring post job if all necessary info is present."""
    chat_id = update.effective_message.chat_id
    job_name = f"post_job_{chat_id}"

    # Remove any existing job with the same name before starting a new one
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    if current_jobs:
        for job in current_jobs:
            job.schedule_removal()
        logger.info(f"Removed existing job: {job_name}")

    # Check if all required user data is present
    channel_id = context.user_data.get('channel_id')
    topic = context.user_data.get('topic')
    interval = context.user_data.get('schedule_interval_seconds')

    if not all([channel_id, topic, interval]):
        # Don't send a message here, as this function is called after each setup step.
        # It will silently wait for all data.
        return

    # Pass user data to the job
    job_data = {'channel_id': channel_id, 'topic': topic}

    # Add the new job to the queue
    context.job_queue.run_repeating(
        post_to_channel,
        interval=interval,
        first=10,  # Start after 10 seconds
        name=job_name,
        data=job_data
    )
    
    await update.effective_message.reply_text("I have everything I need! The posting schedule has now started.")
    logger.info(f"Scheduled posts for channel {channel_id} with topic '{topic}' every {interval} seconds.")

async def stop(update, context: ContextTypes.DEFAULT_TYPE):
    """Stops the scheduled posting."""
    chat_id = update.effective_message.chat_id
    job_name = f"post_job_{chat_id}"
    
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    if not current_jobs:
        await update.message.reply_text("There was no active schedule to stop.")
        return
        
    for job in current_jobs:
        job.schedule_removal()
        
    await update.message.reply_text("I have stopped the posting schedule.")
    logger.info(f"Stopped posting schedule for job: {job_name}")


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

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()
