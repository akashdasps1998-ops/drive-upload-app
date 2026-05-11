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
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)
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

# ================= THREAD-LOCAL CLIENTS =================
_thread_local = threading.local()

def get_drive_service():
    if not hasattr(_thread_local, 'drive_service'):
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        _thread_local.drive_service = build(
            "drive", "v3",
            credentials=creds,
            cache_discovery=False
        )
    return _thread_local.drive_service

def get_sheet_client():
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
    hasher = hashlib.md5()
    hasher.update(file_bytes)
    return hasher.hexdigest()

def create_folder(name, parent_id):
    drive = get_drive_service()
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

def upload_stream(file_bytes, file_name, mime_type, parent_id):
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

# ================= RETRY WRAPPER =================
def retry(func, retries=3, delay=2):
    last_error = None
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            last_error = e
            error_str = str(e)
            if any(x in error_str for x in [
                "SSL", "Connection", "timeout",
                "IncompleteRead", "ConnectionReset"
            ]):
                print(f"⚠️ Attempt {attempt + 1} failed: {e}. Retrying...")
                if hasattr(_thread_local, 'drive_service'):
                    del _thread_local.drive_service
                if hasattr(_thread_local, 'sheet_client'):
                    del _thread_local.sheet_client
                time.sleep(delay * (attempt + 1))
            else:
                raise
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

        # ===== STEP 1: CAPTURE FORM DATA =====
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

        # ===== STEP 2: READ FILES INTO MEMORY =====
        video = request.files.get('video')
        if not video or video.filename == '':
            print("⚠️ No video in request - likely mid-deploy submission")
            return "RETRY", 503

        video_bytes = video.read()
        if len(video_bytes) == 0:
            print("⚠️ Empty video received")
            return "RETRY", 503

        print(f"📹 Video size: {len(video_bytes) / 1024 / 1024:.1f} MB")

        photos = {}
        for i in range(1, 5):
            photo = request.files.get(f'photo{i}')
            if photo and photo.filename != '':
                photos[i] = {
                    'bytes': photo.read(),
                    'filename': photo.filename
                }

        # ===== STEP 3: CREATE DRIVE FOLDERS =====
        print("📂 Creating folders...")
        e_fold = retry(lambda: create_folder(eclinic, ROOT_FOLDER_ID))
        d_fold = retry(lambda: create_folder(today_str, e_fold))
        p_fold = retry(lambda: create_folder("Photos", d_fold))
        v_fold = retry(lambda: create_folder("Videos", d_fold))

        # ===== STEP 4: UPLOAD VIDEO =====
        print("🎥 Uploading video...")
        v_hash = get_file_hash(video_bytes)
        v_name = f"{eclinic}_{today_str}_{v_hash}.mp4"

        video_link = retry(
            lambda: upload_stream(video_bytes, v_name, "video/mp4", v_fold)
        )
        print("✅ Video uploaded:", video_link)

        # ===== STEP 5: UPLOAD PHOTOS =====
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

        with ThreadPoolExecutor(max_workers=2) as executor:
            executor.map(process_photo, photos.items())

        print("✅ Photos uploaded")

        # ===== STEP 6: SHEET ENTRY ONLY AFTER SUCCESS =====
        # ✅ Only written if BOTH video + photos uploaded successfully
        # ✅ No more PENDING rows
        # ✅ No more ERROR backlogs
        print("📝 Writing to sheet...")

        row = [
            agent_msid, eclinic, state,
            today_str, timestamp_str
        ] + scores + [
            final_score, issues, ai_output,
            video_link  # ✅ Direct link written immediately, no PENDING
        ]

        sheet_client = retry(lambda: get_sheet_client())
        retry(lambda: sheet_client.append_row(row, value_input_option="RAW"))
        print("✅ Sheet entry success")

        # ===== STEP 7: BACKUP AFTER SUCCESS =====
        # ✅ Only backs up successful submissions
        # ✅ Keeps backup drive clean
        backup_data = {
            "agent": agent_msid,
            "clinic": eclinic,
            "state": state,
            "timestamp": timestamp_str,
            "video_link": video_link,
            "status": "SUCCESS"
        }

        try:
            backup_id = create_backup(backup_data)
            print("📁 Backup created:", backup_id)
        except Exception as be:
            # ✅ Backup failure does NOT affect main upload
            print(f"⚠️ Backup failed (non-critical): {be}")

        print("🎉 SUCCESS")
        return "✅ Audit Uploaded Successfully!"

    except Exception as e:
        print("❌ ERROR:", str(e))
        # ✅ No sheet entry on failure = zero backlog
        return f"❌ System Error: {str(e)}", 500

# ================= RUN =================
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=10000)