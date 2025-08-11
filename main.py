import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
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
SERVICE_ACCOUNT_FILE = 'credentials.json'

import os
import json
from google.oauth2.service_account import Credentials

creds_info = json.loads(os.environ['GOOGLE_CREDENTIALS_JSON'])
creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)

# ID таблицы и папки на Google Drive (создай заранее)
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Пришли мне своё резюме в формате PDF или DOCX.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    file_name = update.message.document.file_name
    file_path = f"/tmp/{file_name}"
    await file.download_to_drive(file_path)

    # Отправляем на почту
    send_email(file_path, file_name)

    # Загружаем в Google Drive
    upload_to_drive(file_path, file_name)

    # Пишем в Google Sheets
    # Здесь можно добавить парсинг имени и тд. (упрощённо оставим только имя файла)
    append_to_sheet([file_name])

    await update.message.reply_text("Спасибо, резюме принято и отправлено!")

def send_email(file_path, file_name):
    email_user = os.getenv("EMAIL_USER")
    email_password = os.getenv("EMAIL_PASSWORD")

    msg = EmailMessage()
    msg['Subject'] = 'Новое резюме'
    msg['From'] = email_user
    msg['To'] = HR_EMAIL
    msg.set_content('Пришло новое резюме в приложении.')

    with open(file_path, 'rb') as f:
        file_data = f.read()
        msg.add_attachment(file_data, maintype='application', subtype='octet-stream', filename=file_name)

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(email_user, email_password)
        smtp.send_message(msg)

def upload_to_drive(file_path, file_name):
    file_metadata = {
        'name': file_name,
        'parents': [DRIVE_FOLDER_ID]
    }
    media = MediaFileUpload(file_path, resumable=True)
    drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()

def append_to_sheet(values):
    body = {
        'values': [values]
    }
    sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range='A1',
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body=body
    ).execute()

if __name__ == '__main__':
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()
