"""
Telegram CV Bot — BalticWorks (EN)

What it does:
- Menu buttons: Send CV, View vacancies, Contact HR
- Collects candidate info (name/email/phone/position/source) and a file/photo
- Uploads file to Google Drive
  • Chooses target folder by matching text (position/source/filename) against
    patterns in Google Sheets → Categories sheet (FolderID column)
- Appends candidate row to Google Sheets → Leads sheet
- Emails HR via SMTP (optional)
- Captures new messages from your vacancies Telegram topic and writes them to
  Google Sheets → Vacancies sheet
- /whereami prints chat_id and topic_id to configure env

PTB: python-telegram-bot==21.x
"""

import logging
import os
import re
import tempfile
import mimetypes
from pathlib import Path
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone
from typing import Optional
import time

from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

from googleapiclient.discovery import build  # type: ignore
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from google.oauth2.service_account import Credentials as SACredentials

# ========= Load .env =========
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)

# ========= .env variables =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Drive / Sheets
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()  # fallback parent
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
SHEET_TAB = os.getenv("GOOGLE_SHEETS_TAB", "Leads")
CATEGORIES_SHEET_TAB = os.getenv("CATEGORIES_SHEET_TAB", "Categories")

# Vacancies (display) & source
VACANCIES_MODE = os.getenv("VACANCIES_MODE", "sheet").lower()  # "url" or "sheet"
VACANCIES_SHEET_ID = os.getenv("VACANCIES_SHEET_ID", SPREADSHEET_ID)
VACANCIES_SHEET_TAB = os.getenv("VACANCIES_SHEET_TAB", "Vacancies")

def _int_env(name: str, default: int = 0) -> int:
    val = os.getenv(name, "")
    try:
        return int(val)
    except (TypeError, ValueError):
        logging.warning("%s is not numeric (%r). Feature disabled.", name, val)
        return default

VACANCIES_CHAT_ID = _int_env("VACANCIES_CHAT_ID", 0)   # supergroup id (e.g. -1001234567890)
VACANCIES_TOPIC_ID = _int_env("VACANCIES_TOPIC_ID", 0) # message_thread_id (topic)

# Auth mode for Drive/Sheets
AUTH_MODE = os.getenv("DRIVE_AUTH_MODE", "user_oauth").lower()  # "user_oauth" or "service_account"

# Email (SMTP)
MAIL_TO = os.getenv("MAIL_TO")
MAIL_FROM = os.getenv("MAIL_FROM", os.getenv("SMTP_USER"))
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
ATTACH_FILE_TO_EMAIL = os.getenv("ATTACH_FILE_TO_EMAIL", "true").lower() == "true"

# HR & site
VACANCIES_URL = os.getenv("VACANCIES_URL", "").strip()
HR_EMAIL = os.getenv("HR_EMAIL", MAIL_TO or "").strip()
HR_TELEGRAM = os.getenv("HR_TELEGRAM", "").strip()

# ========= Debug prints =========
print(">>> starting main.py")
print(">>> TELEGRAM_TOKEN present:", bool(TELEGRAM_TOKEN))
print(">>> AUTH_MODE:", AUTH_MODE)

# ========= Logging =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ========= Temp dir =========
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "tg_cv_bot"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

def _safe_name(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '_', (name or 'file')).strip('_') or 'file'

# ========= Google APIs init =========
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive']
SHEETS_SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
ALL_SCOPES = list(set(DRIVE_SCOPES + SHEETS_SCOPES))

if AUTH_MODE == "user_oauth":
    from google.oauth2.credentials import Credentials as UserCredentials
    from google.auth.transport.requests import Request
    token_path = Path("token.json")
    if not token_path.exists():
        raise RuntimeError("token.json not found. Run oauth_bootstrap.py first.")
    user_creds = UserCredentials.from_authorized_user_file(str(token_path))
    if not user_creds.valid:
        if user_creds.expired and user_creds.refresh_token:
            user_creds.refresh(Request())
        else:
            raise RuntimeError("Invalid OAuth token.json; re-run oauth_bootstrap.py")
    drive_service = build('drive', 'v3', credentials=user_creds)
    sheets_service = build('sheets', 'v4', credentials=user_creds)
else:
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise RuntimeError(f"Service account JSON not found at {SERVICE_ACCOUNT_FILE}")
    sa_creds = SACredentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=ALL_SCOPES)
    drive_service = build('drive', 'v3', credentials=sa_creds)
    sheets_service = build('sheets', 'v4', credentials=sa_creds)

try:
    who = drive_service.about().get(fields="user(emailAddress,displayName)").execute()
    print(f">>> Drive as: {who['user']['emailAddress']} ({who['user']['displayName']})")
