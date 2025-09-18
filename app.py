import os
import io
import threading
import sqlite3
import datetime
import json
from flask import Flask, render_template, request, jsonify, send_file, abort
from model import train_model_background, extract_embedding_for_image, MODEL_PATH

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "attendance.db")
DATASET_DIR = os.path.join(APP_DIR, "dataset")
os.makedirs(DATASET_DIR, exist_ok=True)

TRAIN_STATUS_FILE = os.path.join(APP_DIR, "train_status.json")

app = Flask(__name__, static_folder="static", template_folder="templates")

# ---------- DB helpers ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    roll TEXT,
                    class TEXT,
                    section TEXT,
                    reg_no TEXT,
                    created_at TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER,
                    name TEXT,
                    timestamp TEXT
                )""")
    conn.commit()
    conn.close()

init_db()

# ---------- Train status helpers ----------
def write_train_status(status_dict):
    with open(TRAIN_STATUS_FILE, "w") as f:
        json.dump(status_dict, f)

def read_train_status():
    if not os.path.exists(TRAIN_STATUS_FILE):
        return {"running": False, "progress": 0, "message": "Not trained"}
    with open(TRAIN_STATUS_FILE, "r") as f:
        return json.load(f)

# ensure initial train status file exists
write_train_status({"running": False, "progress": 0, "message": "No training yet."})

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html")

# Dashboard simple API for attendance stats (last 30 days)
@app.route("/attendance_stats")
def attendance_stats():
    import pandas as pd
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT timestamp FROM attendance", conn)
    conn.close()
    if df.empty:
        from datetime import date, timedelta
        days = [(date.today() - datetime.timedelta(days=i)).strftime("%d-%b") for i in range(29, -1, -1)]
        return jsonify({"dates": days, "counts": [0]*30})
    df['date'] = pd.to_datetime(df['timestamp']).dt.date
    last_30 = [ (datetime.date.today() - datetime.timedelta(days=i)) for i in range(29, -1, -1) ]
    counts = [ int(df[df['date'] == d].shape[0]) for d in last_30 ]
    dates = [ d.strftime("%d-%b") for d in last_30 ]
    return jsonify({"dates": dates, "counts": counts})

# -------- Add student (form) --------
@app.route("/add_student", methods=["GET", "POST"])
def add_student():
    if request.method == "GET":
        return render_template("add_student.html")
    # POST: save student metadata and return student_id
    data = request.form
    name = data.get("name","").strip()
    roll = data.get("roll","").strip()
    cls = data.get("class","").strip()
    sec = data.get("sec","").strip()
    reg_no = data.get("reg_no","").strip()
    if not name:
        return jsonify({"error":"name required"}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.datetime.utcnow().isoformat()
    c.execute("INSERT INTO students (name, roll, class, section, reg_no, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (name, roll, cls, sec, reg_no, now))
    sid = c.lastrowid
    conn.commit()
    conn.close()
    # create dataset folder for this student
    os.makedirs(os.path.join(DATASET_DIR, str(sid)), exist_ok=True)
    return jsonify({"student_id": sid})

# -------- Upload face images (after capture) --------
@app.route("/upload_face", methods=["POST"])
def upload_face():
    student_id = request.form.get("student_id")
    if not student_id:
        return jsonify({"error":"student_id required"}), 400
    files = request.files.getlist("images[]")
    saved = 0
    folder = os.path.join(DATASET_DIR, student_id)
    if not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    for f in files:
        try:
            fname = f"{datetime.datetime.utcnow().timestamp():.6f}_{saved}.jpg"
            path = os.path.join(folder, fname)
            f.save(path)
            saved += 1
        except Exception as e:
            app.logger.error("save error: %s", e)
    return jsonify({"saved": saved})

# -------- Train model (start background thread) --------
@app.route("/train_model", methods=["GET"])
def train_model_route():
    # if already running, respond accordingly
    status = read_train_status()
    if status.get("running"):
        return jsonify({"status":"already_running"}), 202
    # reset status
    write_train_status({"running": True, "progress": 0, "message": "Starting training"})
    # start background thread
    t = threading.Thread(target=train_model_background, args=(DATASET_DIR, lambda p,m: write_train_status({"running": True, "progress": p, "message": m})))
    t.daemon = True
    t.start()
    return jsonify({"status":"started"}), 202

# -------- Train progress (polling) --------
@app.route("/train_status", methods=["GET"])
def train_status():
    return jsonify(read_train_status())

# -------- Mark attendance page --------
@app.route("/mark_attendance", methods=["GET"])
def mark_attendance_page():
    return render_template("mark_attendance.html")

# -------- Recognize face endpoint (POST image) --------
@app.route("/recognize_face", methods=["POST"])
def recognize_face():
    if "image" not in request.files:
        return jsonify({"recognized": False, "error":"no image"}), 400
    img_file = request.files["image"]
    try:
        emb = extract_embedding_for_image(img_file.stream)
        if emb is None:
            return jsonify({"recognized": False, "error":"no face detected"}), 200
        # attempt prediction
        from model import load_model_if_exists, predict_with_model
        clf = load_model_if_exists()
        if clf is None:
            return jsonify({"recognized": False, "error":"model not trained"}), 200
        pred_label, conf = predict_with_model(clf, emb)
        # threshold confidence
        if conf < 0.5:
            return jsonify({"recognized": False, "confidence": float(conf)}), 200
        # find student name
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT name FROM students WHERE id=?", (int(pred_label),))
        row = c.fetchone()
        name = row[0] if row else "Unknown"
        # save attendance record with timestamp
        ts = datetime.datetime.utcnow().isoformat()
        c.execute("INSERT INTO attendance (student_id, name, timestamp) VALUES (?, ?, ?)", (int(pred_label), name, ts))
        conn.commit()
        conn.close()
        return jsonify({"recognized": True, "student_id": int(pred_label), "name": name, "confidence": float(conf)}), 200
    except Exception as e:
        app.logger.exception("recognize error")
        return jsonify({"recognized": False, "error": str(e)}), 500

# -------- Attendance records & filters --------
@app.route("/attendance_record", methods=["GET"])
def attendance_record():
    period = request.args.get("period", "all")  # all, daily, weekly, monthly
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    q = "SELECT id, student_id, name, timestamp FROM attendance"
    params = ()
    if period == "daily":
        today = datetime.date.today().isoformat()
        q += " WHERE date(timestamp) = ?"
        params = (today,)
    elif period == "weekly":
        start = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        q += " WHERE date(timestamp) >= ?"
        params = (start,)
    elif period == "monthly":
        start = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        q += " WHERE date(timestamp) >= ?"
        params = (start,)
    q += " ORDER BY timestamp DESC LIMIT 5000"
    c.execute(q, params)
    rows = c.fetchall()
    conn.close()
    return render_template("attendance_record.html", records=rows, period=period)

# -------- CSV download --------
@app.route("/download_csv", methods=["GET"])
def download_csv():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, student_id, name, timestamp FROM attendance ORDER BY timestamp DESC")
    rows = c.fetchall()
    conn.close()
    output = io.StringIO()
    output.write("id,student_id,name,timestamp\n")
    for r in rows:
        output.write(f'{r[0]},{r[1]},{r[2]},{r[3]}\n')
    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name="attendance.csv", mimetype="text/csv")

# -------- Students API for listing/editing --------
@app.route("/students", methods=["GET"])
def students_list():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, roll, class, section, reg_no, created_at FROM students ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    data = [ {"id":r[0],"name":r[1],"roll":r[2],"class":r[3],"section":r[4],"reg_no":r[5],"created_at":r[6]} for r in rows ]
    return jsonify({"students": data})

@app.route("/students/<int:sid>", methods=["DELETE"])
def delete_student(sid):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM students WHERE id=?", (sid,))
    c.execute("DELETE FROM attendance WHERE student_id=?", (sid,))
    conn.commit()
    conn.close()
    # also delete dataset folder
    folder = os.path.join(DATASET_DIR, str(sid))
    if os.path.isdir(folder):
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    return jsonify({"deleted": True})

# ---------------- run ------------------------
if __name__ == "__main__":
    app.run(debug=True)