import os
import re
import logging
from io import BytesIO
from email.message import EmailMessage
import aiosmtplib

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# --- Настройки ---
TOKEN = os.getenv("TELEGRAM_TOKEN")

# Google API setup
SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']
SERVICE_ACCOUNT_FILE = 'credentials.json'  # файл с твоими Google API ключами
SPREADSHEET_ID = "ТВОЙ_ID_GOOGLE_SHEETS"
DRIVE_FOLDER_ID = "ТВОЙ_ID_ПАПКИ_В_GOOGLE_DRIVE"

# Email HR
HR_EMAIL = "hr@example.com"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "твой.email@gmail.com"
SMTP_PASS = "пароль_приложения_для_gmail"  # лучше использовать App Password (https://support.google.com/accounts/answer/185833)

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Google сервисы ---
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)

# --- Парсер данных из текста ---
def parse_candidate_info(text):
    # Ищем email
    email_match = re.search(r'[\w\.-]+@[\w\.-]+', text)
    email = email_match.group(0) if email_match else ''

    # Ищем телефон (простой вариант, цифры с +, -, пробелами)
    phone_match = re.search(r'(\+?\d[\d \-\(\)]{7,}\d)', text)
    phone = phone_match.group(0) if phone_match else ''

    # Ищем имя (предположим, первая строка - имя)
    lines = text.splitlines()
    name = lines[0] if lines else ''

    # Позицию — попробуем найти слова "Position:", "Должность:" и взять после них
    position_match = re.search(r'(Position|Должность)[:\-]\s*(.+)', text, re.IGNORECASE)
    position = position_match.group(2).strip() if position_match else ''

    return name, phone, email, position

# --- Функция добавления строки в Google Sheets ---
def append_to_sheet(row):
    body = {'values': [row]}
    result = sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Лист1!A:D",
        valueInputOption="RAW",
        body=body
    ).execute()
    logger.info(f'Добавлена строка в таблицу: {row}')
    return result

# --- Функция загрузки файла в Google Drive ---
def upload_to_drive(file_bytes, filename):
    file_metadata = {
        'name': filename,
        'parents': [DRIVE_FOLDER_ID]
    }
    media = MediaIoBaseUpload(BytesIO(file_bytes), mimetype='application/octet-stream')
    file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    logger.info(f'Файл загружен в Google Drive: {filename} с ID {file["id"]}')
    return file['id']

# --- Функция отправки email ---
async def send_email(to_email, subject, body, attachment_bytes, attachment_name):
    msg = EmailMessage()
    msg['From'] = SMTP_USER
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.set_content(body)
    msg.add_attachment(attachment_bytes, maintype='application', subtype='octet-stream', filename=attachment_name)

    await aiosmtplib.send(
        msg,
        hostname=SMTP_SERVER,
        port=SMTP_PORT,
        start_tls=True,
        username=SMTP_USER,
        password=SMTP_PASS
    )
    logger.info(f'Email отправлен на {to_email} с файлом {attachment_name}')

# --- Обработчик команды /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Пришли мне резюме в формате PDF, DOC, DOCX или фото — я обработаю его и отправлю HR.")

# --- Обработчик файлов ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = update.message.document or update.message.photo[-1] if update.message.photo else None
    if not file:
        await update.message.reply_text("Пожалуйста, пришлите файл резюме (PDF, DOC, DOCX) или фото.")
        return

    # Скачиваем файл
    file_obj = await file.get_file()
    file_bytes = await file_obj.download_as_bytearray()
    filename = file.file_name if hasattr(file, 'file_name') else "photo.jpg"

    # Для упрощения парсим текст из файла как байты в строку (лучше подключать парсеры pdf/docx, но это чуть сложнее)
    text = ""
    try:
        if filename.lower().endswith(('.pdf')):
            import fitz  # PyMuPDF
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            for page in doc:
                text += page.get_text()
        elif filename.lower().endswith(('.docx')):
            import docx
            from io import BytesIO
            doc = docx.Document(BytesIO(file_bytes))
            text = "\n".join([p.text for p in doc.paragraphs])
        elif filename.lower().endswith(('.doc')):
            # Можно использовать textract, но нужна отдельная установка и больше зависимостей
            text = "DOC parsing not supported yet."
        else:
            # Для фото пытаемся распознать текст с помощью pytesseract
            from PIL import Image
            import pytesseract
            image = Image.open(BytesIO(file_bytes))
            text = pytesseract.image_to_string(image)
    except Exception as e:
        logger.error(f"Ошибка при парсинге файла: {e}")

    if not text.strip():
        await update.message.reply_text("Не удалось распознать текст из файла. Попробуйте другой формат.")
        return

    # Парсим данные кандидата
    name, phone, email, position = parse_candidate_info(text)

    # Добавляем в Google Sheet
    append_to_sheet([name, phone, email, position])

    # Загружаем файл в Google Drive
    upload_to_drive(file_bytes, filename)

    # Отправляем email HR
    email_text = f"Новое резюме:\nИмя: {name}\nТелефон: {phone}\nEmail: {email}\nПозиция: {position}"
    await send_email(HR_EMAIL, f"Резюме от {name}", email_text, file_bytes, filename)

    await update.message.reply_text(f"Резюме от {name} получено и обработано!")

# --- Главная функция ---
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_document))
    app.run_polling()

if __name__ == "__main__":
    main()
