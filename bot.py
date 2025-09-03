# bot.py
import os
import logging
import random
import asyncio
import pickle
from collections import defaultdict
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    BasePersistence,
)
from telegram.error import TelegramError, BadRequest
from telegram.constants import ChatMemberStatus
import google.generativeai as genai

# --- Setup and Configuration ---
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- MongoDB Persistence Class (Corrected & Complete) ---
class MongoPersistence(BasePersistence):
    """
    A complete and corrected custom persistence class that uses MongoDB to store bot data.
    """
    def __init__(self, mongo_uri: str, database_name: str = 'telegram_bot_db'):
        super().__init__(store_user_data=True, store_chat_data=True, store_bot_data=True)
        self.client = MongoClient(mongo_uri)
        self.db = self.client[database_name]
        self.user_collection = self.db['user_data']
        self.chat_collection = self.db['chat_data']
        self.bot_collection = self.db['bot_data']
        self.conv_collection = self.db['conversations']
        self.callback_collection = self.db['callback_data'] # Added for completeness

    async def get_bot_data(self) -> dict:
        record = self.bot_collection.find_one({'_id': 'bot_data'})
        return pickle.loads(record['data']) if record else {}

    async def update_bot_data(self, data: dict) -> None:
        pickled_data = pickle.dumps(data)
        self.bot_collection.update_one({'_id': 'bot_data'}, {'$set': {'data': pickled_data}}, upsert=True)

    def _get_data_from_collection(self, collection) -> defaultdict:
        data = defaultdict(dict)
        for record in collection.find():
            key = record.get('_id')
            value = record.get('data')
            if key and value:
                data[key] = pickle.loads(value)
        return data

    async def get_user_data(self) -> defaultdict:
        return self._get_data_from_collection(self.user_collection)

    async def update_user_data(self, user_id: int, data: dict) -> None:
        if not data:
            self.user_collection.delete_one({'_id': user_id})
            return
        pickled_data = pickle.dumps(data)
        self.user_collection.update_one({'_id': user_id}, {'$set': {'data': pickled_data}}, upsert=True)

    async def get_chat_data(self) -> defaultdict:
        return self._get_data_from_collection(self.chat_collection)

    async def update_chat_data(self, chat_id: int, data: dict) -> None:
        if not data:
            self.chat_collection.delete_one({'_id': chat_id})
            return
        pickled_data = pickle.dumps(data)
        self.chat_collection.update_one({'_id': chat_id}, {'$set': {'data': pickled_data}}, upsert=True)

    async def get_conversations(self, name: str) -> dict:
        record = self.conv_collection.find_one({'_id': name})
        return pickle.loads(record['data']) if record else {}

    async def update_conversation(self, name: str, key: tuple[int, ...], new_state: object | None) -> None:
        conversations = await self.get_conversations(name)
        if new_state is not None:
            conversations[key] = new_state
        else:
            conversations.pop(key, None)
        
        if conversations:
            pickled_data = pickle.dumps(conversations)
            self.conv_collection.update_one({'_id': name}, {'$set': {'data': pickled_data}}, upsert=True)
        else:
            self.conv_collection.delete_one({'_id': name})
    
    # --- Implementing the missing abstract methods ---

    async def drop_chat_data(self, chat_id: int) -> None:
        self.chat_collection.delete_one({'_id': chat_id})

    async def drop_user_data(self, user_id: int) -> None:
        self.user_collection.delete_one({'_id': user_id})

    async def get_callback_data(self) -> dict | None:
        record = self.callback_collection.find_one({'_id': 'callback_data'})
        return pickle.loads(record['data']) if record else None

    async def update_callback_data(self, data: dict) -> None:
        if data:
            pickled_data = pickle.dumps(data)
            self.callback_collection.update_one({'_id': 'callback_data'}, {'$set': {'data': pickled_data}}, upsert=True)
        else:
            self.callback_collection.delete_one({'_id': 'callback_data'})

    # refresh_* methods are for in-memory caches, which we don't use. So they can be no-ops.
    async def refresh_bot_data(self, bot_data: dict) -> None:
        pass  # Data is always fetched fresh from DB

    async def refresh_chat_data(self, chat_id: int, chat_data: dict) -> None:
        pass  # Data is always fetched fresh from DB

    async def refresh_user_data(self, user_id: int, user_data: dict) -> None:
        pass  # Data is always fetched fresh from DB

    async def flush(self) -> None:
        pass # Data is saved on update, so flush is not needed.

# --- State Constants for ConversationHandler ---
ADD_CHANNEL, ADD_TOPIC, ADD_SCHEDULE_BASE, ADD_SCHEDULE_RANDOM = range(4)
REMOVE_CHANNEL = range(1)
EDIT_CHANNEL_CHOICE, EDIT_TOPIC_RECEIVE = range(2)
POSTNOW_CHANNEL_CHOICE = range(1)

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

