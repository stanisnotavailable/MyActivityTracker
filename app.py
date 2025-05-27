import os
from flask import Flask, redirect, request, session, url_for, render_template, flash, make_response, jsonify
from requests_oauthlib import OAuth2Session
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
import requests
from datetime import datetime, timedelta
import json
import uuid
import hashlib
import sqlite3
import re
import logging
from contextlib import contextmanager

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24))
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Env vars
CID = os.getenv("STRAVA_CLIENT_ID")
CSEC = os.getenv("STRAVA_CLIENT_SECRET")
REDIR = os.getenv("STRAVA_REDIRECT_URI")
SCOPES = ["activity:read_all"]
# Cookie settings
COOKIE_NAME = "strava_session"
COOKIE_MAX_AGE = 30 * 24 * 60 * 60  # 30 days in seconds

# Database setup
DB_PATH = os.getenv("DB_PATH", "activity_tracker.db")

# Get service account email from credentials file
def get_service_account_email():
    try:
        creds_file = os.getenv("GOOGLE_CREDS_FILE")
        if not creds_file:
            return "Service account email not available (GOOGLE_CREDS_FILE not set)"
        
        with open(creds_file, 'r') as f:
            creds_data = json.load(f)
            return creds_data.get('client_email', 'Service account email not found in credentials file')
    except Exception as e:
        logger.error(f"Error reading service account email: {str(e)}")
        return "Error reading service account email"

# Store service account email globally to avoid repeated file reads
SERVICE_ACCOUNT_EMAIL = get_service_account_email()