except Exception as e:
    print(">>> Could not fetch Drive user:", e)
print(">>> Folder ID:", repr(GOOGLE_DRIVE_FOLDER_ID))

# ========= Conversation states =========
(NAME, EMAIL, PHONE, POSITION, SOURCE, WAITING_FILE) = range(6)

# ========= Keyboards =========
BTN_SEND = "Send CV"
BTN_VAC = "View vacancies"
BTN_CONTACT = "Contact HR"

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_SEND)],
         [KeyboardButton(BTN_VAC), KeyboardButton(BTN_CONTACT)]],
        resize_keyboard=True
    )

# ========= Topic link builder (no username needed) =========
def _topic_link_from_ids(chat_id: int, topic_id: int) -> Optional[str]:
    if not (chat_id and topic_id):
        return None
    cid = str(abs(int(chat_id)))
    if cid.startswith("100"):  # supergroups
        cid = cid[3:]
    return f"https://t.me/c/{cid}/{int(topic_id)}"

# ========= Sheets helpers =========
def _append_to_sheet(values: list):
    if not SPREADSHEET_ID:
        logger.warning("SPREADSHEET_ID not set; skipping Google Sheets append.")
        return
    body = {'values': [values]}
    res = sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body
    ).execute()
    logger.info("Sheets append OK → %s", res.get("updates", {}).get("updatedRange"))

def _append_vacancy_row(title: str, url: str, location: str, department: str,
                        chat_id: int, topic_id: int, message_id: int):
    if not VACANCIES_SHEET_ID:
        return
    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')
    body = {"values": [[now_iso, title, url, location, department,
                        str(chat_id), str(topic_id), str(message_id)]]}
    sheets_service.spreadsheets().values().append(
        spreadsheetId=VACANCIES_SHEET_ID,
        range=f"{VACANCIES_SHEET_TAB}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body
    ).execute()

def _fetch_vacancies_from_sheet(max_items: int = 10):
    if not VACANCIES_SHEET_ID:
        return []
    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=VACANCIES_SHEET_ID,
        range=f"{VACANCIES_SHEET_TAB}!A1:H"
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return []
    headers = [h.strip().lower() for h in rows[0]]
    data = rows[1:]

    def idx(name): return headers.index(name) if name in headers else None

    i_title = idx("title"); i_url = idx("url")
    i_loc = idx("location"); i_dep = idx("department")
    out = []
    for r in data:
        title = r[i_title] if (i_title is not None and i_title < len(r)) else ""
        url = r[i_url] if (i_url is not None and i_url < len(r)) else ""
        loc = r[i_loc] if (i_loc is not None and i_loc < len(r)) else ""
        dep = r[i_dep] if (i_dep is not None and i_dep < len(r)) else ""
        if title:
            out.append({"title": title, "url": url, "location": loc, "department": dep})
        if len(out) >= max_items:
            break
    return out

# ========= Categories loader (regex + folder mapping) =========
CATEGORIES_CACHE = {"rows": [], "ts": 0.0}

def _load_categories_from_sheet(force: bool = False):
    """Read Categories sheet: A:Category, B:Keywords, C:FolderID, D:Pattern (optional).
       Build ordered list of {'category','folder','regex','pattern'} with row order.
    """
    try:
        now = time.time()
        if CATEGORIES_CACHE["rows"] and not force and (now - CATEGORIES_CACHE["ts"] < 300):
            return CATEGORIES_CACHE["rows"]

        res = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{CATEGORIES_SHEET_TAB}!A1:D"
        ).execute()
        values = res.get("values", [])
        if not values:
            CATEGORIES_CACHE["rows"] = []
            CATEGORIES_CACHE["ts"] = now
            return []

        headers = [h.strip().lower() for h in values[0]]
        data = values[1:]

        def col(name):
            return headers.index(name) if name in headers else None

        i_cat = col("category")
        i_kw  = col("keywords")
        i_fid = col("folderid")
        i_pat = col("pattern")

        rows = []
        for r in data:
            cat = r[i_cat].strip() if (i_cat is not None and i_cat < len(r)) else ""
            if not cat:
                continue
            folder = (r[i_fid].strip() if (i_fid is not None and i_fid < len(r)) else "") or ""
            pattern = (r[i_pat].strip() if (i_pat is not None and i_pat < len(r)) else "")
            if not pattern:
                kw = r[i_kw].strip() if (i_kw is not None and i_kw < len(r)) else ""
                if kw:
                    toks = [t.strip() for t in kw.split(",") if t.strip()]
                    if toks:
                        pattern = r"(?i)\b(" + "|".join(toks) + r")\b"
            rx = None
            if pattern:
                try:
                    rx = re.compile(pattern)
                except re.error:
                    rx = None  # ignore invalid pattern
            rows.append({"category": cat, "folder": folder, "regex": rx, "pattern": pattern})

        CATEGORIES_CACHE["rows"] = rows
        CATEGORIES_CACHE["ts"] = now
        return rows
    except Exception as e:
        logger.exception("Failed to load Categories from sheet: %s", e)
        return []

