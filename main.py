import os
import re
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

# ================= Logging =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ================= Load .env =================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
SHEET_TAB = os.getenv("GOOGLE_SHEETS_TAB", "Leads")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN not found in .env!")
    exit(1)

# ================= Google APIs =================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]

# 🔑 Важно: должен лежать рядом с main.py
creds = Credentials.from_authorized_user_file("token.json", SCOPES)

sheets_service = build("sheets", "v4", credentials=creds)
drive_service = build("drive", "v3", credentials=creds)

def append_to_sheet(values: list):
    try:
        body = {"values": [values]}
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_TAB}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
        logger.info("✅ Row added to Google Sheets")
    except Exception as e:
        logger.error(f"❌ Error writing to Google Sheets: {e}")

def upload_to_drive(local_path: str, filename: str, mime_type: str = None) -> str:
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
    body = {"name": filename}
    if GOOGLE_DRIVE_FOLDER_ID:
        body["parents"] = [GOOGLE_DRIVE_FOLDER_ID]
    try:
        file = drive_service.files().create(
            body=body,
            media_body=media,
            fields="id, webViewLink"
        ).execute()
        return file.get("webViewLink")
    except Exception as e:
        logger.error(f"❌ Error uploading to Drive: {e}")
        return ""

# ================= Temp dir =================
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "tg_cv_bot"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# ================= States =================
(NAME, EMAIL, PHONE, POSITION, SOURCE, WAITING_FILE) = range(6)

# ================= UI =================
BTN_SEND = "Send CV"
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(BTN_SEND)]], resize_keyboard=True)

# ================= Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome! Please choose an option.", reply_markup=main_menu_kb())

async def apply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("What is your full name?")
    return NAME

async def ask_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("What is your email address?")
    return EMAIL

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["email"] = update.message.text
    await update.message.reply_text("What is your phone number?")
    return PHONE

async def ask_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["phone"] = update.message.text
    await update.message.reply_text("Which position are you applying for?")
    return POSITION

async def ask_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["position"] = update.message.text
    await update.message.reply_text("Where did you find this vacancy?")
    return SOURCE

async def wait_for_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["source"] = update.message.text
    await update.message.reply_text("Please send your CV (PDF/DOC/DOCX or JPG/PNG).")
    return WAITING_FILE

async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_obj = None
    filename = None
    mime_type = None

    if update.message.document:
        doc = update.message.document
        file_obj = await context.bot.get_file(doc.file_id)
        filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', doc.file_name or "cv.pdf")
        mime_type = doc.mime_type
    elif update.message.photo:
        photo = update.message.photo[-1]
        file_obj = await context.bot.get_file(photo.file_id)
        filename = f"cv_{photo.file_id}.jpg"
        mime_type = "image/jpeg"
    else:
        await update.message.reply_text("⚠️ Please upload your CV as PDF/DOC/DOCX or JPG/PNG.")
        return WAITING_FILE

    local_path = DOWNLOAD_ROOT / filename
    try:
        await file_obj.download_to_drive(custom_path=str(local_path))
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"File not saved: {local_path}")
        logger.info(f"📥 File saved locally: {local_path}")
    except Exception as e:
        logger.error(f"❌ Error saving file: {e}")
        await update.message.reply_text(f"Error saving your CV: {e}")
        return WAITING_FILE

    try:
        logger.info(f"⬆️ Uploading {local_path} to Google Drive...")
        drive_link = upload_to_drive(str(local_path), filename, mime_type)
        if drive_link:
            logger.info(f"✅ Uploaded to Drive: {drive_link}")
        else:
            logger.warning("⚠️ File uploaded but no link returned")
    except Exception as e:
        logger.error(f"❌ Error uploading to Drive: {e}")
        await update.message.reply_text("CV saved locally, but failed to upload to Drive.")
        return ConversationHandler.END

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    append_to_sheet([
        now,
        context.user_data.get("name"),
        context.user_data.get("email"),
        context.user_data.get("phone"),
        context.user_data.get("position"),
        context.user_data.get("source"),
        filename,
        drive_link
    ])

    await update.message.reply_text(
        "✅ Thank you! Your CV has been successfully received and uploaded. We will contact you soon.",
        reply_markup=main_menu_kb()
    )
    return ConversationHandler.END

# ================= Main =================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"(?i)^send cv$"), apply_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_email)],
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_position)],
            POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_source)],
            SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_for_file)],
            WAITING_FILE: [MessageHandler(filters.Document.ALL | filters.PHOTO, receive_document)],
        },
        fallbacks=[],
    )
    app.add_handler(conv)
    logger.info("🚀 Bot started and waiting for CV submissions")
    app.run_polling()

if __name__ == "__main__":
    main()
