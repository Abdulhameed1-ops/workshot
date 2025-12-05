from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import os
from functools import wraps
import requests
from datetime import datetime
import logging
import sqlite3
import PyPDF2
import docx
import openpyxl
from PIL import Image
import io
import base64
import re
import json

app = Flask(__name__, template_folder='style')
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SESSION_COOKIE_SECURE'] = True  # HTTPS only in production
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('static', exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# API Keys - MUST use environment variables in production

OCR_API_KEY = os.environ.get("OCR_API_KEY", "")
COHERE_API_KEY = os.environ.get("COHERE_API_KEY", "")

if not OCR_API_KEY or not COHERE_API_KEY:
    logger.warning("API keys not set! Set OCR_API_KEY and COHERE_API_KEY environment variables")

OCR_URL = "https://api.ocr.space/parse/image"
COHERE_URL = "https://api.cohere.ai/v1/chat"
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin@workshot.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")

GUEST_CHAT_LIMIT = 5

# ===============================
# DATABASE FUNCTIONS
# ===============================
def init_db():
    conn = sqlite3.connect('workshot.db')
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        full_name TEXT NOT NULL,
        password TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_login TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT NOT NULL,
        message TEXT NOT NULL,
        response TEXT NOT NULL,
        files TEXT,
        timestamp TEXT NOT NULL
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS conversation_context (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT NOT NULL,
        session_id TEXT NOT NULL,
        context_data TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT,
        action TEXT NOT NULL,
        details TEXT,
        timestamp TEXT NOT NULL
    )''')
    
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect('workshot.db')
    conn.row_factory = sqlite3.Row
    return conn

def log_activity(user_email, action, details=""):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO activity_logs (user_email, action, details, timestamp) VALUES (?, ?, ?, ?)",
                  (user_email, action, details, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging activity: {str(e)}")

def get_conversation_context(user_email, session_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT context_data FROM conversation_context 
                     WHERE user_email = ? AND session_id = ? 
                     ORDER BY updated_at DESC LIMIT 1""", (user_email, session_id))
        result = c.fetchone()
        conn.close()
        
        if result:
            return json.loads(result['context_data'])
        return []
    except Exception as e:
        logger.error(f"Error getting context: {str(e)}")
        return []

def update_conversation_context(user_email, session_id, context_data):
    try:
        conn = get_db()
        c = conn.cursor()
        
        if len(context_data) > 10:
            context_data = context_data[-10:]
        
        c.execute("""INSERT OR REPLACE INTO conversation_context 
                     (user_email, session_id, context_data, updated_at) 
                     VALUES (?, ?, ?, ?)""",
                  (user_email, session_id, json.dumps(context_data), datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error updating context: {str(e)}")

init_db()

# ===============================
# AUTH DECORATORS
# ===============================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session and "guest_mode" not in session:
            return redirect(url_for("landing"))
        return f(*args, **kwargs)
    return decorated_function

# ===============================
# TEXT FORMATTING FUNCTIONS
# ===============================
def format_ai_response(text):
    text = re.sub(r'\*\*\*+', '', text)
    text = re.sub(r'####+', '', text)
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'^\s*[\*\-\+]\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*(\d+)\.\s+', r'\1. ', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    return text

# ===============================
# FILE EXTRACTION FUNCTIONS
# ===============================
def extract_text_from_pdf(file):
    try:
        pdf_reader = PyPDF2.PdfReader(file)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text() + "\n"
        return text.strip(), None
    except Exception as e:
        return None, f"PDF Error: {str(e)}"

def extract_text_from_docx(file):
    try:
        doc = docx.Document(file)
        text = "\n".join([paragraph.text for paragraph in doc.paragraphs])
        return text.strip(), None
    except Exception as e:
        return None, f"DOCX Error: {str(e)}"

def extract_text_from_excel(file):
    try:
        workbook = openpyxl.load_workbook(file)
        text = ""
        for sheet in workbook.worksheets:
            text += f"\n[Sheet: {sheet.title}]\n"
            for row in sheet.iter_rows(values_only=True):
                row_text = " | ".join([str(cell) if cell is not None else "" for cell in row])
                if row_text.strip():
                    text += row_text + "\n"
        return text.strip(), None
    except Exception as e:
        return None, f"Excel Error: {str(e)}"

def extract_text_from_image(image_file):
    try:
        if not OCR_API_KEY:
            return None, "OCR API key not configured"
            
        image_file.seek(0)
        files = {"file": (image_file.filename, image_file.read())}
        
        payload = {
            "apikey": OCR_API_KEY,
            "language": "eng",
            "isOverlayRequired": False,
            "detectOrientation": True,
            "scale": True,
            "OCREngine": 2
        }
        
        res = requests.post(OCR_URL, data=payload, files=files, timeout=30)

        if res.status_code != 200:
            return None, f"OCR Service Error: {res.status_code}"

        data = res.json()
        if "ParsedResults" not in data or not data["ParsedResults"]:
            return None, "No text detected in image"

        text = data["ParsedResults"][0].get("ParsedText", "")
        if not text.strip():
            return None, "No readable text found in image"

        return text.strip(), None
    except Exception as e:
        logger.error(f"OCR Exception: {str(e)}")
        return None, f"OCR Error: {str(e)}"

def process_file(file):
    filename = file.filename.lower()
    
    if filename.endswith('.pdf'):
        return extract_text_from_pdf(file)
    elif filename.endswith('.docx') or filename.endswith('.doc'):
        return extract_text_from_docx(file)
    elif filename.endswith(('.xlsx', '.xls')):
        return extract_text_from_excel(file)
    elif filename.endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff')):
        return extract_text_from_image(file)
    else:
        return None, "Unsupported file type"

# ===============================
# AI FUNCTIONS WITH CONTEXT
# ===============================
def cohere_chat_with_context(text, user_prompt, conversation_history):
    try:
        if not COHERE_API_KEY:
            return None, "AI API key not configured"
            
        headers = {
            "Authorization": f"Bearer {COHERE_API_KEY}",
            "Content-Type": "application/json"
        }

        context_messages = ""
        if conversation_history:
            for msg in conversation_history[-5:]:
                context_messages += f"\nUser: {msg['user']}\nAssistant: {msg['assistant']}\n"

        system_message = (
            "You are Workshot AI, a friendly and intelligent assistant app that lets users capture their work and instantly turn it into clean, clear, AI-enhanced visuals. "
            "user will just snap a photo, and workshot automatically improves it -removing distractions, adjusting lighting, highliting important details, and organizing everything into a neat, professional-looking result."
            "You maintain context from previous messages in the conversation. "
            "When users refer to previous topics, acknowledge and build upon them. "
            "You can play games, have conversations, and help with any questions. "
            "Provide clear, engaging responses with proper formatting. "
            "Use bullet points (•) and numbering naturally. "
            "Be conversational and remember what was discussed before."
            "Workshot is perfect for: students showing assignments, Designing capturingvprogress, Technicians documenting tsks, creator sharing work in a clean format."
            "When solving anything you give clear and clean answers and readable solutions like maths when it is about maths you will giv e readable calcuations solutons explanagtions and answers"
           
        )

        full_prompt = f"{system_message}\n\nPrevious conversation:\n{context_messages}\n\nCurrent content:\n{text}\n\nUser: {user_prompt}"

        payload = {
            "model": "command-a-03-2025",
            "message": full_prompt,
            "temperature": 0.8,
            "max_tokens": 2500
        }

        res = requests.post(COHERE_URL, headers=headers, json=payload, timeout=60)
        
        if res.status_code != 200:
            error_detail = res.text
            logger.error(f"Cohere API Error: {error_detail}")
            return None, f"AI Service Error ({res.status_code})"

        data = res.json()
        reply = data.get("text", "")

        if not reply:
            return None, "AI returned no response"

        reply = format_ai_response(reply)
        return reply, None
        
    except requests.exceptions.Timeout:
        return None, "Request timeout - please try again"
    except Exception as e:
        logger.error(f"Cohere Exception: {str(e)}")
        return None, f"AI Error: {str(e)}"

# ===============================
# ROUTES
# ===============================
@app.route("/")
def index():
    return redirect(url_for("landing"))

@app.route("/landing")
def landing():
    return render_template('LANDING_HTML.html')

@app.route("/guest")
def guest():
    session["guest_mode"] = True
    session["guest_chat_count"] = 0
    session["session_id"] = datetime.now().isoformat()
    log_activity("guest", "guest_session", "Guest started session")
    return redirect(url_for("chat"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        try:
            data = request.get_json()
            email = data.get("email", "").strip().lower()
            password = data.get("password", "")

            if not email or not password:
                return jsonify({"success": False, "error": "Email and password required"})

            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE email = ?", (email,))
            user = c.fetchone()
            conn.close()

            if user and check_password_hash(user["password"], password):
                session.pop("guest_mode", None)
                session.pop("guest_chat_count", None)
                session["user_id"] = email
                session["user_name"] = user["full_name"]
                
                conn = get_db()
                c = conn.cursor()
                c.execute("UPDATE users SET last_login = ? WHERE email = ?", 
                         (datetime.now().isoformat(), email))
                conn.commit()
                conn.close()
                
                log_activity(email, "login", "User logged in")
                return jsonify({"success": True, "redirect": url_for("chat")})
            
            return jsonify({"success": False, "error": "Invalid email or password"})
        except Exception as e:
            logger.error(f"Login error: {str(e)}")
            return jsonify({"success": False, "error": "An error occurred"})

    return render_template('LOGIN_HTML.html')

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        try:
            data = request.get_json()
            email = data.get("email", "").strip().lower()
            full_name = data.get("full_name", "").strip()
            password = data.get("password", "")
            confirm_password = data.get("confirm_password", "")

            if not email or not full_name or not password:
                return jsonify({"success": False, "error": "All fields required"})

            if password != confirm_password:
                return jsonify({"success": False, "error": "Passwords don't match"})

            if len(password) < 6:
                return jsonify({"success": False, "error": "Password must be 6+ characters"})

            conn = get_db()
            c = conn.cursor()
            
            c.execute("SELECT email FROM users WHERE email = ?", (email,))
            if c.fetchone():
                conn.close()
                return jsonify({"success": False, "error": "Email already registered"})

            hashed_password = generate_password_hash(password)
            c.execute("INSERT INTO users (email, full_name, password, created_at) VALUES (?, ?, ?, ?)",
                     (email, full_name, hashed_password, datetime.now().isoformat()))
            conn.commit()
            conn.close()
            
            log_activity(email, "signup", "New user registered")
            return jsonify({"success": True, "redirect": url_for("login")})
            
        except Exception as e:
            logger.error(f"Signup error: {str(e)}")
            return jsonify({"success": False, "error": "Error creating account"})

    return render_template('SIGNUP_HTML.html')

@app.route("/chat")
@login_required
def chat():
    user_name = session.get("user_name", "Guest")
    is_guest = "guest_mode" in session
    return render_template('DASHBOARD_HTML.html', user_name=user_name, is_guest=is_guest)

@app.route("/send_message", methods=["POST"])
@login_required
def send_message():
    try:
        is_guest = "guest_mode" in session
        
        if is_guest:
            guest_count = session.get("guest_chat_count", 0)
            if guest_count >= GUEST_CHAT_LIMIT:
                return jsonify({"error": "Guest limit reached. Please sign up to continue."}), 403
            session["guest_chat_count"] = guest_count + 1
        
        user_email = session.get("user_id", "guest")
        session_id = session.get("session_id", datetime.now().isoformat())
        user_message = request.form.get("message", "").strip()
        files = request.files.getlist("files")

        if not user_message and not files:
            return jsonify({"error": "Please provide a message or upload files"})

        conversation_history = get_conversation_context(user_email, session_id)

        extracted_text = ""
        file_names = []
        
        if files:
            for file in files:
                if file and file.filename:
                    filename = secure_filename(file.filename)
                    file_names.append(filename)
                    
                    text, err = process_file(file)
                    if err:
                        logger.warning(f"File processing error: {err}")
                        continue
                    
                    if text:
                        extracted_text += f"\n[File: {filename}]\n{text}\n"

        if not extracted_text and user_message:
            extracted_text = "No file content available."

        ai_response, err = cohere_chat_with_context(extracted_text, user_message, conversation_history)
        
        if err:
            logger.error(f"AI Error: {err}")
            return jsonify({"error": err})

        conversation_history.append({
            "user": user_message,
            "assistant": ai_response,
            "files": file_names
        })
        update_conversation_context(user_email, session_id, conversation_history)

        if not is_guest:
            conn = get_db()
            c = conn.cursor()
            c.execute("""INSERT INTO chat_history (user_email, message, response, files, timestamp) 
                         VALUES (?, ?, ?, ?, ?)""",
                     (user_email, user_message, ai_response, ", ".join(file_names), datetime.now().isoformat()))
            conn.commit()
            conn.close()
            
            log_activity(user_email, "send_message", f"Processed {len(file_names)} files")
        
        return jsonify({
            "success": True,
            "response": ai_response,
            "files_processed": len(file_names),
            "is_guest": is_guest,
            "remaining_chats": GUEST_CHAT_LIMIT - session.get("guest_chat_count", 0) if is_guest else None
        })
    
    except Exception as e:
        logger.error(f"Error in send_message: {str(e)}")
        return jsonify({"error": "Server error. Please try again."}), 500

@app.route("/clear_context", methods=["POST"])
@login_required
def clear_context():
    try:
        session["session_id"] = datetime.now().isoformat()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/get_history")
@login_required
def get_history():
    try:
        if "guest_mode" in session:
            return jsonify({"history": []})
        
        user_email = session.get("user_id")
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT id, message, response, files, timestamp 
                     FROM chat_history 
                     WHERE user_email = ? 
                     ORDER BY timestamp DESC LIMIT 50""", (user_email,))
        history = c.fetchall()
        conn.close()
        
        return jsonify({
            "history": [dict(h) for h in history]
        })
    except Exception as e:
        logger.error(f"Error getting history: {str(e)}")
        return jsonify({"error": "Could not load history"}), 500

@app.route("/delete_history/<int:history_id>", methods=["DELETE"])
@login_required
def delete_history(history_id):
    try:
        if "guest_mode" in session:
            return jsonify({"error": "Guests cannot delete history"}), 403
        
        user_email = session.get("user_id")
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM chat_history WHERE id = ? AND user_email = ?", (history_id, user_email))
        conn.commit()
        conn.close()
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/logout")
def logout():
    user_email = session.get("user_id", "guest")
    log_activity(user_email, "logout", "User logged out")
    session.clear()
    return redirect(url_for("landing"))

@app.errorhandler(404)
def not_found(error):
    return redirect(url_for("landing")), 404

@app.errorhandler(500)
def server_error(error):
    logger.error(f"Server error: {error}")
    return jsonify({"error": "Internal server error"}), 500

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "File too large. Max 50MB"}), 413

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_ENV") == "development"
    
    print("=" * 60)
    print(" Workshot - AI Learning Assistant")
    print("=" * 60)
    print(f"\nServer starting on port {port}...")
    print(f"Debug mode: {debug_mode}")
    print("\nSupported Files: Images, PDF, DOCX, XLSX")
    print("Features: Contextual AI, Voice Lectures, File Analysis")
    print("\nPress CTRL+C to stop")
    print("=" * 60)
    
    app.run(host="0.0.0.0", port=port)