import logging
import random
import os
import asyncio
import json
from flask import Flask, request, Response

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    TypeHandler # Needed for processing updates manually
)
from telegram.error import BadRequest
from dotenv import load_dotenv
from pymongo import MongoClient, errors as pymongo_errors
from datetime import datetime, timedelta # For subscription expiry

# Load environment variables from .env file (if it exists)
load_dotenv()

# === CONFIG ===
# Get token from environment variable
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    TOKEN = 'FALLBACK_TOKEN_HERE' # Replace with your bot token if not using .env
    if TOKEN == 'FALLBACK_TOKEN_HERE':
        print("CRITICAL: Bot token not found. Set TELEGRAM_BOT_TOKEN in your .env file or environment variables, or directly in the script.")
        exit()
    else:
        print("WARNING: Using fallback token from script. Set TELEGRAM_BOT_TOKEN in environment for deployment.")

# Webhook/Port setup
PORT = int(os.environ.get('PORT', 8443))
WEBHOOK_MODE = os.environ.get('WEBHOOK_MODE', 'False').lower() == 'true'
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL")

if WEBHOOK_MODE and not WEBHOOK_URL_BASE:
    print("CRITICAL: WEBHOOK_MODE is True, but WEBHOOK_URL environment variable is not set.")
    exit()

WEBHOOK_PATH = "webhook"
WEBHOOK_FULL_URL = f"{WEBHOOK_URL_BASE}/{WEBHOOK_PATH}" if WEBHOOK_URL_BASE else None

# Quiz file and batch size
QUIZ_FILE = 'tests.txt'
QUESTIONS_PER_BATCH = 10

# === STATES for ConversationHandler ===
SELECTING_SUBJECT, QUIZ_IN_PROGRESS = range(2)

# === Logging Setup ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# === MONGODB CONFIGURATION ===
# !!! REPLACE with YOUR details or use environment variables !!!
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://admin:Kratos2004@cluster1.mkyyr.mongodb.net/Sher-gost-bot?retryWrites=true&w=majority")
DB_NAME = os.environ.get("DB_NAME", "Sher-gost-bot") # Default DB name, can be overridden by env var
PAID_USERS_COLLECTION_NAME = "sher-bot"
YOUR_ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", 6202344101)) # !!! REPLACE 0 with YOUR Telegram admin chat_id !!!

if YOUR_ADMIN_CHAT_ID == 0:
    logger.warning("CRITICAL: ADMIN_CHAT_ID is not set. The /addsubscriber command will not be secure.")
    # Consider exiting if admin functionality is critical and ID is not set.

# === Initialize MongoDB Client ===
try:
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    paid_users_collection = db[PAID_USERS_COLLECTION_NAME]
    paid_users_collection.create_index("chat_id", unique=True) # Ensures chat_id is unique and indexed
    logger.info(f"Successfully connected to MongoDB: {DB_NAME} and collection: {PAID_USERS_COLLECTION_NAME}")
except pymongo_errors.ConnectionFailure as e:
    logger.critical(f"Could not connect to MongoDB: {e}")
    exit() # Critical to have DB for paid features
except Exception as e:
    logger.critical(f"An error occurred during MongoDB setup: {e}")
    exit()


