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

# Логи
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
HR_EMAIL = os.getenv("HR_EMAIL")

# Google API setup
SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']

# Загружаем учетные данные из переменной окружения
creds_info = json.loads(os.environ['GOOGLE_CREDENTIALS_JSON'])
creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)

# Инициализация сервисов
drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)

# ID таблицы и папки на Google Drive (создай заранее и добавь в env)
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Пришли мне своё резюме в формате PDF или DOCX.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        file = await update.message.document.get_file()
        file_name = update.message.document.file_name
        file_path = f"/tmp/{file_name}"
        await file.download_to_drive(file_path)

        # Отправляем на почту
        send_email(file_path, file_name)

        # Загружаем в Google Drive
        upload_to_drive(file_path, file_name)

        # Записываем в Google Sheets
        append_to_sheet([file_name])

        await update.message.reply_text("Спасибо, резюме принято и отправлено!")

    except Exception as e:
        logging.error(f"Ошибка обработки документа: {e}")
        await update.message.reply_text("Произошла ошибка при обработке вашего резюме. Попробуйте позже.")

def send_email(file_path, file_name):
    email_user = os.getenv("EMAIL_USER")
    email_passwo_
