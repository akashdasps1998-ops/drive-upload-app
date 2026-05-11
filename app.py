from flask import Flask, render_template, request
import os
from datetime import datetime
import hashlib
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

# Google Libraries
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload  # ← KEY CHANGE: stream instead of file

app = Flask(__name__)

# ↓ Increased but keep reasonable for Render free tier
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024

# ================= GOOGLE AUTH =================
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]

creds_json = os.environ.get("GOOGLE_CREDENTIALS")
if not creds_json:
    raise Exception("❌ GOOGLE_CREDENTIALS not set")

creds_dict = json.loads(creds_json)

# -----------------------------------------------
# FIX #1: Thread-local credentials
# Each thread gets its own Google API client
# This fixes ALL the SSL errors you're seeing
# -----------------------------------------------
_thread_local = threading.local()

def get_drive_service():
    """Get a thread-local Drive service instance."""
    if not hasattr(_thread_local, 'drive_service'):
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        _thread_local.drive_service = build(
            "drive", "v3",
            credentials=creds,
            cache_discovery=False  # Avoids file cache issues on Render
        )
    return _thread_local.drive_service

def get_sheet_client():
    """Get a thread-local Sheets client."""
    if not hasattr(_thread_local, 'sheet_client'):
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        gc = gspread.authorize(creds)
        _thread_local.sheet_client = gc.open_by_key(
            "1wyxj7NDoPgbHtiXTYwvmflXS1tgn49e5uivtd_4Y8A4"
        ).sheet1
    return _thread_local.sheet_client

# ================= SETTINGS =================
ROOT_FOLDER_ID = "1uGfbVLbokVyUxHH5W66ULb1BvzOjzzKH"
BACKUP_FOLDER_ID = "0AM63WZlfiwbsUk9PVA"

# ================= UTILS =================
def get_file_hash(file_bytes):
    """Hash from bytes, not file object."""
    hasher = hashlib.md5()
    hasher.update(file_bytes)
    return hasher.hexdigest()

def create_folder(name, parent_id):
    drive = get_drive_service()
    # Sanitize folder name to avoid Drive API issues
    safe_name = name.replace("'", "\\'")
    query = (
        f"name='{safe_name}' and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )

    results = drive.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()

    files = results.get("files", [])
    if files:
        return files[0]["id"]

    folder = drive.files().create(
        body={
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]
        },
        fields="id",
        supportsAllDrives=True
    ).execute()

    return folder.get("id")