# === Utils ===
def load_questions(file_path):
    """Loads questions from a text file into a dictionary by subject."""
    subjects = {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        logger.error(f"Error: Quiz file not found at {file_path}")
        return subjects

    current_subject = None
    blocks = content.replace('\r\n', '\n').strip().split('\n\n')

    for block in blocks:
        lines = block.strip().split('\n')
        if lines[0].startswith("Subject:"):
            try:
                current_subject = lines[0].split(":", 1)[1].strip()
                if current_subject:
                    subjects[current_subject] = []
                    logger.info(f"Found subject: {current_subject}")
                else:
                    logger.warning(f"Found empty subject name in block: {block}")
                    current_subject = None
            except IndexError:
                logger.warning(f"Malformed Subject line: {lines[0]}")
                current_subject = None
            continue

        if current_subject is None:
            logger.warning(f"Skipping block due to missing subject context: {block}")
            continue

        if len(lines) < 6:
            logger.warning(f"Skipping malformed block (less than 6 lines) for subject '{current_subject}': {block}")
            continue

        if current_subject not in subjects:
            logger.error(f"Internal logic error: Subject '{current_subject}' not initialized.")
            continue

        try:
            question_text = lines[0]
            options = lines[1:5]
            answer_line = lines[5]

            if not all(len(opt) > 2 and opt[1] == ')' and opt[0].isalpha() for opt in options):
                logger.warning(f"Malformed options format in block for subject '{current_subject}': {options}")
                continue
            if not answer_line.startswith("Answer:"):
                logger.warning(f"Malformed answer line format for subject '{current_subject}': {answer_line}")
                continue

            correct_answer_letter = answer_line.split(":", 1)[1].strip()
            if not correct_answer_letter or len(correct_answer_letter) != 1 or not correct_answer_letter.isalpha():
                logger.warning(f"Invalid correct answer letter '{correct_answer_letter}' for subject '{current_subject}': {answer_line}")
                continue

            subjects[current_subject].append({
                'question': question_text,
                'options': options,
                'correct': correct_answer_letter.upper()
            })
        except IndexError:
            logger.warning(f"Skipping block due to parsing error (IndexError) for subject '{current_subject}': {block}")
        except Exception as e:
            logger.error(f"Unexpected error parsing block for subject '{current_subject}': {e}\nBlock: {block}", exc_info=True)

    logger.info(f"Loaded subjects: {list(subjects.keys())}")
    if not subjects:
        logger.warning("No subjects were loaded. Check tests.txt format and content.")
    return subjects

# === Helper Function for Start Keyboard ===
def get_start_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup | None:
    known_subjects = ["amaliy-dasturlar" , "dasturlash-tillari-bazasi" , "OT" ,"KTT"] # These should match keys from tests.txt
    keyboard = []
    loaded_subjects = context.bot_data.get('questions', {})

    for subj in known_subjects:
        if subj in loaded_subjects and loaded_subjects[subj]:
            keyboard.append([InlineKeyboardButton(text=subj, callback_data=f"subj|{subj}")])
        else:
            logger.warning(f"Subject '{subj}' hardcoded but not loaded or has no questions. Skipping button.")

    if any(loaded_subjects.values()):
        keyboard.append([InlineKeyboardButton(text="Random 50 savol", callback_data="random")])

    return InlineKeyboardMarkup(keyboard) if keyboard else None

# === Subscription Helper Function ===
def is_user_subscribed(chat_id: int) -> bool:
    """Checks if a user is subscribed and their subscription is active."""
    try:
        user_doc = paid_users_collection.find_one({"chat_id": chat_id})
        if not user_doc:
            return False  # User not found

        # Check for subscription expiry if the field exists and is not None
        if "subscription_expires_at" in user_doc and user_doc["subscription_expires_at"] is not None:
            if user_doc["subscription_expires_at"] < datetime.now():
                logger.info(f"Subscription for chat_id {chat_id} has expired on {user_doc['subscription_expires_at']}.")
                # Optional: You could remove the user or mark them as expired in the DB
                # paid_users_collection.delete_one({"chat_id": chat_id})
                return False # Subscription expired
        
        # If no expiry date, or if expiry date is in the future, they are subscribed
        return True
    except Exception as e:
        logger.error(f"Error checking subscription status for chat_id {chat_id}: {e}")
        return False # Fail-safe: if there's an error, deny access

# === Bot Handlers (Async v20 Style) ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handles the /start command.
    Checks if user is subscribed. If so, starts quiz selection.
    Otherwise, prompts for payment.
    """
    user = update.effective_user
    chat_id = user.id
    logger.info(f"User {user.id} ({user.first_name}) attempting /start.")

    if is_user_subscribed(chat_id):
        logger.info(f"User {user.id} is subscribed. Proceeding to quiz selection.")
        context.user_data.clear() # Clear previous quiz state for subscribed user
        
        reply_markup = get_start_keyboard(context)
        if not reply_markup:
            await update.message.reply_text(
                "Uzr, hozirda hech qanday fanga doir savollar topilmadi. Doniyor bilan bog'laning."
            )
            return ConversationHandler.END # End if no subjects loaded
        
        await update.message.reply_text(
            f"Salom, {user.first_name}!\nQuiz botimizga xush kelibsiz.\n"
            "Fan tanlang yoki aralash savollardan boshlang:", 
            reply_markup=reply_markup
        )
        return SELECTING_SUBJECT # Proceed to subject selection
    else:
        logger.info(f"User {user.id} is not subscribed. Sending payment info.")
        await update.message.reply_text(
            f"Salom, {user.first_name}! 👋\n"
            "Bu botdagi quizlardan to'liq foydalanish uchun obuna bo'lishingiz kerak.\n\n"
            "Obuna bo'lish uchun /payment buyrug'ini bering yoki admin bilan bog'laning. +998330004136" 
            # Consider adding admin username here e.g. @YourAdminUsername
        )
        return ConversationHandler.END # End conversation if not paid

async def payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Provides payment instructions to the user."""
    chat_id = update.effective_chat.id # For displaying their ID for their convenience
    # !!! REPLACE with your actual payment details and admin contact !!!
    payment_instructions = (
        "Obuna bo'lish uchun quyidagi amallarni bajaring:\n\n"
        "💳 To'lov miqdori: [20000] so'm\n"
        "🏢 To'lov usuli: [PAYME / CLICK / 9860040114871250]\n\n"
        "To'lovni amalga oshirgandan so'ng, to'lov chekining rasmini (screenshot) va Telegram User ID'ingizni "
        f"(`{chat_id}`) quyidagi manzilga yuboring: +998330004136\n\n" # !!! REPLACE @YourAdminUsername !!!
        "Tasdiqlangandan so'ng, sizga botdan foydalanish huquqi beriladi."
    )
    await update.message.reply_text(payment_instructions)

async def add_subscriber_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to add or update a subscriber."""
    admin_chat_id = update.effective_chat.id
    if admin_chat_id != YOUR_ADMIN_CHAT_ID:
        await update.message.reply_text("Kechirasiz, bu buyruq faqat adminlar uchun. 🤫")
        return

    if not context.args or len(context.args) == 0:
        await update.message.reply_text("Qo'llash usuli: /addsubscriber <foydalanuvchi_chat_id> [kun_soni_tugashiga]")
        return

    try:
        target_chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Xato Chat ID. Raqam bo'lishi kerak.")
        return

    expiry_days = None
    if len(context.args) > 1:
        try:
            expiry_days = int(context.args[1])
            if expiry_days <= 0:
                await update.message.reply_text("Kun soni musbat raqam bo'lishi kerak.")
                return
        except ValueError:
            await update.message.reply_text("Xato kun soni. Raqam bo'lishi kerak.")
            return
    
    try:
        user_doc = paid_users_collection.find_one({"chat_id": target_chat_id})
        now = datetime.now()
        new_expiry_date = now + timedelta(days=expiry_days) if expiry_days is not None else None

        if user_doc:
            update_fields = {"$set": {"subscribed_at": user_doc.get("subscribed_at", now)}}
            if new_expiry_date:
                update_fields["$set"]["subscription_expires_at"] = new_expiry_date
            elif "subscription_expires_at" in user_doc: # If expiry_days is None, remove existing expiry
                 update_fields["$unset"] = {"subscription_expires_at": ""}

            paid_users_collection.update_one({"chat_id": target_chat_id}, update_fields)
            expiry_message = f"va {new_expiry_date.strftime('%Y-%m-%d %H:%M')} da tugaydi." if new_expiry_date else "muddatsiz."
            await update.message.reply_text(f"Foydalanuvchi {target_chat_id} uchun obuna yangilandi {expiry_message}")
        else:
            target_username = update.effective_user.username if update.effective_user else None
            subscriber_data = {"chat_id": target_chat_id, "subscribed_at": now, "username": target_username }
            if new_expiry_date:
                subscriber_data["subscription_expires_at"] = new_expiry_date
            
            paid_users_collection.insert_one(subscriber_data)
            expiry_message = f"{new_expiry_date.strftime('%Y-%m-%d %H:%M')} gacha." if new_expiry_date else "muddatsiz."
            await update.message.reply_text(f"Foydalanuvchi {target_chat_id} obunachilar ro'yxatiga qo'shildi {expiry_message} 🎉")

        # Notify the subscribed user
        try:
            confirmation_text = "Tabriklaymiz! Sizning obunangiz faollashtirildi."
            if new_expiry_date:
                confirmation_text += f" U {new_expiry_date.strftime('%Y-%m-%d %H:%M')} gacha amal qiladi."
            confirmation_text += "\nEndi quizlardan foydalanishingiz mumkin. /start buyrug'ini bering!"
            await context.bot.send_message(chat_id=target_chat_id, text=confirmation_text)
        except Exception as notify_error:
            logger.error(f"Faollashtirish xabarini yuborishda xatolik {target_chat_id}: {notify_error}")
            await update.message.reply_text(f"(Foydalanuvchiga faollashtirish xabarini yuborib bo'lmadi: {str(notify_error)})")

    except Exception as e:
        logger.error(f"/addsubscriber buyrug'ida xatolik: {e}")
        await update.message.reply_text("Xatolik yuz berdi. Bot loglarini tekshiring.")


async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer() 
    data = query.data
    user_id = update.effective_user.id

    context.user_data.clear()
    logger.info(f"User {user_id} selected an option '{data}'. Cleared user_data before starting quiz.")

    all_loaded_questions = context.bot_data.get('questions', {})
    questions_to_ask = []
    subject_name = "Unknown"

    if data.startswith("subj|"):
        subject_name = data.split("|", 1)[1]
        if subject_name in all_loaded_questions and all_loaded_questions[subject_name]:
            questions_to_ask = list(all_loaded_questions[subject_name])
            random.shuffle(questions_to_ask)
            logger.info(f"User {user_id} selected subject: {subject_name}, {len(questions_to_ask)} questions.")
        else:
            logger.error(f"User {user_id} clicked button for subject '{subject_name}', but questions not loaded/empty.")
            await query.edit_message_text(f"Kechirasiz, '{subject_name}' fani uchun savollarni yuklashda xatolik.")
            return ConversationHandler.END
    elif data == "random":
        subject_name = 'Random Mix'
        temp_list = []
        if not any(all_loaded_questions.values()):
            logger.error(f"User {user_id} requested random questions, but no subjects/questions loaded.")
            await query.edit_message_text("Kechirasiz, aralashtirish uchun hech qanday savol mavjud emas.")
            return ConversationHandler.END

        total_available = sum(len(qs) for qs in all_loaded_questions.values())
        target_total = min(50, total_available)

        for subj, subj_questions in all_loaded_questions.items():
            if not subj_questions: continue
            proportion = len(subj_questions) / total_available if total_available > 0 else 0
            count = max(1, round(target_total * proportion)) if total_available > 0 else min(10, len(subj_questions))
            actual_count = min(count, len(subj_questions))
            if actual_count > 0:
                temp_list.extend(random.sample(subj_questions, actual_count))
        
        questions_to_ask = temp_list
        random.shuffle(questions_to_ask)
        logger.info(f"User {user_id} selected random questions. Prepared {len(questions_to_ask)} questions.")
    else:
        logger.warning(f"Received unexpected callback data in start_quiz state: {data}")
        await query.edit_message_text("Uzr , kutilmagan xato , botni qayta ishga tushuring /start")
        return ConversationHandler.END

    if not questions_to_ask:
        logger.error(f"Failed to prepare any questions for user {user_id} for selection '{data}'.")
        await query.edit_message_text("Uzr, savollar topilmadi.")
        return ConversationHandler.END

    context.user_data['subject'] = subject_name
    context.user_data['questions'] = questions_to_ask
    context.user_data['index'] = 0
    context.user_data['score'] = 0
    context.user_data['answered_in_batch'] = set()
    context.user_data['current_batch_indices'] = []

    await query.edit_message_text(f"Test boshlanmoqda: {subject_name}")
    await send_next_question_batch(update, context)
    return QUIZ_IN_PROGRESS

async def send_next_question_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current_index = context.user_data.get('index', 0)
    questions = context.user_data.get('questions', [])
    total_questions = len(questions)

    if current_index >= total_questions:
        logger.info(f"send_next_question_batch: No more questions to send for user {user_id}.")
        await context.bot.send_message(chat_id=chat_id, text="Barcha savollarga javob berdingiz!")
        reply_markup = get_start_keyboard(context)
        if reply_markup:
            await context.bot.send_message(chat_id=chat_id, text="Yangi fanni tanlash?", reply_markup=reply_markup)
        return SELECTING_SUBJECT

    if not questions:
        logger.error(f"send_next_question_batch: No questions found for user {user_id}.")
        await context.bot.send_message(chat_id=chat_id, text="Xatolik: savollar topilmadi.")
        context.user_data.clear()
        return ConversationHandler.END

    end_index = min(current_index + QUESTIONS_PER_BATCH, total_questions)
    batch_indices = list(range(current_index, end_index))
    batch_questions = questions[current_index:end_index]

    context.user_data['current_batch_indices'] = batch_indices
    context.user_data['answered_in_batch'] = set()
    logger.info(f"Sending questions {current_index + 1}-{end_index} to user {user_id}. Batch indices: {batch_indices}")

    for i, q_data in enumerate(batch_questions):
        question_global_index = batch_indices[i]
        options_buttons = []
        options_text_parts = []

        valid_options = [opt for opt in q_data.get('options', []) if isinstance(opt, str) and len(opt) > 2 and opt[1] == ')']
        if not valid_options:
            logger.error(f"Q {question_global_index} user {user_id} invalid options: {q_data.get('options')}")
            await context.bot.send_message(chat_id=chat_id, text=f"Q {question_global_index + 1} o'tkazib yuborildi (xato variantlar).")
            context.user_data['answered_in_batch'].add(question_global_index)
            continue

        for opt in valid_options:
            option_letter = opt[0].upper()
            options_text_parts.append(f"{opt}")
            callback_data = f"ans|{question_global_index}|{option_letter}"
            options_buttons.append(InlineKeyboardButton(text=option_letter, callback_data=callback_data))
        
        question_text_body = q_data.get('question', 'Xatolik: Savol matni yo\'q')
        options_text_formatted = "\n".join(options_text_parts)
        full_message_text = f"{question_global_index + 1}. {question_text_body}\n\n{options_text_formatted}"
        
        keyboard = [options_buttons]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=full_message_text,
            reply_markup=reply_markup
        )

    context.user_data['index'] = end_index

    if end_index < total_questions:
        next_button_keyboard = [[InlineKeyboardButton("Keyingi testlar", callback_data="next")]] # Changed text for clarity
        await context.bot.send_message(
            chat_id=chat_id,
            text="Barcha savollarga javob berib bo'lgach, keyingisiga o'ting...",
            reply_markup=InlineKeyboardMarkup(next_button_keyboard)
        )
    return QUIZ_IN_PROGRESS

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        _, qid_str, selected_letter = query.data.split("|")
        qid = int(qid_str)
    except (ValueError, IndexError):
        logger.error(f"Invalid callback data in handle_answer: {query.data}")
        try: await query.edit_message_text("Uzr, no tugma va javob formati.")
        except BadRequest: pass
        return QUIZ_IN_PROGRESS

    questions = context.user_data.get("questions", [])
    total_questions = len(questions)
    current_batch_indices = context.user_data.get("current_batch_indices", [])
    answered_in_batch = context.user_data.get("answered_in_batch", set())

    if not questions or qid >= total_questions:
        logger.error(f"handle_answer: Invalid questions/qid {qid} for user {user_id}.")
        try: await query.edit_message_text("Uzr, savollarni yuklashda xatolik.")
        except BadRequest: pass
        context.user_data.clear()
        return ConversationHandler.END

    if qid in current_batch_indices:
        if qid not in answered_in_batch:
            answered_in_batch.add(qid)
            context.user_data['answered_in_batch'] = answered_in_batch
            logger.info(f"User {user_id} answered question {qid} in current batch. Batch answered: {len(answered_in_batch)}/{len(current_batch_indices)}")
        else:
            logger.info(f"User {user_id} re-answered question {qid} in current batch.")
    else:
        logger.warning(f"User {user_id} answered question {qid} which is not in current batch {current_batch_indices}.")

    question_data = questions[qid]
    correct_answer_letter = question_data.get('correct')
    question_text = question_data.get('question', '[Savol yo\'q]')

    options_text_parts = []
    selected_option_text = f"({selected_letter})"
    correct_option_text = f"({correct_answer_letter})"
    valid_options = question_data.get('options', [])
    for opt in valid_options:
        if isinstance(opt, str) and len(opt) > 0:
            options_text_parts.append(opt)
            if opt.startswith(selected_letter): selected_option_text = opt
            if opt.startswith(correct_answer_letter): correct_option_text = opt
    options_text_formatted = "\n".join(options_text_parts)

    feedback = ""
    correctly_answered_key = f"correct_{qid}"
    is_correct = (selected_letter == correct_answer_letter)

    if is_correct:
        feedback = "✅ !"
        if not context.user_data.get(correctly_answered_key, False):
            context.user_data['score'] = context.user_data.get('score', 0) + 1
            context.user_data[correctly_answered_key] = True
            logger.info(f"User {user_id} answered Q {qid} correctly. Score: {context.user_data['score']}")
        else:
            logger.info(f"User {user_id} re-answered Q {qid} correctly. Score not changed.")
    else:
        feedback = f"❌ Xato!  javob: {correct_option_text}"
    
    updated_text = (
        f"{qid + 1}. {question_text}\n\n"
        f"{options_text_formatted}\n\n"
        f"--------------------\n"
        f"{feedback}\n"
        f"Siz tanladingiz: {selected_option_text}"
    )
    try:
        await query.edit_message_text(text=updated_text, reply_markup=None)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning(f"Could not edit message user {user_id}, qid {qid}: {e}")

    is_last_batch = (total_questions - 1) in current_batch_indices
    all_in_batch_answered = len(answered_in_batch) >= len(current_batch_indices)

    if is_last_batch and all_in_batch_answered:
        score = context.user_data.get('score', 0)
        logger.info(f"User {user_id} finished the final batch. Quiz finished. Score: {score}/{total_questions}")
        
        reply_markup = get_start_keyboard(context)
        finish_text = f"Test tugadi!\nSizning natijangiz: {score}/{total_questions}\n\nYangi fan tanlash?"
        if not reply_markup:
            finish_text = f"Test tugadi!\nSizning natijangiz: {score}/{total_questions}\n(Fan tanlashda xatolik, botni /start bilan qayta ishga tushiring)"
        
        await context.bot.send_message(chat_id=chat_id, text=finish_text, reply_markup=reply_markup)
        return SELECTING_SUBJECT
    else:
        return QUIZ_IN_PROGRESS

async def handle_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    current_batch_indices = context.user_data.get("current_batch_indices", [])
    answered_in_batch = context.user_data.get("answered_in_batch", set())

    if len(answered_in_batch) >= len(current_batch_indices):
        logger.info(f"User {user_id} finished batch {current_batch_indices}, proceeding to next.")
        try: await query.delete_message()
        except BadRequest as e: logger.warning(f"Could not delete 'Next Batch' prompt: {e}")
        return await send_next_question_batch(update, context)
    else:
        remaining_count = len(current_batch_indices) - len(answered_in_batch)
        plural = "ta" # Uzbek doesn't typically pluralize with 's' for count
        logger.info(f"User {user_id} clicked 'Next Batch' prematurely. Answered: {len(answered_in_batch)}/{len(current_batch_indices)}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Iltimos, qolgan {remaining_count} {plural} savolga javob bering!"
        )
        return QUIZ_IN_PROGRESS

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user: logger.info("User %s canceled the conversation.", user.first_name)
    else: logger.info("Conversation canceled (user info not available).")

    cancel_message = "Quiz bekor qilindi. Qayta boshlash uchun /start buyrug'ini bering."
    if update.message:
        await update.message.reply_text(cancel_message)
    elif update.callback_query:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=cancel_message)
        try: await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except BadRequest: pass
    
    context.user_data.clear()
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            if not isinstance(context.error, BadRequest) or "Message is not modified" not in str(context.error):
                await update.effective_message.reply_text("Uzr, kutilmagan xato yuz berdi. /start buyrug'ini bosib qayta uruning.")
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")

# === Flask App Setup ===
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Quiz Bot is alive!"

@flask_app.route(f"/{WEBHOOK_PATH}", methods=["POST"])
async def telegram_webhook():
    if request.is_json:
        update_data = request.get_json()
        # Log the raw update data for debugging if needed
        # logger.debug(f"Webhook received JSON data: {update_data}")
        update = Update.de_json(update_data, application.bot)
        async with application: # Ensure context for async operations
            await application.process_update(update)
        return Response("OK", status=200)
    else:
        logger.warning("Webhook received non-JSON request.")
        return Response("Bad Request", status=400)

# === Main Application Setup ===
loaded_questions = load_questions(QUIZ_FILE)
if not loaded_questions:
    logger.critical(f"CRITICAL: No questions loaded from {QUIZ_FILE}. Bot may not function correctly.")

application = ApplicationBuilder().token(TOKEN).build()
application.bot_data['questions'] = loaded_questions
logger.info(f"Stored {len(loaded_questions)} subjects in bot_data.")

conv_handler = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        SELECTING_SUBJECT: [
            CallbackQueryHandler(start_quiz, pattern="^subj\\|"),
            CallbackQueryHandler(start_quiz, pattern="^random$")
        ],
        QUIZ_IN_PROGRESS: [
            CallbackQueryHandler(handle_answer, pattern="^ans\\|"),
            CallbackQueryHandler(handle_next, pattern="^next$")
        ],
    },
    fallbacks=[CommandHandler("start", start), CommandHandler("cancel", cancel)], # Added cancel to fallbacks
)

application.add_handler(conv_handler)
# Add new payment-related handlers OUTSIDE the conversation handler
application.add_handler(CommandHandler("payment", payment_command))
application.add_handler(CommandHandler("addsubscriber", add_subscriber_command))
# Ensure cancel is also available globally if desired, or keep it only in fallbacks
# application.add_handler(CommandHandler("cancel", cancel)) # If you want /cancel to work anytime

application.add_error_handler(error_handler)

# === Running the Application ===
async def setup_webhook():
    logger.info(f"Attempting to set webhook to: {WEBHOOK_FULL_URL}")
    if not WEBHOOK_FULL_URL:
        logger.error("WEBHOOK_FULL_URL is not defined. Cannot set webhook.")
        return False
    try:
        await application.initialize()
        webhook_info = await application.bot.get_webhook_info()
        if webhook_info.url != WEBHOOK_FULL_URL:
            logger.info(f"Webhook currently set to '{webhook_info.url}'. Setting to '{WEBHOOK_FULL_URL}'...")
            await application.bot.set_webhook(url=WEBHOOK_FULL_URL, allowed_updates=Update.ALL_TYPES)
            new_webhook_info = await application.bot.get_webhook_info()
            if new_webhook_info.url == WEBHOOK_FULL_URL:
                logger.info("Webhook set successfully.")
                return True
            else:
                logger.error(f"Failed to set webhook. Current URL: {new_webhook_info.url}")
                return False
        else:
            logger.info("Webhook is already set correctly.")
            return True
    except Exception as e:
        logger.error(f"Exception during webhook setup: {e}", exc_info=True)
        return False

async def main_async_setup():
    if WEBHOOK_MODE:
        logger.info("Running webhook setup...")
        await setup_webhook()
    else:
        logger.info("Polling mode enabled. Skipping webhook setup.")

if __name__ == "__main__":
    try:
        asyncio.run(main_async_setup())
    except Exception as e:
        logger.error(f"Error during initial async setup: {e}", exc_info=True)

    if WEBHOOK_MODE:
        logger.info(f"Starting Flask server on host 0.0.0.0 port {PORT} for webhook...")
        # For production, consider using a WSGI server like gunicorn or waitress
        # from waitress import serve
        # serve(flask_app, host='0.0.0.0', port=PORT)
        flask_app.run(host="0.0.0.0", port=PORT, debug=False) # debug=False for production
    else:
        logger.info("Starting bot polling...")
        try:
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        except KeyboardInterrupt:
            logger.info("Polling stopped manually.")
        except Exception as e:
            logger.error(f"Error during polling: {e}", exc_info=True)