async def generate_welcome_message() -> str:
    """Generates a short, friendly welcome message using the Gemini AI."""
    try:
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        prompt = "Create a very short, friendly, and slightly enthusiastic welcome message for a user starting a Telegram bot. The bot helps automate channel posting. Just one or two sentences."
        response = await model.generate_content_async(prompt)
        return response.text if response.text else "Welcome! I'm ready to help you manage your channels."
    except Exception as e:
        logger.error(f"Error generating welcome message: {e}")
        return "Welcome! Let's get your channels automated."


# --- Core Bot & Job Functions ---
async def post_to_channel(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    channel_id, topic, base_seconds, random_seconds, chat_id = (
        job.data['channel_id'], job.data['topic'], job.data['base_seconds'],
        job.data['random_seconds'], job.chat_id
    )
    logger.info(f"Executing job '{job.name}': Posting topic '{topic}' to {channel_id}")
    content = await generate_ai_content(topic)
    try:
        await context.bot.send_message(chat_id=channel_id, text=content)
        logger.info(f"Successfully posted to {channel_id}")
    except (TelegramError, BadRequest) as e:
        logger.error(f"Failed to post to {channel_id}: {e}. Removing job.")
        remove_job_if_exists(job.name, context)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ I couldn't post to {channel_id} (Reason: {e}). The schedule for this channel has been stopped. Please check my admin permissions and re-add the channel."
        )
        if context.user_data.get('channels', {}).get(channel_id):
            del context.user_data['channels'][channel_id]
        return

    delay = base_seconds + random.uniform(-random_seconds, random_seconds)
    next_run_time = max(60, delay)
    context.job_queue.run_once(
        post_to_channel, when=next_run_time, data=job.data, name=job.name, chat_id=chat_id
    )
    logger.info(f"Job '{job.name}' has been rescheduled to run in {next_run_time:.2f} seconds.")

def schedule_first_job_for_channel(context: ContextTypes.DEFAULT_TYPE, chat_id: int, channel_id: str, topic: str, base_seconds: int, random_seconds: int):
    job_name = f"post_job_{chat_id}_{channel_id.replace('@', '')}"
    remove_job_if_exists(job_name, context)
    job_data = {
        'channel_id': channel_id, 'topic': topic,
        'base_seconds': base_seconds, 'random_seconds': random_seconds
    }
    context.job_queue.run_once(
        post_to_channel, when=10, data=job_data, name=job_name, chat_id=chat_id
    )
    logger.info(f"Scheduled first job: '{job_name}'.")