@contextmanager
def get_db_connection():
    """Context manager for database connections"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """Initialize the database with required tables"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Create sessions table to store user tokens
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            token_data TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # Create spreadsheets table to store spreadsheet configurations
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS spreadsheets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sheet_id TEXT NOT NULL,
            is_default INTEGER DEFAULT 0,
            include_date INTEGER DEFAULT 1,
            include_distance INTEGER DEFAULT 1,
            include_time INTEGER DEFAULT 1,
            include_pace INTEGER DEFAULT 1,
            include_hr INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # Create header_mappings table to store field-to-header mappings
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS header_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spreadsheet_id INTEGER NOT NULL,
            worksheet_name TEXT NOT NULL,
            field_name TEXT NOT NULL,
            header_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (spreadsheet_id) REFERENCES spreadsheets(id) ON DELETE CASCADE,
            UNIQUE(spreadsheet_id, worksheet_name, field_name)
        )
        ''')
        
        # Check if default spreadsheet exists, if not add it from env
        cursor.execute("SELECT COUNT(*) FROM spreadsheets WHERE is_default = 1")
        if cursor.fetchone()[0] == 0:
            default_sheet_name = os.getenv("SHEET_NAME")
            default_sheet_id = os.getenv("SHEET_ID", "")
            if default_sheet_name:
                cursor.execute(
                    "INSERT INTO spreadsheets (name, sheet_id, is_default) VALUES (?, ?, 1)",
                    (default_sheet_name, default_sheet_id)
                )
        
        conn.commit()

def migrate_db():
    """Perform database migrations for schema changes"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Check if columns exist in the spreadsheets table
        try:
            cursor.execute("PRAGMA table_info(spreadsheets)")
            columns = [column[1] for column in cursor.fetchall()]
            
            # Add missing columns if they don't exist
            if "include_date" not in columns:
                cursor.execute("ALTER TABLE spreadsheets ADD COLUMN include_date INTEGER DEFAULT 1")
            if "include_distance" not in columns:
                cursor.execute("ALTER TABLE spreadsheets ADD COLUMN include_distance INTEGER DEFAULT 1")
            if "include_time" not in columns:
                cursor.execute("ALTER TABLE spreadsheets ADD COLUMN include_time INTEGER DEFAULT 1")
            if "include_pace" not in columns:
                cursor.execute("ALTER TABLE spreadsheets ADD COLUMN include_pace INTEGER DEFAULT 1")
            if "include_hr" not in columns:
                cursor.execute("ALTER TABLE spreadsheets ADD COLUMN include_hr INTEGER DEFAULT 1")
            
            # Check if header_mappings table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='header_mappings'")
            if not cursor.fetchone():
                cursor.execute('''
                CREATE TABLE header_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    spreadsheet_id INTEGER NOT NULL,
                    worksheet_name TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    header_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (spreadsheet_id) REFERENCES spreadsheets(id) ON DELETE CASCADE,
                    UNIQUE(spreadsheet_id, worksheet_name, field_name)
                )
                ''')
            
            conn.commit()
            logger.info("Database migration completed successfully")
        except Exception as e:
            logger.error(f"Error during database migration: {str(e)}")

# Initialize database on startup
init_db()
# Run migrations
migrate_db()

# Function to get all spreadsheets
def get_spreadsheets():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id, name, sheet_id, is_default, include_date, include_distance, include_time, include_pace, include_hr FROM spreadsheets ORDER BY is_default DESC, name")
            sheets = [dict(row) for row in cursor.fetchall()]
            
            # Ensure sheet_id is always a string (not None)
            for sheet in sheets:
                if sheet["sheet_id"] is None:
                    sheet["sheet_id"] = ""
                    
            return sheets
        except sqlite3.OperationalError as e:
            # If columns don't exist yet, fall back to basic query
            if "no such column" in str(e):
                logger.warning("Column missing in spreadsheets table, using fallback query")
                cursor.execute("SELECT id, name, sheet_id, is_default FROM spreadsheets ORDER BY is_default DESC, name")
                sheets = []
                for row in cursor.fetchall():
                    sheet_dict = dict(row)
                    # Add default values for missing columns
                    sheet_dict["include_date"] = 1
                    sheet_dict["include_distance"] = 1
                    sheet_dict["include_time"] = 1
                    sheet_dict["include_pace"] = 1
                    sheet_dict["include_hr"] = 1
                    
                    # Ensure sheet_id is always a string (not None)
                    if sheet_dict["sheet_id"] is None:
                        sheet_dict["sheet_id"] = ""
                        
                    sheets.append(sheet_dict)
                return sheets
            else:
                raise

# Function to get default spreadsheet
def get_default_spreadsheet():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id, name, sheet_id, include_date, include_distance, include_time, include_pace, include_hr FROM spreadsheets WHERE is_default = 1 LIMIT 1")
            result = cursor.fetchone()
            if result:
                sheet_dict = dict(result)
                # Ensure sheet_id is always a string (not None)
                if sheet_dict["sheet_id"] is None:
                    sheet_dict["sheet_id"] = ""
                return sheet_dict
            return None
        except sqlite3.OperationalError as e:
            # If columns don't exist yet, fall back to basic query
            if "no such column" in str(e):
                logger.warning("Column missing in spreadsheets table, using fallback query")
                cursor.execute("SELECT id, name, sheet_id FROM spreadsheets WHERE is_default = 1 LIMIT 1")
                result = cursor.fetchone()
                if result:
                    sheet_dict = dict(result)
                    # Add default values for missing columns
                    sheet_dict["include_date"] = 1
                    sheet_dict["include_distance"] = 1
                    sheet_dict["include_time"] = 1
                    sheet_dict["include_pace"] = 1
                    sheet_dict["include_hr"] = 1
                    # Ensure sheet_id is always a string (not None)
                    if sheet_dict["sheet_id"] is None:
                        sheet_dict["sheet_id"] = ""
                    return sheet_dict
                return None
            else:
                raise

# Function to get a spreadsheet by ID
def get_spreadsheet(spreadsheet_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id, name, sheet_id, is_default, include_date, include_distance, include_time, include_pace, include_hr FROM spreadsheets WHERE id = ?", (spreadsheet_id,))
            result = cursor.fetchone()
            if result:
                sheet_dict = dict(result)
                # Ensure sheet_id is always a string (not None)
                if sheet_dict["sheet_id"] is None:
                    sheet_dict["sheet_id"] = ""
                return sheet_dict
            return None
        except sqlite3.OperationalError as e:
            # If columns don't exist yet, fall back to basic query
            if "no such column" in str(e):
                logger.warning("Column missing in spreadsheets table, using fallback query")
                cursor.execute("SELECT id, name, sheet_id, is_default FROM spreadsheets WHERE id = ?", (spreadsheet_id,))
                result = cursor.fetchone()
                if result:
                    sheet_dict = dict(result)
                    # Add default values for missing columns
                    sheet_dict["include_date"] = 1
                    sheet_dict["include_distance"] = 1
                    sheet_dict["include_time"] = 1
                    sheet_dict["include_pace"] = 1
                    sheet_dict["include_hr"] = 1
                    # Ensure sheet_id is always a string (not None)
                    if sheet_dict["sheet_id"] is None:
                        sheet_dict["sheet_id"] = ""
                    return sheet_dict
                return None
            else:
                raise

def get_strava_session(token=None, state=None):
    return OAuth2Session(
        CID, redirect_uri=REDIR, scope=SCOPES, state=state, token=token
    )


def is_token_expired(token):
    if not token:
        return True
    # Check if current time is past the expiration time
    now = datetime.now()
    expires_at = datetime.fromtimestamp(token.get("expires_at", 0))
    return now >= expires_at


def refresh_token(token):
    refresh_url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": CID,
        "client_secret": CSEC,
        "grant_type": "refresh_token",
        "refresh_token": token.get("refresh_token"),
    }

    response = requests.post(refresh_url, data=payload)
    if response.status_code != 200:
        return None

    new_token = response.json()
    return new_token


def generate_session_id():
    """Generate a secure random session ID"""
    return hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest()


def get_token_from_session_id(session_id):
    """Retrieve token data from database using session ID"""
    if not session_id:
        return None
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT token_data FROM sessions WHERE session_id = ?", (session_id,))
        result = cursor.fetchone()
        
        if result:
            return json.loads(result['token_data'])
        return None


def store_token_with_session_id(session_id, token_data):
    """Store token data with session ID in the database"""
    if not session_id or not token_data:
        return
    
    token_json = json.dumps(token_data)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Use REPLACE to handle both inserts and updates
        cursor.execute(
            "REPLACE INTO sessions (session_id, token_data) VALUES (?, ?)",
            (session_id, token_json)
        )
        conn.commit()


def delete_session(session_id):
    """Delete a session from the database"""
    if not session_id:
        return
        
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()


@app.route("/")
def home():
    # Try to get token from session first
    token = session.get("token")
    
    # If not in session, try to get from cookie session ID
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if token:
        # Check if token is expired and refresh if needed
        if is_token_expired(token):
            new_token = refresh_token(token)
            if new_token:
                token = new_token
                session["token"] = token
                
                # Update token in database if we have a session ID
                if COOKIE_NAME in request.cookies:
                    session_id = request.cookies.get(COOKIE_NAME)
                    store_token_with_session_id(session_id, token)
                
                return render_template("index.html", authenticated=True)
            else:
                # If refresh fails, clear the token and cookie
                session.pop("token", None)
                resp = make_response(render_template("index.html", authenticated=False))
                resp.delete_cookie(COOKIE_NAME)
                return resp
        
        return render_template("index.html", authenticated=True)
    
    return render_template("index.html", authenticated=False)


@app.route("/login")
def login():
    strava = get_strava_session()
    auth_url, state = strava.authorization_url("https://www.strava.com/oauth/authorize")
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        flash("Error: No authorization code received")
        return redirect(url_for("home"))

    # Exchange the code for a token directly using requests
    token_url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": CID,
        "client_secret": CSEC,
        "code": code,
        "grant_type": "authorization_code",
    }

    response = requests.post(token_url, data=payload)
    if response.status_code != 200:
        flash(f"Error: Failed to get token. Please try again.")
        return redirect(url_for("home"))

    token = response.json()
    session["token"] = token
    session.permanent = True
    
    # Generate a session ID and store token
    session_id = generate_session_id()
    store_token_with_session_id(session_id, token)
    
    # Set the session ID in a cookie
    resp = make_response(redirect(url_for("home")))
    resp.set_cookie(
        COOKIE_NAME,
        session_id,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=request.is_secure,
        samesite='Lax'
    )
    
    flash("Successfully connected to Strava!")
    return resp


@app.route("/preview_activities", methods=["POST"])
def preview_activities():
    token = session.get("token")
    
    # Try to get from cookie session ID if not in session
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if not token:
        flash("Please connect your Strava account first")
        return redirect(url_for("home"))

    # Check if token is expired and refresh if needed
    if is_token_expired(token):
        new_token = refresh_token(token)
        if not new_token:
            flash("Your authentication has expired. Please login again.")
            return redirect(url_for("login"))
        token = new_token
        session["token"] = token
        session.permanent = True
        
        # Update token in database if we have a session ID
        if COOKIE_NAME in request.cookies:
            session_id = request.cookies.get(COOKIE_NAME)
            store_token_with_session_id(session_id, token)

    # Process form data
    before = request.form.get("before")
    after = request.form.get("after")
    page = request.form.get("page", "1")
    per_page = request.form.get("per_page", "30")

    # Build query parameters
    params = {"page": page, "per_page": per_page}

    if before:
        params["before"] = before

    if after:
        params["after"] = after

    # Use the access_token from the token dictionary
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers=headers,
        params=params,
    )

    if resp.status_code != 200:
        flash(f"Error accessing Strava API: {resp.status_code}")
        return redirect(url_for("home"))

    acts = resp.json()
    
    if not acts:
        flash("No activities found with the specified criteria")
        return redirect(url_for("import_activities"))

    # Process and format activities for display
    formatted_activities = []
    for a in acts:
        # Format date (dd/mm/yyyy)
        date_obj = datetime.strptime(a["start_date"], "%Y-%m-%dT%H:%M:%SZ")
        formatted_date = date_obj.strftime("%d/%m/%Y")
        
        # Format distance (xx,yy km)
        distance_km_numeric = round(a["distance"] / 1000, 2)
        distance_km = str(distance_km_numeric).replace('.', ',')
        
        # Format duration (hh:mm:ss)
        duration_seconds = a["moving_time"]
        hours, remainder = divmod(duration_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        # Calculate pace (min/km)
        if distance_km_numeric > 0:
            pace_seconds = duration_seconds / distance_km_numeric
            pace_minutes, pace_remainder_seconds = divmod(pace_seconds, 60)
            pace = f"{int(pace_minutes):02d}:{int(pace_remainder_seconds):02d}".replace('.', ',')
        else:
            pace = "00:00"
            
        # Get HR (if available)
        avg_hr_raw = a.get("average_heartrate", "")
        avg_hr = round(float(avg_hr_raw)) if avg_hr_raw != "" else ""
        
        formatted_activities.append({
            "date": formatted_date,
            "distance": distance_km,
            "duration": duration,
            "pace": pace,
            "heart_rate": avg_hr,
            "name": a.get("name", "Activity"),
            "type": a.get("type", "")
        })

    # Store the formatted activities in the session for later use
    session["preview_activities"] = formatted_activities
    session["import_params"] = {
        "before": before,
        "after": after,
        "page": page,
        "per_page": per_page
    }
    
    # Get all spreadsheets for selection
    all_spreadsheets = get_spreadsheets()
    
    # Get selected spreadsheet ID from form if available
    selected_spreadsheet_id = request.form.get("spreadsheet_id")
    selected_spreadsheet = None
    
    if selected_spreadsheet_id:
        # Find the selected spreadsheet in the list
        for sheet in all_spreadsheets:
            if str(sheet["id"]) == selected_spreadsheet_id:
                selected_spreadsheet = sheet
                break
    
    # If no spreadsheet is selected, use the default one
    if not selected_spreadsheet:
        for sheet in all_spreadsheets:
            if sheet.get("is_default"):
                selected_spreadsheet = sheet
                break
    
    # If still no spreadsheet, use the first one
    if not selected_spreadsheet and all_spreadsheets:
        selected_spreadsheet = all_spreadsheets[0]
    
    # Ensure sheet_id is included in the selected spreadsheet
    if selected_spreadsheet and not selected_spreadsheet.get("sheet_id"):
        logger.warning(f"Selected spreadsheet {selected_spreadsheet.get('name')} has no sheet_id")
        print(f"DEBUG: Selected spreadsheet {selected_spreadsheet.get('name')} has no sheet_id")
    else:
        logger.info(f"Selected spreadsheet: {selected_spreadsheet.get('name')}, sheet_id: {selected_spreadsheet.get('sheet_id')}")
        print(f"DEBUG: Selected spreadsheet: {selected_spreadsheet.get('name')}, sheet_id: {selected_spreadsheet.get('sheet_id')}")
    
    # Get worksheet names for the selected spreadsheet
    worksheet_names = []
    selected_worksheet = request.form.get("worksheet_name", "Sheet1")
    if selected_spreadsheet and selected_spreadsheet.get("sheet_id"):
        try:
            logger.info(f"Getting worksheets for selected spreadsheet: {selected_spreadsheet.get('name')} (ID: {selected_spreadsheet.get('sheet_id')})")
            worksheet_names = get_worksheet_names(selected_spreadsheet.get("sheet_id"))
        except Exception as e:
            logger.error(f"Error getting worksheet names: {str(e)}")
            worksheet_names = ["Sheet1"]
    
    # Get saved field mappings from session (if coming back from a failed import)
    saved_field_mappings = session.get("saved_field_mappings")
    saved_field_mappings_json = json.dumps(saved_field_mappings) if saved_field_mappings else "null"
    
    return render_template(
        "preview_activities.html", 
        activities=formatted_activities, 
        spreadsheets=all_spreadsheets,
        selected_spreadsheet=selected_spreadsheet,
        worksheet_names=worksheet_names,
        selected_worksheet=selected_worksheet,
        saved_field_mappings_json=saved_field_mappings_json
    )


@app.route("/confirm_import", methods=["POST"])
def confirm_import():
    token = session.get("token")
    formatted_activities = session.get("preview_activities")
    
    if not token:
        flash("Please connect your Strava account first")
        return redirect(url_for("home"))
    
    if not formatted_activities:
        flash("No activities to import. Please preview activities first.")
        return redirect(url_for("import_activities"))
    
    # Get the selected spreadsheet ID from form
    spreadsheet_id = request.form.get("spreadsheet_id")
    
    # Get the selected worksheet name
    worksheet_name = request.form.get("worksheet_name", "Sheet1")
    
    # Get field mappings
    field_mappings = {}
    for field in ["date", "distance", "duration", "pace", "heart_rate"]:
        header = request.form.get(f"map_{field}")
        if header:
            field_mappings[field] = header
    
    logger.info(f"Field mappings: {field_mappings}")
    print(f"DEBUG: Field mappings: {field_mappings}")
    
    # If no spreadsheet selected, use the default
    spreadsheet = None
    if spreadsheet_id:
        spreadsheet = get_spreadsheet(int(spreadsheet_id))
    
    if not spreadsheet:
        spreadsheet = get_default_spreadsheet()
    
    if not spreadsheet:
        flash("No spreadsheet configured. Please add a spreadsheet first.")
        return redirect(url_for("spreadsheets"))

    # Save header mappings to database for future use
    try:
        save_header_mappings(spreadsheet["id"], worksheet_name, field_mappings)
        logger.info(f"Saved header mappings for spreadsheet {spreadsheet['id']}, worksheet {worksheet_name}")
    except Exception as e:
        logger.error(f"Error saving header mappings: {str(e)}")
        print(f"DEBUG: Error saving header mappings: {str(e)}")

    # Update column preferences based on the field mappings
    include_date = 1 if "date" in field_mappings else 0
    include_distance = 1 if "distance" in field_mappings else 0
    include_time = 1 if "duration" in field_mappings else 0
    include_pace = 1 if "pace" in field_mappings else 0
    include_hr = 1 if "heart_rate" in field_mappings else 0
    
    # Save column preferences for this spreadsheet
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE spreadsheets SET 
                include_date = ?, 
                include_distance = ?, 
                include_time = ?, 
                include_pace = ?, 
                include_hr = ? 
                WHERE id = ?""",
            (
                include_date,
                include_distance,
                include_time,
                include_pace,
                include_hr,
                spreadsheet["id"]
            )
        )
        conn.commit()
        
    logger.info(f"Updated column preferences for spreadsheet {spreadsheet['id']}: date={include_date}, distance={include_distance}, time={include_time}, pace={include_pace}, hr={include_hr}")
    print(f"DEBUG: Updated column preferences: date={include_date}, distance={include_distance}, time={include_time}, pace={include_pace}, hr={include_hr}")

    try:
        # Auth to Google Sheets
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            os.getenv("GOOGLE_CREDS_FILE"),
            [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        client = gspread.authorize(creds)
        
        # Log the service account email for debugging
        logger.info(f"Using service account: {SERVICE_ACCOUNT_EMAIL}")
        
        # Try to open by ID first if available, otherwise by name
        sheet_obj = None
        if spreadsheet.get("sheet_id"):
            try:
                logger.debug(f"Attempting to open spreadsheet by ID: {spreadsheet['sheet_id']}")
                sheet_obj = client.open_by_key(spreadsheet["sheet_id"])
                logger.info(f"Successfully opened spreadsheet by ID: {spreadsheet['sheet_id']}")
            except gspread.exceptions.APIError as e:
                logger.error(f"API Error when opening spreadsheet by ID: {str(e)}")
                if "not found" in str(e).lower():
                    flash(f"Spreadsheet with ID '{spreadsheet['sheet_id']}' was not found. Make sure the ID is correct and the spreadsheet is shared with {SERVICE_ACCOUNT_EMAIL}")
                    return redirect(url_for("spreadsheets"))
                try:
                    logger.debug(f"Falling back to opening by name: {spreadsheet['name']}")
                    sheet_obj = client.open(spreadsheet["name"])
                    logger.info(f"Successfully opened spreadsheet by name: {spreadsheet['name']}")
                except Exception as e2:
                    logger.error(f"Error opening by name: {str(e2)}")
                    flash(f"Could not open spreadsheet by ID or name. Please make sure the spreadsheet is shared with {SERVICE_ACCOUNT_EMAIL}")
                    return redirect(url_for("spreadsheets"))
            except Exception as e:
                logger.error(f"Error opening by ID: {str(e)}")
                try:
                    logger.debug(f"Falling back to opening by name: {spreadsheet['name']}")
                    sheet_obj = client.open(spreadsheet["name"])
                    logger.info(f"Successfully opened spreadsheet by name: {spreadsheet['name']}")
                except Exception as e2:
                    logger.error(f"Error opening by name: {str(e2)}")
                    flash(f"Could not open spreadsheet. Please make sure the spreadsheet is shared with {SERVICE_ACCOUNT_EMAIL}")
                    return redirect(url_for("spreadsheets"))
        else:
            try:
                logger.debug(f"Attempting to open spreadsheet by name: {spreadsheet['name']}")
                sheet_obj = client.open(spreadsheet["name"])
                logger.info(f"Successfully opened spreadsheet by name: {spreadsheet['name']}")
            except Exception as e:
                logger.error(f"Error opening by name: {str(e)}")
                flash(f"Could not open spreadsheet '{spreadsheet['name']}'. Please make sure the spreadsheet is shared with {SERVICE_ACCOUNT_EMAIL}")
                return redirect(url_for("spreadsheets"))
        
        # Get the specified worksheet or create it if it doesn't exist
        try:
            sheet = sheet_obj.worksheet(worksheet_name)
            logger.info(f"Using worksheet: {worksheet_name}")
        except gspread.exceptions.WorksheetNotFound:
            try:
                # Create a new worksheet with the specified name
                sheet = sheet_obj.add_worksheet(title=worksheet_name, rows=100, cols=20)
                logger.info(f"Created new worksheet: {worksheet_name}")
                
                # Add headers to the new worksheet if we have field mappings
                if field_mappings:
                    # Create a list of headers in the order they appear in field_mappings
                    headers = list(field_mappings.values())
                    if headers:
                        sheet.update('A1', [headers])
                        logger.info(f"Added headers to new worksheet: {headers}")
            except Exception as e:
                logger.error(f"Error creating worksheet: {str(e)}")
                flash(f"Could not create worksheet '{worksheet_name}': {str(e)}")
                
                # Store the field mappings in session to preserve them
                session["saved_field_mappings"] = field_mappings
                
                # Redirect back to preview with the same parameters
                import_params = session.get("import_params", {})
                return redirect(url_for("preview_activities", **import_params))
        
        # Get all values to find the last row with data and verify headers
        all_values = sheet.get_all_values()
        
        # Check if sheet has headers and find column indices
        if not all_values:
            # Add headers if sheet is empty and we have field mappings
            print(f"DEBUG: Field mappings: {field_mappings}")
            if field_mappings:
                headers = list(field_mappings.values())
                if headers:
                    sheet.update('A1', [headers])
                    all_values = [headers]  # Update all_values to include the new headers
            else:
                flash("No field mappings provided and sheet is empty. Please select at least one field to import.")
                
                # Store the field mappings in session to preserve them
                session["saved_field_mappings"] = field_mappings
                
                # Redirect back to preview with the same parameters
                import_params = session.get("import_params", {})
                return redirect(url_for("preview_activities", **import_params))
        
        headers = all_values[0]
        
        # Find column indices for each mapped field
        column_indices = {}
        for field, header in field_mappings.items():
            try:
                column_indices[field] = headers.index(header)
            except ValueError:
                flash(f"Could not find '{header}' header in the spreadsheet")
                
                # Store the field mappings in session to preserve them
                session["saved_field_mappings"] = field_mappings
                
                # Redirect back to preview with the same parameters
                import_params = session.get("import_params", {})
                return redirect(url_for("preview_activities", **import_params))
        
        # Find the first empty row
        first_empty_row = len(all_values) + 1
        
        # Import each activity from the previewed data
        for i, activity in enumerate(formatted_activities):
            # Calculate row index for this activity
            row_idx = first_empty_row + i
            
            # Create a row with values in the correct positions
            row_values = [""] * len(headers)
            
            # Fill in the values based on field mappings
            for field, col_idx in column_indices.items():
                row_values[col_idx] = activity[field]
            
            # Update the entire row at once (more efficient)
            sheet.update(f'A{row_idx}', [row_values])
        
        flash(f"Successfully imported {len(formatted_activities)} activities to '{spreadsheet['name']}' (worksheet: {worksheet_name})!")
        
        # Clear the preview data from session
        session.pop("preview_activities", None)
        session.pop("import_params", None)
        session.pop("saved_field_mappings", None)
        
    except Exception as e:
        logger.error(f"Error importing to spreadsheet: {str(e)}")
        flash(f"Error importing to spreadsheet: {str(e)}")
        
        # Store the field mappings in session to preserve them
        session["saved_field_mappings"] = field_mappings
        
        # Redirect back to preview with the same parameters
        import_params = session.get("import_params", {})
        return redirect(url_for("preview_activities", **import_params))

    return redirect(url_for("home"))


@app.route("/import", methods=["GET", "POST"])
def import_activities():
    token = session.get("token")
    
    # Try to get from cookie session ID if not in session
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if not token:
        flash("Please connect your Strava account first")
        return redirect(url_for("home"))

    # Check if token is expired and refresh if needed
    if is_token_expired(token):
        new_token = refresh_token(token)
        if not new_token:
            flash("Your authentication has expired. Please login again.")
            return redirect(url_for("login"))
        token = new_token
        session["token"] = token
        session.permanent = True
        
        # Update token in database if we have a session ID
        if COOKIE_NAME in request.cookies:
            session_id = request.cookies.get(COOKIE_NAME)
            store_token_with_session_id(session_id, token)

    # If it's a GET request, render the form
    if request.method == "GET":
        return render_template("import_form.html")

    # For POST requests, redirect to preview
    return redirect(url_for("preview_activities"))


@app.route("/sync_today")
def sync_today():
    token = session.get("token")
    
    # Try to get from cookie session ID if not in session
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if not token:
        flash("Please connect your Strava account first")
        return redirect(url_for("home"))

    # Check if token is expired and refresh if needed
    if is_token_expired(token):
        new_token = refresh_token(token)
        if not new_token:
            flash("Your authentication has expired. Please login again.")
            return redirect(url_for("login"))
        token = new_token
        session["token"] = token
        session.permanent = True
        
        # Update token in database if we have a session ID
        if COOKIE_NAME in request.cookies:
            session_id = request.cookies.get(COOKIE_NAME)
            store_token_with_session_id(session_id, token)

    # Calculate today's timestamp (beginning of day)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    after_timestamp = int(today.timestamp())

    # Use the access_token from the token dictionary
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    params = {"after": after_timestamp, "per_page": 30}
    
    resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers=headers,
        params=params,
    )

    if resp.status_code != 200:
        flash(f"Error accessing Strava API: {resp.status_code}")
        return redirect(url_for("home"))

    acts = resp.json()
    
    if not acts:
        flash("No activities found for today")
        return redirect(url_for("home"))
    # Process and format activities for display
    formatted_activities = []
    for a in acts:
        # Format date (dd/mm/yyyy)
        date_obj = datetime.strptime(a["start_date"], "%Y-%m-%dT%H:%M:%SZ")
        formatted_date = date_obj.strftime("%d/%m/%Y")
        
        # Format distance (xx,yy km)
        distance_km_numeric = round(a["distance"] / 1000, 2)
        distance_km = str(distance_km_numeric).replace('.', ',')
        
        # Format duration (hh:mm:ss)
        duration_seconds = a["moving_time"]
        hours, remainder = divmod(duration_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        # Calculate pace (min/km)
        if distance_km_numeric > 0:
            pace_seconds = duration_seconds / distance_km_numeric
            pace_minutes, pace_remainder_seconds = divmod(pace_seconds, 60)
            pace = f"{int(pace_minutes):02d}:{int(pace_remainder_seconds):02d}".replace('.', ',')
        else:
            pace = "00:00"
            
        # Get HR (if available)
        avg_hr_raw = a.get("average_heartrate", "")
        avg_hr = round(float(avg_hr_raw)) if avg_hr_raw != "" else ""
        
        formatted_activities.append({
            "date": formatted_date,
            "distance": distance_km,
            "duration": duration,
            "pace": pace,
            "heart_rate": avg_hr,
            "name": a.get("name", "Activity"),
            "type": a.get("type", "")
        })

    # Store the formatted activities in the session for later use
    session["preview_activities"] = formatted_activities
    
    # Get all spreadsheets for selection
    all_spreadsheets = get_spreadsheets()
    
    # Get default spreadsheet
    default_spreadsheet = None
    for sheet in all_spreadsheets:
        if sheet.get("is_default"):
            default_spreadsheet = sheet
            break
    
    # If no default, use the first one
    if not default_spreadsheet and all_spreadsheets:
        default_spreadsheet = all_spreadsheets[0]
    
    # Ensure sheet_id is included in the default spreadsheet
    if default_spreadsheet and not default_spreadsheet.get("sheet_id"):
        logger.warning(f"Default spreadsheet {default_spreadsheet.get('name')} has no sheet_id")
        print(f"DEBUG: Default spreadsheet {default_spreadsheet.get('name')} has no sheet_id")
    else:
        logger.info(f"Default spreadsheet: {default_spreadsheet.get('name')}, sheet_id: {default_spreadsheet.get('sheet_id')}")
        print(f"DEBUG: Default spreadsheet: {default_spreadsheet.get('name')}, sheet_id: {default_spreadsheet.get('sheet_id')}")
    
    # Get worksheet names for the selected spreadsheet
    worksheet_names = []
    selected_worksheet = "Sheet1"  # Default worksheet
    if default_spreadsheet and default_spreadsheet.get("sheet_id"):
        try:
            logger.info(f"Getting worksheets for default spreadsheet: {default_spreadsheet.get('name')} (ID: {default_spreadsheet.get('sheet_id')})")
            worksheet_names = get_worksheet_names(default_spreadsheet.get("sheet_id"))
            if worksheet_names:
                selected_worksheet = worksheet_names[0]  # Use first worksheet as default
        except Exception as e:
            logger.error(f"Error getting worksheet names: {str(e)}")
            worksheet_names = ["Sheet1"]
    
    # Get saved field mappings from session (if coming back from a failed import)
    saved_field_mappings = session.get("saved_field_mappings")
    
    return render_template(
        "preview_activities.html", 
        activities=formatted_activities, 
        spreadsheets=all_spreadsheets,
        selected_spreadsheet=default_spreadsheet,
        worksheet_names=worksheet_names,
        selected_worksheet=selected_worksheet,
        saved_field_mappings=saved_field_mappings
    )


@app.route("/logout")
def logout():
    # Clear token from session
    session.pop("token", None)
    
    # Clear token from database if session ID exists
    if COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        delete_session(session_id)
    
    # Delete the cookie
    resp = make_response(redirect(url_for("home")))
    resp.delete_cookie(COOKIE_NAME)
    
    flash("You have been logged out")
    return resp


@app.route("/spreadsheets")
def spreadsheets():
    token = session.get("token")
    
    # Try to get from cookie session ID if not in session
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if not token:
        flash("Please connect your Strava account first")
        return redirect(url_for("home"))
    
    # Get all spreadsheets
    all_spreadsheets = get_spreadsheets()
    
    return render_template("spreadsheets.html", spreadsheets=all_spreadsheets)


def extract_sheet_id_from_url(url):
    """Extract the spreadsheet ID from a Google Sheets URL"""
    if not url:
        return ""
    
    # Pattern to match Google Sheets URL and extract the ID
    pattern = r"https://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)"
    match = re.search(pattern, url)
    
    if match:
        sheet_id = match.group(1)
        logger.debug(f"Extracted sheet ID: {sheet_id} from URL: {url}")
        return sheet_id
    logger.debug(f"No sheet ID found in URL, returning as is: {url}")
    return url  # Return the original input if it doesn't match the pattern


@app.route("/spreadsheets/add", methods=["GET", "POST"])
def add_spreadsheet():
    token = session.get("token")
    
    # Try to get from cookie session ID if not in session
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if not token:
        flash("Please connect your Strava account first")
        return redirect(url_for("home"))
    
    if request.method == "GET":
        return render_template("add_spreadsheet.html", service_account_email=SERVICE_ACCOUNT_EMAIL)
    
    # Process form data
    name = request.form.get("name")
    sheet_input = request.form.get("sheet_id", "")
    is_default = 1 if request.form.get("is_default") else 0
    
    # Get column preferences
    include_date = 1 if request.form.get("include_date") == "on" else 0
    include_distance = 1 if request.form.get("include_distance") == "on" else 0
    include_time = 1 if request.form.get("include_time") == "on" else 0
    include_pace = 1 if request.form.get("include_pace") == "on" else 0
    include_hr = 1 if request.form.get("include_hr") == "on" else 0
    
    if not name:
        flash("Spreadsheet name is required")
        return render_template("add_spreadsheet.html", service_account_email=SERVICE_ACCOUNT_EMAIL)
    
    # Extract sheet ID from URL if provided
    sheet_id = extract_sheet_id_from_url(sheet_input)
    
    # Verify the spreadsheet is accessible before saving
    if sheet_id:
        try:
            # Auth to Google Sheets
            creds = ServiceAccountCredentials.from_json_keyfile_name(
                os.getenv("GOOGLE_CREDS_FILE"),
                [
                    "https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/drive",
                ],
            )
            client = gspread.authorize(creds)
            
            # Try to open the spreadsheet to verify access
            try:
                sheet_obj = client.open_by_key(sheet_id)
                logger.info(f"Successfully verified access to spreadsheet with ID: {sheet_id}")
            except gspread.exceptions.APIError as e:
                logger.error(f"API Error when verifying spreadsheet: {str(e)}")
                if "not found" in str(e).lower():
                    flash(f"Spreadsheet with ID '{sheet_id}' was not found. Make sure the ID is correct and the spreadsheet is shared with {SERVICE_ACCOUNT_EMAIL}")
                else:
                    flash(f"Error accessing spreadsheet: {str(e)}")
                return render_template("add_spreadsheet.html", service_account_email=SERVICE_ACCOUNT_EMAIL)
            except Exception as e:
                logger.error(f"Error when verifying spreadsheet: {str(e)}")
                flash(f"Error accessing spreadsheet: {str(e)}")
                return render_template("add_spreadsheet.html", service_account_email=SERVICE_ACCOUNT_EMAIL)
        except Exception as e:
            logger.error(f"Error with Google credentials: {str(e)}")
            flash(f"Error with Google credentials: {str(e)}")
            return render_template("add_spreadsheet.html", service_account_email=SERVICE_ACCOUNT_EMAIL)
    
    # If setting as default, unset any existing default
    if is_default:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE spreadsheets SET is_default = 0")
            conn.commit()
    
    # Insert new spreadsheet with column preferences
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO spreadsheets 
               (name, sheet_id, is_default, include_date, include_distance, include_time, include_pace, include_hr) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, sheet_id, is_default, include_date, include_distance, include_time, include_pace, include_hr)
        )
        conn.commit()
    
    flash(f"Spreadsheet '{name}' added successfully")
    return redirect(url_for("spreadsheets"))


@app.route("/spreadsheets/edit/<int:id>", methods=["GET", "POST"])
def edit_spreadsheet(id):
    token = session.get("token")
    
    # Try to get from cookie session ID if not in session
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if not token:
        flash("Please connect your Strava account first")
        return redirect(url_for("home"))
    
    spreadsheet = get_spreadsheet(id)
    if not spreadsheet:
        flash("Spreadsheet not found")
        return redirect(url_for("spreadsheets"))
    
    if request.method == "GET":
        return render_template("edit_spreadsheet.html", spreadsheet=spreadsheet, service_account_email=SERVICE_ACCOUNT_EMAIL)
    
    # Process form data
    name = request.form.get("name")
    sheet_input = request.form.get("sheet_id", "")
    is_default = 1 if request.form.get("is_default") else 0
    
    # Get column preferences
    include_date = 1 if request.form.get("include_date") == "on" else 0
    include_distance = 1 if request.form.get("include_distance") == "on" else 0
    include_time = 1 if request.form.get("include_time") == "on" else 0
    include_pace = 1 if request.form.get("include_pace") == "on" else 0
    include_hr = 1 if request.form.get("include_hr") == "on" else 0
    
    if not name:
        flash("Spreadsheet name is required")
        return render_template("edit_spreadsheet.html", spreadsheet=spreadsheet, service_account_email=SERVICE_ACCOUNT_EMAIL)
    
    # Extract sheet ID from URL if provided
    sheet_id = extract_sheet_id_from_url(sheet_input)
    
    # Verify the spreadsheet is accessible before saving
    if sheet_id:
        try:
            # Auth to Google Sheets
            creds = ServiceAccountCredentials.from_json_keyfile_name(
                os.getenv("GOOGLE_CREDS_FILE"),
                [
                    "https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/drive",
                ],
            )
            client = gspread.authorize(creds)
            
            # Try to open the spreadsheet to verify access
            try:
                sheet_obj = client.open_by_key(sheet_id)
                logger.info(f"Successfully verified access to spreadsheet with ID: {sheet_id}")
            except gspread.exceptions.APIError as e:
                logger.error(f"API Error when opening spreadsheet by ID: {str(e)}")
                if "not found" in str(e).lower():
                    flash(f"Spreadsheet with ID '{sheet_id}' was not found. Make sure the ID is correct and the spreadsheet is shared with {SERVICE_ACCOUNT_EMAIL}")
                else:
                    flash(f"Error accessing spreadsheet: {str(e)}")
                return render_template("edit_spreadsheet.html", spreadsheet=spreadsheet, service_account_email=SERVICE_ACCOUNT_EMAIL)
            except Exception as e:
                logger.error(f"Error when verifying spreadsheet: {str(e)}")
                flash(f"Error accessing spreadsheet: {str(e)}")
                return render_template("edit_spreadsheet.html", spreadsheet=spreadsheet, service_account_email=SERVICE_ACCOUNT_EMAIL)
        except Exception as e:
            logger.error(f"Error with Google credentials: {str(e)}")
            flash(f"Error with Google credentials: {str(e)}")
            return render_template("edit_spreadsheet.html", spreadsheet=spreadsheet, service_account_email=SERVICE_ACCOUNT_EMAIL)
    
    # If setting as default, unset any existing default
    if is_default:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE spreadsheets SET is_default = 0")
            conn.commit()
    
    # Update spreadsheet with column preferences
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE spreadsheets SET 
               name = ?, 
               sheet_id = ?, 
               is_default = ?,
               include_date = ?,
               include_distance = ?,
               include_time = ?,
               include_pace = ?,
               include_hr = ?
               WHERE id = ?""",
            (name, sheet_id, is_default, include_date, include_distance, include_time, include_pace, include_hr, id)
        )
        conn.commit()
    
    flash(f"Spreadsheet '{name}' updated successfully")
    return redirect(url_for("spreadsheets"))


@app.route("/spreadsheets/delete/<int:id>", methods=["POST"])
def delete_spreadsheet(id):
    token = session.get("token")
    
    # Try to get from cookie session ID if not in session
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if not token:
        flash("Please connect your Strava account first")
        return redirect(url_for("home"))
    
    spreadsheet = get_spreadsheet(id)
    if not spreadsheet:
        flash("Spreadsheet not found")
        return redirect(url_for("spreadsheets"))
    
    # Check if it's the default spreadsheet
    if spreadsheet.get("is_default"):
        flash("Cannot delete the default spreadsheet")
        return redirect(url_for("spreadsheets"))
    
    # Delete spreadsheet
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM spreadsheets WHERE id = ?", (id,))
        conn.commit()
    
    flash(f"Spreadsheet '{spreadsheet['name']}' deleted successfully")
    return redirect(url_for("spreadsheets"))


@app.route("/spreadsheets/set_default/<int:id>", methods=["POST"])
def set_default_spreadsheet(id):
    token = session.get("token")
    
    # Try to get from cookie session ID if not in session
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if not token:
        flash("Please connect your Strava account first")
        return redirect(url_for("home"))
    
    spreadsheet = get_spreadsheet(id)
    if not spreadsheet:
        flash("Spreadsheet not found")
        return redirect(url_for("spreadsheets"))
    
    # Set as default and unset any existing default
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE spreadsheets SET is_default = 0")
        cursor.execute("UPDATE spreadsheets SET is_default = 1 WHERE id = ?", (id,))
        conn.commit()
    
    flash(f"'{spreadsheet['name']}' set as default spreadsheet")
    return redirect(url_for("spreadsheets"))


# Add a new function to get worksheet names from a spreadsheet
def get_worksheet_names(spreadsheet_id):
    """Get all worksheet names from a spreadsheet"""
    logger.info(f"Fetching worksheet names for spreadsheet ID: {spreadsheet_id}")
    
    if not spreadsheet_id or spreadsheet_id == "undefined" or spreadsheet_id == "null":
        logger.warning(f"Invalid spreadsheet ID: {spreadsheet_id}")
        return ["Sheet1"]
        
    try:
        # Auth to Google Sheets
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            os.getenv("GOOGLE_CREDS_FILE"),
            [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        client = gspread.authorize(creds)
        
        # Try to open the spreadsheet
        logger.debug(f"Attempting to open spreadsheet with ID: {spreadsheet_id}")
        sheet_obj = client.open_by_key(spreadsheet_id)
        
        # Get all worksheets
        worksheets = sheet_obj.worksheets()
        
        # Return list of worksheet names
        worksheet_names = [ws.title for ws in worksheets]
        logger.info(f"Found {len(worksheet_names)} worksheets: {worksheet_names}")
        return worksheet_names
    except gspread.exceptions.APIError as e:
        logger.error(f"Google Sheets API error: {str(e)}")
        return ["Sheet1"]
    except Exception as e:
        logger.error(f"Error getting worksheet names: {str(e)}")
        return ["Sheet1"]  # Default fallback


@app.route("/get_worksheets/<sheet_id>")
def get_worksheets(sheet_id):
    """API endpoint to get worksheets for a spreadsheet"""
    logger.info(f"Getting worksheets for sheet ID: {sheet_id}")
    
    token = session.get("token")
    
    # Try to get from cookie session ID if not in session
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if not token:
        logger.warning("User not authenticated when requesting worksheets")
        return jsonify({"error": "Not authenticated", "worksheets": ["Sheet1"]}), 401
    
    # Get worksheet names
    try:
        if not sheet_id or sheet_id == "undefined" or sheet_id == "null":
            logger.warning(f"Invalid sheet ID received: {sheet_id}")
            return jsonify({"worksheets": ["Sheet1"]})
            
        worksheet_names = get_worksheet_names(sheet_id)
        logger.info(f"Found worksheets: {worksheet_names}")
        return jsonify({"worksheets": worksheet_names})
    except Exception as e:
        logger.error(f"Error getting worksheets: {str(e)}")
        return jsonify({"error": str(e), "worksheets": ["Sheet1"]})


@app.route("/debug/spreadsheets")
def debug_spreadsheets():
    """Debug endpoint to check spreadsheet IDs"""
    if not app.debug:
        return "Debug endpoints only available in debug mode", 403
        
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, sheet_id FROM spreadsheets")
        spreadsheets = [dict(row) for row in cursor.fetchall()]
        
    return jsonify({"spreadsheets": spreadsheets})


def get_worksheet_headers(spreadsheet_id, worksheet_name=None):
    """Get headers from the first row of a worksheet"""
    logger.info(f"Fetching headers for spreadsheet ID: {spreadsheet_id}, worksheet: {worksheet_name}")
    print(f"DEBUG: Fetching headers for spreadsheet ID: {spreadsheet_id}, worksheet: {worksheet_name}")
    
    if not spreadsheet_id or spreadsheet_id == "undefined" or spreadsheet_id == "null":
        logger.warning(f"Invalid spreadsheet ID: {spreadsheet_id}")
        return []
        
    try:
        # Auth to Google Sheets
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            os.getenv("GOOGLE_CREDS_FILE"),
            [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        client = gspread.authorize(creds)
        
        # Try to open the spreadsheet
        logger.debug(f"Attempting to open spreadsheet with ID: {spreadsheet_id}")
        sheet_obj = client.open_by_key(spreadsheet_id)
        
        # Get all worksheets
        worksheets = sheet_obj.worksheets()
        
        if not worksheets:
            logger.warning("No worksheets found in the spreadsheet")
            return []
            
        # If worksheet_name is None or empty, use the first worksheet
        if not worksheet_name or worksheet_name == "undefined" or worksheet_name == "null":
            worksheet = worksheets[0]
            logger.info(f"No worksheet specified, using first worksheet: {worksheet.title}")
        else:
            # Get the specified worksheet
            try:
                worksheet = sheet_obj.worksheet(worksheet_name)
                logger.info(f"Using specified worksheet: {worksheet_name}")
            except gspread.exceptions.WorksheetNotFound:
                # If worksheet not found, use the first worksheet
                worksheet = worksheets[0]
                logger.warning(f"Worksheet '{worksheet_name}' not found, using '{worksheet.title}' instead")
        
        # Get the first row (headers)
        headers = worksheet.row_values(1)
        
        # Filter out empty headers and ensure we have values
        headers = [h for h in headers if h.strip()]
        
        logger.info(f"Found {len(headers)} headers: {headers}")
        print(f"DEBUG: Found {len(headers)} headers: {headers}")
        return headers
    except gspread.exceptions.APIError as e:
        logger.error(f"Google Sheets API error: {str(e)}")
        print(f"DEBUG: Google Sheets API error: {str(e)}")
        return []
    except Exception as e:
        logger.error(f"Error getting worksheet headers: {str(e)}")
        print(f"DEBUG: Error getting worksheet headers: {str(e)}")
        return []

@app.route("/get_worksheet_headers/<sheet_id>/<worksheet_name>")
def get_headers_endpoint(sheet_id, worksheet_name):
    """API endpoint to get headers for a worksheet"""
    logger.info(f"Getting headers for sheet ID: {sheet_id}, worksheet: {worksheet_name}")
    print(f"DEBUG: Getting headers for sheet ID: {sheet_id}, worksheet: {worksheet_name}")
    
    token = session.get("token")
    
    # Try to get from cookie session ID if not in session
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if not token:
        logger.warning("User not authenticated when requesting worksheet headers")
        print("DEBUG: User not authenticated when requesting worksheet headers")
        return jsonify({"error": "Not authenticated", "headers": []}), 401
    
    # Get worksheet headers
    try:
        if not sheet_id or sheet_id == "undefined" or sheet_id == "null":
            logger.warning(f"Invalid sheet ID received: {sheet_id}")
            print(f"DEBUG: Invalid sheet ID received: {sheet_id}")
            return jsonify({"headers": []})
            
        headers = get_worksheet_headers(sheet_id, worksheet_name)
        logger.info(f"Found headers: {headers}")
        print(f"DEBUG: Found headers: {headers}")
        return jsonify({"headers": headers})
    except Exception as e:
        logger.error(f"Error getting worksheet headers: {str(e)}")
        print(f"DEBUG: Error getting worksheet headers: {str(e)}")
        return jsonify({"error": str(e), "headers": []})


# Functions to save and load header mappings
def save_header_mappings(spreadsheet_id, worksheet_name, mappings):
    """Save header mappings for a spreadsheet and worksheet"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # First delete any existing mappings for this spreadsheet and worksheet
        cursor.execute(
            "DELETE FROM header_mappings WHERE spreadsheet_id = ? AND worksheet_name = ?",
            (spreadsheet_id, worksheet_name)
        )
        
        # Insert new mappings
        for field_name, header_name in mappings.items():
            if header_name:  # Only save non-empty mappings
                cursor.execute(
                    "INSERT INTO header_mappings (spreadsheet_id, worksheet_name, field_name, header_name) VALUES (?, ?, ?, ?)",
                    (spreadsheet_id, worksheet_name, field_name, header_name)
                )
        
        conn.commit()
        logger.info(f"Saved {len(mappings)} header mappings for spreadsheet {spreadsheet_id}, worksheet {worksheet_name}")

def get_header_mappings(spreadsheet_id, worksheet_name):
    """Get header mappings for a spreadsheet and worksheet"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT field_name, header_name FROM header_mappings WHERE spreadsheet_id = ? AND worksheet_name = ?",
            (spreadsheet_id, worksheet_name)
        )
        
        mappings = {row['field_name']: row['header_name'] for row in cursor.fetchall()}
        logger.info(f"Retrieved {len(mappings)} header mappings for spreadsheet {spreadsheet_id}, worksheet {worksheet_name}")
        return mappings


@app.route("/get_header_mappings/<spreadsheet_id>/<worksheet_name>")
def get_header_mappings_endpoint(spreadsheet_id, worksheet_name):
    """API endpoint to get saved header mappings for a spreadsheet and worksheet"""
    logger.info(f"Getting header mappings for spreadsheet ID: {spreadsheet_id}, worksheet: {worksheet_name}")
    print(f"DEBUG: Getting header mappings for spreadsheet ID: {spreadsheet_id}, worksheet: {worksheet_name}")
    
    token = session.get("token")
    
    # Try to get from cookie session ID if not in session
    if not token and COOKIE_NAME in request.cookies:
        session_id = request.cookies.get(COOKIE_NAME)
        token = get_token_from_session_id(session_id)
        if token:
            session["token"] = token
    
    if not token:
        logger.warning("User not authenticated when requesting header mappings")
        print("DEBUG: User not authenticated when requesting header mappings")
        return jsonify({"error": "Not authenticated", "mappings": {}, "column_preferences": {}}), 401
    
    # Get header mappings
    try:
        if not spreadsheet_id or spreadsheet_id == "undefined" or spreadsheet_id == "null":
            logger.warning(f"Invalid spreadsheet ID received: {spreadsheet_id}")
            print(f"DEBUG: Invalid spreadsheet ID received: {spreadsheet_id}")
            return jsonify({"mappings": {}, "column_preferences": {}})
            
        # Get saved mappings from database
        mappings = get_header_mappings(int(spreadsheet_id), worksheet_name)
        
        # Get spreadsheet column preferences
        spreadsheet = get_spreadsheet(int(spreadsheet_id))
        column_preferences = {}
        if spreadsheet:
            column_preferences = {
                "date": spreadsheet.get("include_date", 1) == 1,
                "distance": spreadsheet.get("include_distance", 1) == 1,
                "time": spreadsheet.get("include_time", 1) == 1,
                "pace": spreadsheet.get("include_pace", 1) == 1,
                "heart_rate": spreadsheet.get("include_hr", 1) == 1
            }
        
        logger.info(f"Found mappings: {mappings}")
        logger.info(f"Column preferences: {column_preferences}")
        print(f"DEBUG: Found mappings: {mappings}")
        print(f"DEBUG: Column preferences: {column_preferences}")
        
        return jsonify({
            "mappings": mappings,
            "column_preferences": column_preferences
        })
    except Exception as e:
        logger.error(f"Error getting header mappings: {str(e)}")
        print(f"DEBUG: Error getting header mappings: {str(e)}")
        return jsonify({"error": str(e), "mappings": {}, "column_preferences": {}})


if __name__ == "__main__":
    app.run(debug=True)

