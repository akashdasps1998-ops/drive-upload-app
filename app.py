from flask import Flask, render_template, request
import os
from datetime import datetime
import hashlib
import json


# Google Libraries
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ================= GOOGLE AUTH =================
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]
creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS"))
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
client = gspread.authorize(creds)

# ✅ SETTINGS (From Old Code)
sheet = client.open_by_key("1wyxj7NDoPgbHtiXTYwvmflXS1tgn49e5uivtd_4Y8A4").sheet1
drive_service = build("drive", "v3", credentials=creds)
ROOT_FOLDER_ID = "1uGfbVLbokVyUxHH5W66ULb1BvzOjzzKH"

# ================= UTILS (Old Logic) =================
def get_file_hash(file):
    hasher = hashlib.md5()
    file.seek(0)
    while chunk := file.read(4096):
        hasher.update(chunk)
    file.seek(0)
    return hasher.hexdigest()

def create_folder(name, parent_id):
    query = f"name='{name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = drive_service.files().list(
        q=query, fields="files(id, name)", supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    folder_metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    folder = drive_service.files().create(
        body=folder_metadata, fields="id", supportsAllDrives=True
    ).execute()
    return folder.get("id")

def upload_file(file_path, file_name, mime_type, parent_id):
    file_metadata = {"name": file_name, "parents": [parent_id]}
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True, chunksize=1024*1024)
    file = drive_service.files().create(
        body=file_metadata, media_body=media, fields="id", supportsAllDrives=True
    ).execute()
    return f"https://drive.google.com/file/d/{file.get('id')}/view"

# ================= ROUTES =================
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/guideline') # From New Code
def guideline():
    return render_template('guideline.html')

@app.route('/upload', methods=['POST'])
def upload():
    try:
        def get_v(key):
            val = request.form.get(key, "").strip()
            return val if val else "-"

        # 1. Capture Data
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
        ai_output = get_v("aiOutput")

        today_str = datetime.now().strftime("%d-%m-%Y")
        timestamp_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

        # 2. Handle Folders
        e_fold = create_folder(eclinic, ROOT_FOLDER_ID)
        d_fold = create_folder(today_str, e_fold)
        p_fold = create_folder("Photos", d_fold)
        v_fold = create_folder("Videos", d_fold)

        # 3. Handle Video (with Hash logic from Old Code)
        video = request.files.get('video')
        if not video: return "❌ Video File Missing"
        v_hash = get_file_hash(video)
        v_name = f"{eclinic}_{today_str}_{v_hash}.mp4"
        v_path = os.path.join(UPLOAD_FOLDER, v_name)
        video.save(v_path)
        video_link = upload_file(v_path, v_name, "video/mp4", v_fold)
        os.remove(v_path)   # Cleanup Video

        # 4. Handle Photos
        for i in range(1, 5):
            photo = request.files.get(f'photo{i}')
            if not photo: return f"❌ Photo {i} Missing"
            p_name = f"{eclinic}_{state}_P{i}_{photo.filename}"
            p_path = os.path.join(UPLOAD_FOLDER, p_name)
            photo.save(p_path)
            upload_file(p_path, p_name, "image/jpeg", p_fold)
            os.remove(p_path) # Cleanup

        # 5. Save to Sheet
        row = [agent_msid, eclinic, state, today_str, timestamp_str] + scores + [final_score, issues, ai_output, video_link]
        sheet.append_row(row, value_input_option="RAW")
        
       
        return "✅ Audit Uploaded Successfully!"
    except Exception as e:
        return f"❌ System Error: {str(e)}"

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=10000)