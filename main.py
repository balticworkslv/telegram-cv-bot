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

# ID таблицы и папки на Google Drive
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Пришли мне своё резюме в формате PDF или DOCX.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        document = update.message.document
        if not document:
            await update.message.reply_text("Пожалуйста, отправьте файл в формате PDF или DOCX.")
            return

        # Проверяем формат файла
        if not (document.file_name.lower().endswith('.pdf') or document.file_name.lower().endswith('.docx')):
            await update.message.reply_text("Неверный формат файла. Отправьте PDF или DOCX.")
            return

        file = await document.get_file()
        file_name = document.file_name
        file_path = f"/tmp/{file_name}"

        await file.download_to_drive(file_path)
        logging.info(f"Файл {file_name} скачан во временную папку.")

        # Отправка на email
        send_email(file_path, file_name)

        # Загрузка в Google Drive
        upload_to_drive(file_path, file_name)

        # Запись в Google Sheets
        append_to_sheet([file_name])

        await update.message.reply_text("Спасибо, резюме принято и отправлено!")

    except Exception as e:
        logging.error(f"Ошибка обработки документа: {e}")
        await update.message.reply_text("Произошла ошибка при обработке вашего резюме. Попробуйте позже.")

def send_email(file_path, file_name):
    email_user = os.getenv("EMAIL_USER")
    email_password = os.getenv("EMAIL_PASSWORD")

    if not email_user or not email_password or not HR_EMAIL:
        logging.error("Не заданы переменные окружения EMAIL_USER, EMAIL_PASSWORD или HR_EMAIL")
        return

    msg = EmailMessage()
    msg['Subject'] = 'Новое резюме'
    msg['From'] = email_user
    msg['To'] = HR_EMAIL
    msg.set_content('Прикреплено новое резюме от кандидата.')

    # Читаем файл и прикрепляем
    with open(file_path, 'rb') as f:
        file_data = f.read()
    # Определяем mime type по расширению
    if file_name.lower().endswith('.pdf'):
        maintype, subtype = 'application', 'pdf'
    else:
        maintype, subtype = 'application', 'vnd.openxmlformats-officedocument.wordprocessingml.document'

    msg.add_attachment(file_data, maintype=maintype, subtype=subtype, filename=file_name)

    # Отправка письма через SMTP (пример Gmail SMTP)
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(email_user, email_password)
            smtp.send_message(msg)
        logging.info("Письмо с резюме отправлено на email.")
    except Exception as e:
        logging.error(f"Ошибка отправки email: {e}")

def upload_to_drive(file_path, file_name):
    try:
        file_metadata = {
            'name': file_name,
            'parents': [DRIVE_FOLDER_ID]
        }
        media = MediaFileUpload(file_path, resumable=True)
        drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        logging.info(f"Файл {file_name} загружен в Google Drive.")
    except Exception as e:
        logging.error(f"Ошибка загрузки в Google Drive: {e}")

def append_to_sheet(row_values):
    try:
        body = {
            'values': [row_values]
        }
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Лист1!A1',  # замените 'Лист1' на имя вашего листа
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body=body
        ).execute()
        logging.info("Данные добавлены в Google Sheets.")
    except Exception as e:
        logging.error(f"Ошибка добавления данных в Google Sheets: {e}")

def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    application.run_polling()

if __name__ == "__main__":
    main()
