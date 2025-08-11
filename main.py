import os
import json
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import smtplib
from email.message import EmailMessage

# ===== Логи =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ===== Переменные окружения =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HR_EMAIL = os.getenv("HR_EMAIL", "").strip()
EMAIL_USER = os.getenv("EMAIL_USER", "").strip()
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip()
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "").strip()
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

# Проверяем, что всё есть
required_vars = {
    "BOT_TOKEN": BOT_TOKEN,
    "HR_EMAIL": HR_EMAIL,
    "EMAIL_USER": EMAIL_USER,
    "EMAIL_PASSWORD": EMAIL_PASSWORD,
    "SPREADSHEET_ID": SPREADSHEET_ID,
    "DRIVE_FOLDER_ID": DRIVE_FOLDER_ID,
    "GOOGLE_CREDENTIALS_JSON": GOOGLE_CREDENTIALS_JSON
}

for var, value in required_vars.items():
    if not value:
        raise ValueError(f"Missing environment variable: {var}")

# ===== Google API =====
SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']
creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)

drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)

# ===== Telegram Handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Пришли своё резюме (PDF или DOCX).")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        file = await update.message.document.get_file()
        file_name = update.message.document.file_name
        file_path = f"/tmp/{file_name}"
        await file.download_to_drive(file_path)

        send_email(file_path, file_name)
        upload_to_drive(file_path, file_name)
        append_to_sheet([file_name])

        await update.message.reply_text("Спасибо! Резюме получено.")

    except Exception as e:
        logging.exception("Ошибка при обработке документа")
        await update.message.reply_text("Произошла ошибка. Попробуйте снова.")

# ===== Email =====
def send_email(file_path, file_name):
    msg = EmailMessage()
    msg["From"] = EMAIL_USER
    msg["To"] = HR_EMAIL
    msg["Subject"] = f"Новое резюме: {file_name}"

    with open(file_path, "rb") as f:
        file_data = f.read()
    msg.add_attachment(file_data, maintype="application", subtype="octet-stream", filename=file_name)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)

# ===== Google Drive =====
def upload_to_drive(file_path, file_name):
    file_metadata = {"name": file_name, "parents": [DRIVE_FOLDER_ID]}
    media = MediaFileUpload(file_path, resumable=True)
    drive_service.files().create(body=file_metadata, media_body=media, fields="id").execute()

# ===== Google Sheets =====
def append_to_sheet(values):
    body = {"values": [values]}
    sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Лист1!A:A",
        valueInputOption="RAW",
        body=body
    ).execute()

# ===== Запуск =====
if __name__ == "__main__":
    logging.info("Starting bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()