# -----------------------------------------------
# FIX #2: Upload from memory (BytesIO), NOT disk
# This eliminates SIGSEGV crashes from disk I/O
# and fixes "file not found" after restarts
# -----------------------------------------------
def upload_stream(file_bytes, file_name, mime_type, parent_id):
    """Upload directly from memory buffer."""
    drive = get_drive_service()
    stream = BytesIO(file_bytes)

    media = MediaIoBaseUpload(
        stream,
        mimetype=mime_type,
        resumable=True,
        chunksize=5 * 1024 * 1024
    )

    file = drive.files().create(
        body={"name": file_name, "parents": [parent_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True
    ).execute()

    return f"https://drive.google.com/file/d/{file.get('id')}/view"

# ================= BACKUP SYSTEM =================
def create_backup(data):
    """Backup stored in memory, uploaded to Drive directly."""
    drive = get_drive_service()
    file_name = f"backup_{int(time.time())}.json"
    json_bytes = json.dumps(data).encode("utf-8")
    stream = BytesIO(json_bytes)

    media = MediaIoBaseUpload(stream, mimetype="application/json")

    file = drive.files().create(
        body={"name": file_name, "parents": [BACKUP_FOLDER_ID]},
        media_body=media,
        fields="id",
        supportsAllDrives=True
    ).execute()

    return file.get("id")

def update_backup_status(file_id, status):
    drive = get_drive_service()
    try:
        drive.files().update(
            fileId=file_id,
            body={"description": f"STATUS: {status}"},
            supportsAllDrives=True
        ).execute()
    except Exception as e:
        print(f"⚠️ Backup status update failed: {e}")

# ================= RETRY WRAPPER =================
# FIX #3: Retry logic for transient SSL errors
def retry(func, retries=3, delay=2):
    """Retry a function on SSL/connection errors."""
    last_error = None
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            last_error = e
            error_str = str(e)
            # Only retry on known transient errors
            if any(x in error_str for x in [
                "SSL", "Connection", "timeout",
                "IncompleteRead", "ConnectionReset"
            ]):
                print(f"⚠️ Attempt {attempt + 1} failed: {e}. Retrying...")
                # Clear thread-local clients so they get recreated fresh
                if hasattr(_thread_local, 'drive_service'):
                    del _thread_local.drive_service
                if hasattr(_thread_local, 'sheet_client'):
                    del _thread_local.sheet_client
                time.sleep(delay * (attempt + 1))
            else:
                raise  # Non-retryable error
    raise last_error

# ================= ROUTES =================
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/guideline')
def guideline():
    return render_template('guideline.html')

@app.route('/upload', methods=['POST'])
def upload():
    try:
        def get_v(key):
            val = request.form.get(key, "").strip()
            return val if val else "-"

        # ===== Capture Data =====
        agent_msid = get_v("agentMsid")
        eclinic = get_v("eclinicCode").upper()
        state = get_v("state")

        scores = [
            get_v("cleanliness"), get_v("cleanliness_comment"),
            get_v("board"), get_v("board_comment"),
            get_v("poster"), get_v("poster_comment"),
            get_v("furniture"), get_v("furniture_comment"),
            get_v("equipment"), get_v("equipment_comment")
        ]

        final_score = get_v("finalScore")
        issues = get_v("issues")
        ai_output = get_v("aiOutput")[:40000]

        today_str = datetime.now().strftime("%d-%m-%Y")
        timestamp_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

        # -----------------------------------------------
        # FIX #4: Read ALL files into memory FIRST
        # before doing anything else.
        # Flask request context closes after response,
        # so we capture bytes immediately.
        # -----------------------------------------------
        video = request.files.get('video')
        if not video:
            return "❌ Video Missing", 400

        video_bytes = video.read()  # Read into memory
        print(f"📹 Video size: {len(video_bytes) / 1024 / 1024:.1f} MB")

        photos = {}
        for i in range(1, 5):
            photo = request.files.get(f'photo{i}')
            if photo:
                photos[i] = {
                    'bytes': photo.read(),
                    'filename': photo.filename
                }

        # ===== BACKUP =====
        backup_data = {
            "agent": agent_msid,
            "clinic": eclinic,
            "state": state,
            "timestamp": timestamp_str,
            "scores": scores,
            "final_score": final_score,
            "issues": issues,
            "ai_output": ai_output[:1000]  # Keep backup small
        }

        backup_id = retry(lambda: create_backup(backup_data))
        print("📁 Backup created:", backup_id)

        # ===== SHEET =====
        row = [
            agent_msid, eclinic, state,
            today_str, timestamp_str
        ] + scores + [
            final_score, issues, ai_output, "PENDING"
        ]

        sheet_client = retry(lambda: get_sheet_client())
        retry(lambda: sheet_client.append_row(row, value_input_option="RAW"))
        print("✅ Sheet entry success")

        row_number = retry(lambda: len(sheet_client.col_values(1)))
        print("📍 Row:", row_number)

        # ===== DRIVE FOLDERS =====
        e_fold = retry(lambda: create_folder(eclinic, ROOT_FOLDER_ID))
        d_fold = retry(lambda: create_folder(today_str, e_fold))
        p_fold = retry(lambda: create_folder("Photos", d_fold))
        v_fold = retry(lambda: create_folder("Videos", d_fold))

        # ===== VIDEO UPLOAD (from memory) =====
        print("🎥 Uploading video...")
        v_hash = get_file_hash(video_bytes)
        v_name = f"{eclinic}_{today_str}_{v_hash}.mp4"

        video_link = retry(
            lambda: upload_stream(video_bytes, v_name, "video/mp4", v_fold)
        )
        print("✅ Video uploaded:", video_link)

        # ===== PHOTOS (from memory, parallel) =====
        print("🖼️ Uploading photos...")

        def process_photo(item):
            i, photo_data = item
            p_name = f"{eclinic}_{state}_P{i}_{photo_data['filename']}"
            retry(lambda: upload_stream(
                photo_data['bytes'],
                p_name,
                "image/jpeg",
                p_fold
            ))

        # Limit workers to avoid memory pressure on free tier
        with ThreadPoolExecutor(max_workers=2) as executor:
            executor.map(process_photo, photos.items())

        print("✅ Photos uploaded")

        # ===== UPDATE SHEET =====
        retry(lambda: sheet_client.update_cell(row_number, len(row), video_link))
        print("🔄 Sheet updated with Drive link")

        # ===== FINAL =====
        update_backup_status(backup_id, "SUCCESS")
        print("🎉 SUCCESS")

        return "✅ Audit Uploaded Successfully!"

    except Exception as e:
        print("❌ ERROR:", str(e))
        return f"❌ System Error: {str(e)}", 500

# ================= RUN =================
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=10000)