def _select_folder_for_text(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return (folder_id, category_name) by first matching regex; else (None, None)."""
    rows = _load_categories_from_sheet()
    if not rows:
        return None, None
    t = text or ""
    for row in rows:
        rx = row.get("regex")
        if rx and rx.search(t):
            return (row.get("folder") or None), row.get("category")
    return None, None

# ========= Drive + Email helpers =========
def _upload_to_drive(local_path: str, filename: str, mime: Optional[str] = None, parent_id: Optional[str] = None) -> tuple[str, str]:
    media = MediaFileUpload(local_path, mimetype=mime, resumable=True)
    body = {'name': filename}
    pid = parent_id or GOOGLE_DRIVE_FOLDER_ID
    if pid:
        body['parents'] = [pid]
    created = drive_service.files().create(
        body=body, media_body=media,
        fields='id, webViewLink',
        supportsAllDrives=True
    ).execute()
    return created.get('id'), created.get('webViewLink')

def _send_email(subject: str, body: str, attachment_path: Optional[str] = None, attachment_name: Optional[str] = None):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and MAIL_TO):
        logger.warning("SMTP/MAIL env not configured; skipping email send.")
        return
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = MAIL_FROM or SMTP_USER
    msg['To'] = MAIL_TO
    msg.set_content(body)
    if attachment_path and ATTACH_FILE_TO_EMAIL:
        ctype, encoding = mimetypes.guess_type(attachment_path)
        if ctype is None or encoding is not None:
            ctype = 'application/octet-stream'
        maintype, subtype = ctype.split('/', 1)
        with open(attachment_path, 'rb') as fp:
            msg.add_attachment(fp.read(), maintype=maintype, subtype=subtype,
                               filename=attachment_name or os.path.basename(attachment_path))
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
                s.login(SMTP_USER, SMTP_PASSWORD); s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.ehlo(); s.starttls(); s.ehlo()
                s.login(SMTP_USER, SMTP_PASSWORD); s.send_message(msg)
    except Exception as e:
        logger.exception("SMTP send failed: %s", e)

# ========= Vacancy parsing from Telegram post =========
URL_RE = re.compile(r'(https?://\S+)')
def _parse_vacancy_from_text(text: str):
    t = text or ""
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    title = lines[0] if lines else ""
    m = URL_RE.search(t)
    url = m.group(1) if m else ""
    return title, url, "", ""  # title, url, location, department

# ========= Bot Handlers =========
THANK_YOU_TEXT = (
    "Thank you, your CV has been received.\n"
    "Our HR team will contact you if your profile matches our vacancies"
)

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Choose an option:", reply_markup=main_menu_kb())

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! This is the BalticWorks bot for CV submissions.\nUse the menu below to continue.",
        reply_markup=main_menu_kb()
    )

async def whereami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    await update.message.reply_text(
        f"chat_id = {chat.id}\nmessage_thread_id (topic_id) = {getattr(msg,'message_thread_id', None)}"
    )

# capture vacancies from your topic (write into Vacancies sheet)
async def capture_vacancy_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (VACANCIES_CHAT_ID and VACANCIES_TOPIC_ID):
        return
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.id != VACANCIES_CHAT_ID:
        return
    if getattr(msg, "message_thread_id", None) != VACANCIES_TOPIC_ID:
        return
    text = msg.text or msg.caption
    if not text:
        return
    title, url, loc, dep = _parse_vacancy_from_text(text)
    if not title:
        return
    try:
        _append_vacancy_row(title, url, loc, dep, chat.id, msg.message_thread_id, msg.message_id)
    except Exception as e:
        logger.exception("Failed to append vacancy to sheet: %s", e)

# View vacancies -> open your topic link (t.me/c/...), no username
async def view_vacancies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic_url = _topic_link_from_ids(VACANCIES_CHAT_ID, VACANCIES_TOPIC_ID)
    if topic_url:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open vacancies topic", url=topic_url)]])
        await update.message.reply_text(
            "Our vacancies are posted in our Telegram topic:",
            reply_markup=kb
        )
        return

    # Fallback to Sheet list (if topic IDs not configured)
    if VACANCIES_MODE == "sheet":
        try:
            items = _fetch_vacancies_from_sheet(max_items=10)
        except Exception as e:
            logger.exception("Vacancies fetch failed: %s", e)
            items = []
        if not items:
            if VACANCIES_URL:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("More on site", url=VACANCIES_URL)]])
                await update.message.reply_text("No open vacancies right now.", reply_markup=kb)
            else:
                await update.message.reply_text("No open vacancies right now.", reply_markup=main_menu_kb())
            return
        rows = []
        for it in items:
            label = it["title"]
            if it.get("location"):
                label += f" — {it['location']}"
            rows.append([InlineKeyboardButton(label[:64], url=it["url"] or VACANCIES_URL or "https://t.me/")])
        if VACANCIES_URL:
            rows.append([InlineKeyboardButton("More on site", url=VACANCIES_URL)])
        await update.message.reply_text("Open roles:", reply_markup=InlineKeyboardMarkup(rows))
        return

    # Final fallback — external URL
    if VACANCIES_URL:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open vacancies", url=VACANCIES_URL)]])
        await update.message.reply_text("Here are our current vacancies:", reply_markup=kb)
    else:
        await update.message.reply_text(
            "Vacancies page is not configured yet.\nPlease contact HR for the latest openings.",
            reply_markup=main_menu_kb()
        )

async def contact_hr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["You can reach our HR team here:"]
    if HR_EMAIL:
        lines.append(f"• Email: {HR_EMAIL}")
    if HR_TELEGRAM:
        handle = HR_TELEGRAM if HR_TELEGRAM.startswith("@") else f"@{HR_TELEGRAM}"
        lines.append(f"• Telegram: {handle}")
    if not (HR_EMAIL or HR_TELEGRAM):
        lines.append("• Email: info@balticworks.lv")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu_kb())

# === CV flow ===
async def apply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("What is your full name?")
    return NAME

async def ask_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = (update.message.text or '').strip()
    await update.message.reply_text("Your email address?")
    return EMAIL

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['email'] = (update.message.text or '').strip()
    await update.message.reply_text("Your phone number (with country code)?")
    return PHONE

async def ask_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['phone'] = (update.message.text or '').strip()
    await update.message.reply_text("Which position are you applying for?")
    return POSITION

async def ask_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['position'] = (update.message.text or '').strip()
    await update.message.reply_text("Where did you find this vacancy? (LinkedIn/Telegram/Website/Referral/Other)")
    return SOURCE

async def wait_for_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['source'] = (update.message.text or '').strip()
    await update.message.reply_text("Please send your CV file (PDF/DOC/DOCX) or a photo (JPG/PNG).")
    return WAITING_FILE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Okay, cancelled the application.", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def _finalize_and_notify(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               local_path: Path, display_name: str, web_link: str):
    user = update.effective_user
    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')
    name = context.user_data.get('name', user.full_name or '')
    email = context.user_data.get('email', '')
    phone = context.user_data.get('phone', '')
    position = context.user_data.get('position', '')
    source = context.user_data.get('source', '')

    # Log payload (debug)
    logger.info("Appending to Sheets payload: %s",
                [now_iso, name, email, phone, position, source,
                 display_name, web_link,
                 f"t.me/{user.username}" if user.username else str(user.id)])

    # Sheets row (candidates)
    try:
        _append_to_sheet([
            now_iso, name, email, phone, position, source,
            display_name, web_link,
            f"t.me/{user.username}" if user.username else str(user.id),
        ])
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        logger.exception("Sheets append failed: %s", e)
        await update.message.reply_text(f"Sheets error {status}: {e}", reply_markup=main_menu_kb())
    except Exception as e:
        logger.exception("Sheets append failed (generic): %s", e)
        await update.message.reply_text(f"Sheets error: {e}", reply_markup=main_menu_kb())

    # Email to HR
    subject = f"[CV] {name} — {position}" if position else f"[CV] {name or display_name}"
    body = (
        f"New application from Telegram:\n\n"
        f"Name: {name}\nEmail: {email}\nPhone: {phone}\n"
        f"Position: {position}\nSource: {source}\n"
        f"File: {display_name}\nDrive link: {web_link}\n"
        f"User: @{user.username if user.username else user.id}\n"
        f"Time: {now_iso}\n"
    )
    try:
        _send_email(subject, body, str(local_path), display_name)
    except Exception as e:
        logger.exception("Email send failed: %s", e)
    finally:
        try: local_path.unlink(missing_ok=True)
        except Exception as e: logger.warning("Could not delete temp file %s: %s", local_path, e)

async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document:
        await update.message.reply_text("Please send a file.", reply_markup=main_menu_kb())
        return WAITING_FILE

    logger.info("DOC from user=%s | name=%s | mime=%s",
                update.effective_user.id, document.file_name, document.mime_type)

    safe_name = _safe_name(document.file_name)
    tmp_path = DOWNLOAD_ROOT / f"{update.message.message_id}_{safe_name}"
    file_obj = await context.bot.get_file(document.file_id)
    await file_obj.download_to_drive(custom_path=str(tmp_path))
    mime_type = document.mime_type or mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'

    # choose folder by Categories
    match_text = " ".join(filter(None, [
        context.user_data.get('position', ''),
        context.user_data.get('source', ''),
        safe_name
    ]))
    parent_id, cat_name = _select_folder_for_text(match_text)
    logger.info("Category detect: %r -> %r (folder=%r)", match_text, cat_name, parent_id)

    try:
        _, web_link = _upload_to_drive(str(tmp_path), safe_name, mime_type, parent_id=parent_id)
        logger.info("Drive uploaded OK | name=%s | link=%s", safe_name, web_link)
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        logger.exception("Drive upload failed: %s", e)
        await update.message.reply_text(f"Drive error {status}: {e}", reply_markup=main_menu_kb())
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        return ConversationHandler.END
    except Exception as e:
        logger.exception("Drive upload failed (generic): %s", e)
        await update.message.reply_text(f"Drive error: {e}", reply_markup=main_menu_kb())
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        return ConversationHandler.END

    await _finalize_and_notify(update, context, tmp_path, safe_name, web_link)
    await update.message.reply_text(
        "Thank you, your CV has been received.\nOur HR team will contact you if your profile matches our vacancies",
        reply_markup=main_menu_kb()
    )
    return ConversationHandler.END

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    logger.info("PHOTO from user=%s | file_id=%s | size=%s",
                update.effective_user.id, photo.file_id, photo.file_size)

    safe_name = f"photo_{update.message.message_id}.jpg"
    tmp_path = DOWNLOAD_ROOT / safe_name
    file_obj = await context.bot.get_file(photo.file_id)
    await file_obj.download_to_drive(custom_path=str(tmp_path))

    # choose folder by Categories
    match_text = " ".join(filter(None, [
        context.user_data.get('position', ''),
        context.user_data.get('source', ''),
        safe_name
    ]))
    parent_id, cat_name = _select_folder_for_text(match_text)
    logger.info("Category detect (photo): %r -> %r (folder=%r)", match_text, cat_name, parent_id)

    try:
        _, web_link = _upload_to_drive(str(tmp_path), safe_name, 'image/jpeg', parent_id=parent_id)
        logger.info("Drive uploaded OK | name=%s | link=%s", safe_name, web_link)
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        logger.exception("Drive upload failed: %s", e)
        await update.message.reply_text(f"Drive error {status}: {e}", reply_markup=main_menu_kb())
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        return ConversationHandler.END
    except Exception as e:
        logger.exception("Drive upload failed (generic): %s", e)
        await update.message.reply_text(f"Drive error: {e}", reply_markup=main_menu_kb())
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        return ConversationHandler.END

    await _finalize_and_notify(update, context, tmp_path, safe_name, web_link)
    await update.message.reply_text(
        "Thank you, your CV has been received.\nOur HR team will contact you if your profile matches our vacancies",
        reply_markup=main_menu_kb()
    )
    return ConversationHandler.END

# ========= Main =========
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set in environment.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("vacancies", view_vacancies))
    app.add_handler(CommandHandler("contact", contact_hr))
    app.add_handler(CommandHandler("whereami", whereami))

    # Conversation for CV form (Send CV enters the conversation)
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("apply", apply_start),
            MessageHandler(filters.Regex(r'(?i)^send cv$'), apply_start),
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_email)],
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_position)],
            POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_source)],
            SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_for_file)],
            WAITING_FILE: [
                MessageHandler(filters.Document.ALL, receive_document),
                MessageHandler(filters.PHOTO, receive_photo),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Menu buttons (outside conversation)
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^view vacancies$'), view_vacancies))
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^contact hr$'), contact_hr))

    # Capture posts from vacancy topic (in groups)
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & (filters.TEXT | filters.Caption) & ~filters.COMMAND,
        capture_vacancy_post
    ))

    # Also accept docs/photos outside of /apply
    app.add_handler(MessageHandler(filters.Document.ALL, receive_document))
    app.add_handler(MessageHandler(filters.PHOTO, receive_photo))

    print(">>> running polling...")
    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()