def remove_job_if_exists(name: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    current_jobs = context.job_queue.get_jobs_by_name(name)
    if not current_jobs: return False
    for job in current_jobs:
        job.schedule_removal()
    logger.info(f"Removed job: {name}")
    return True

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = await generate_welcome_message()
    await update.message.reply_text(f"{welcome_text}\n\nUse /help for a list of commands.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    commands_text = (
        "Here are the available commands:\n\n"
        "**SETUP & MANAGE**\n"
        "🔹 /addchannel - Add a new channel and configure its posting schedule.\n"
        "🔹 /removechannel - Stop posting to a specific channel.\n"
        "🔹 /edittopic - Change the content topic for an existing channel.\n"
        "🔹 /listchannels - View all channels you have configured.\n\n"
        "**POSTING**\n"
        "🔸 /postnow - Post immediately to a single chosen channel.\n"
        "🔸 /broadcast - Post immediately to ALL configured channels.\n\n"
        "🔹 /help - Show this message again.\n"
        "🔹 /cancel - Stop any current setup process."
    )
    await update.message.reply_text(commands_text, parse_mode='Markdown')

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('channels'):
        await update.message.reply_text("You haven't added any channels yet. Use /addchannel to start.")
        return
    message = "Here are your configured channels:\n\n"
    for channel, config in context.user_data['channels'].items():
        topic = config['topic']
        base_h = config['base_seconds'] / 3600
        rand_h = config['random_seconds'] / 3600
        message += (
            f"▪️ **Channel:** `{channel}`\n"
            f"   **Topic:** `{topic}`\n"
            f"   **Schedule:** Every `{base_h:.2f} ± {rand_h:.2f}` hours\n\n"
        )
    await update.message.reply_text(message, parse_mode='MarkdownV2')

# --- Conversation Handlers (Add, Remove, Edit, Postnow, Broadcast) ---
# The logic for these conversations remains unchanged.

async def add_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Let's add a new channel. What is the channel's username? (Must be in @username format)")
    return ADD_CHANNEL

async def add_channel_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_name = update.message.text
    if not channel_name.startswith('@'):
        await update.message.reply_text("Invalid format. Please provide the username starting with '@' (e.g., @mychannel).")
        return ADD_CHANNEL
    try:
        member = await context.bot.get_chat_member(chat_id=channel_name, user_id=context.bot.id)
        if member.status != ChatMemberStatus.ADMINISTRATOR or not member.can_post_messages:
            await update.message.reply_text("I need to be an administrator with 'Post messages' permission in that channel. Please grant me the rights and try again.")
            return ConversationHandler.END
    except (BadRequest, TelegramError) as e:
        logger.error(f"Error checking permissions for {channel_name}: {e}")
        await update.message.reply_text("I couldn't find that channel or I'm not a member. Please add me to the channel first and then try again.")
        return ConversationHandler.END
    context.user_data['temp_channel_name'] = channel_name
    await update.message.reply_text(f"Great, I have the necessary permissions for {channel_name}. Now, what topic should I post about?")
    return ADD_TOPIC

async def add_channel_receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['temp_channel_topic'] = update.message.text
    await update.message.reply_text("Got it. What's the base posting interval in hours? (e.g., 8). You can use decimals.")
    return ADD_SCHEDULE_BASE

async def add_channel_receive_schedule_base(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        base_hours = float(update.message.text)
        if base_hours <= 0:
            await update.message.reply_text("Please enter a positive number of hours.")
            return ADD_SCHEDULE_BASE
        if base_hours < 1:
            await update.message.reply_text("⚠️ **Warning:** A schedule of less than 1 hour is risky and may lead to Telegram limiting your bot. Proceed with caution.", parse_mode='Markdown')
        context.user_data['temp_base_seconds'] = base_hours * 3600
        await update.message.reply_text("Okay. Now, what's the random time range in hours? (e.g., enter '2' for ±2 hours). Enter '0' for no randomization.")
        return ADD_SCHEDULE_RANDOM
    except ValueError:
        await update.message.reply_text("That's not a valid number. Please enter the interval in hours.")
        return ADD_SCHEDULE_BASE

async def add_channel_receive_schedule_random(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        random_hours = float(update.message.text)
        if random_hours < 0:
            await update.message.reply_text("Please enter a positive number or 0.")
            return ADD_SCHEDULE_RANDOM
        random_seconds = random_hours * 3600
        channel_id = context.user_data.pop('temp_channel_name')
        topic = context.user_data.pop('temp_channel_topic')
        base_seconds = context.user_data.pop('temp_base_seconds')
        if 'channels' not in context.user_data:
            context.user_data['channels'] = {}
        context.user_data['channels'][channel_id] = {
            'topic': topic, 'base_seconds': base_seconds, 'random_seconds': random_seconds
        }
        schedule_first_job_for_channel(context, chat_id, channel_id, topic, base_seconds, random_seconds)
        await update.message.reply_text(f"Success! I've scheduled posts about '{topic}' to {channel_id}.")
        return ConversationHandler.END
    except (ValueError, KeyError):
        await update.message.reply_text("Something went wrong. Please start over with /addchannel.")
        return ConversationHandler.END

async def remove_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('channels'):
        await update.message.reply_text("You don't have any channels to remove.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(name, callback_data=name)] for name in context.user_data['channels']]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Please choose which channel to stop posting to:', reply_markup=reply_markup)
    return REMOVE_CHANNEL

async def remove_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_to_remove = query.data
    job_name = f"post_job_{query.message.chat_id}_{channel_to_remove.replace('@', '')}"
    remove_job_if_exists(job_name, context)
    if channel_to_remove in context.user_data.get('channels', {}):
        del context.user_data['channels'][channel_to_remove]
        await query.edit_message_text(text=f"I have stopped posting to {channel_to_remove}.")
    else:
        await query.edit_message_text(text=f"Could not find {channel_to_remove}.")
    return ConversationHandler.END

async def edittopic_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('channels'):
        await update.message.reply_text("You don't have any channels configured to edit.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(name, callback_data=name)] for name in context.user_data['channels']]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Which channel do you want to change the topic for?', reply_markup=reply_markup)
    return EDIT_CHANNEL_CHOICE

async def edittopic_choose_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['temp_channel_to_edit'] = query.data
    await query.edit_message_text(text=f"Okay, what is the new topic for {query.data}?")
    return EDIT_TOPIC_RECEIVE

async def edittopic_receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_topic = update.message.text
    try:
        channel_id = context.user_data.pop('temp_channel_to_edit')
        config = context.user_data['channels'][channel_id]
        config['topic'] = new_topic
        schedule_first_job_for_channel(context, update.effective_chat.id, channel_id, new_topic, config['base_seconds'], config['random_seconds'])
        await update.message.reply_text(f"Topic for {channel_id} has been updated to '{new_topic}'.")
    except KeyError:
        await update.message.reply_text("Something went wrong. Please start over with /edittopic.")
    return ConversationHandler.END

async def postnow_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('channels'):
        await update.message.reply_text("You don't have any channels to post to.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(name, callback_data=name)] for name in context.user_data['channels']]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Which channel would you like to post to now?', reply_markup=reply_markup)
    return POSTNOW_CHANNEL_CHOICE

async def postnow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = query.data
    await query.edit_message_text(text=f"Generating content for {channel_id}...")
    try:
        config = context.user_data['channels'][channel_id]
        content = await generate_ai_content(config['topic'])
        await context.bot.send_message(chat_id=channel_id, text=content)
        await query.edit_message_text(text=f"✅ Successfully posted to {channel_id}!")
    except (KeyError, TelegramError, BadRequest) as e:
        await query.edit_message_text(text=f"❌ Failed to post to {channel_id}. Reason: {e}")
    return ConversationHandler.END

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('channels'):
        await update.message.reply_text("You have no channels configured to broadcast to.")
        return
    await update.message.reply_text(f"Starting broadcast to {len(context.user_data['channels'])} channels...")
    success_count, error_count = 0, 0
    for channel_id, config in context.user_data['channels'].items():
        try:
            content = await generate_ai_content(config['topic'])
            await context.bot.send_message(chat_id=channel_id, text=content)
            logger.info(f"Broadcast successful to {channel_id}")
            success_count += 1
        except (TelegramError, BadRequest) as e:
            logger.error(f"Broadcast failed for {channel_id}: {e}")
            error_count += 1
        await asyncio.sleep(1)
    await update.message.reply_text(f"Broadcast complete!\n✅ Success: {success_count}\n❌ Failed: {error_count}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in list(context.user_data.keys()):
        if key.startswith('temp_'):
            del context.user_data[key]
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

async def post_init(application: Application) -> None:
    """Reschedule all jobs on bot startup from persisted data."""
    # A deep copy is needed because the data might be modified during iteration
    user_data_copy = copy.deepcopy(application.user_data)
    for user_id, user_data in user_data_copy.items():
        if 'channels' in user_data:
            for channel_id, config in user_data['channels'].items():
                logger.info(f"Rescheduling job from DB for user {user_id}, channel {channel_id}")
                # The chat_id for run_once is the user's private chat
                schedule_first_job_for_channel(
                    application, user_id, channel_id,
                    config['topic'], config['base_seconds'], config['random_seconds']
                )

# --- Main Application Setup ---
def main():
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    mongo_uri = os.getenv("MONGODB_URI")
    gemini_api_key_env = os.getenv("GEMINI_API_KEY")

    if not all([telegram_token, mongo_uri, gemini_api_key_env]):
        logger.error("One or more environment variables (TELEGRAM_BOT_TOKEN, MONGODB_URI, GEMINI_API_KEY) are missing.")
        return

    persistence = MongoPersistence(mongo_uri=mongo_uri)

    application = (
        Application.builder()
        .token(telegram_token)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )

    # Conversation Handlers
    add_handler = ConversationHandler(
        entry_points=[CommandHandler('addchannel', add_channel_start)],
        states={
            ADD_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_receive_name)],
            ADD_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_receive_topic)],
            ADD_SCHEDULE_BASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_receive_schedule_base)],
            ADD_SCHEDULE_RANDOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_receive_schedule_random)],
        }, fallbacks=[CommandHandler('cancel', cancel)], persistent=True, name="add_channel_conv"
    )
    remove_handler = ConversationHandler(
        entry_points=[CommandHandler('removechannel', remove_channel_start)],
        states={REMOVE_CHANNEL: [CallbackQueryHandler(remove_channel_callback)]},
        fallbacks=[CommandHandler('cancel', cancel)], persistent=True, name="remove_channel_conv"
    )
    edit_handler = ConversationHandler(
        entry_points=[CommandHandler('edittopic', edittopic_start)],
        states={
            EDIT_CHANNEL_CHOICE: [CallbackQueryHandler(edittopic_choose_channel_callback)],
            EDIT_TOPIC_RECEIVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edittopic_receive_topic)],
        }, fallbacks=[CommandHandler('cancel', cancel)], persistent=True, name="edit_topic_conv"
    )
    postnow_handler = ConversationHandler(
        entry_points=[CommandHandler('postnow', postnow_start)],
        states={POSTNOW_CHANNEL_CHOICE: [CallbackQueryHandler(postnow_callback)]},
        fallbacks=[CommandHandler('cancel', cancel)], persistent=True, name="post_now_conv"
    )

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("listchannels", list_channels))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(add_handler)
    application.add_handler(remove_handler)
    application.add_handler(edit_handler)
    application.add_handler(postnow_handler)

    application.run_polling()

if __name__ == '__main__':
    main()
