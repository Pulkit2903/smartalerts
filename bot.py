"""
smartalerts - Google Calendar Bot
Auto-adds placement deadlines to Google Calendar with alarms.
"""

import os
import re
import json
import base64
import sqlite3
import schedule
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import google.generativeai as genai

load_dotenv()

GEMINI_KEY = os.getenv('GEMINI_API_KEY')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL_MINUTES', '15'))

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar'
]

if not GEMINI_KEY:
    raise SystemExit("GEMINI_API_KEY not set. Add it to .env before running.")

genai.configure(api_key=GEMINI_KEY)
ai_model = genai.GenerativeModel('gemini-2.5-flash-lite')


def init_database():
    conn = sqlite3.connect('placements.db')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS placements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id TEXT UNIQUE,
            company TEXT,
            role TEXT,
            ctc TEXT,
            deadline TEXT,
            eligibility TEXT,
            link TEXT,
            calendar_event_id TEXT,
            created_at TEXT
        )
    ''')
    conn.commit()
    conn.close()
    print("Database ready")


def is_email_processed(email_id):
    conn = sqlite3.connect('placements.db')
    result = conn.execute(
        'SELECT id FROM placements WHERE email_id = ?', (email_id,)
    ).fetchone()
    conn.close()
    return result is not None


def save_placement(email_id, data, event_id=None):
    conn = sqlite3.connect('placements.db')
    conn.execute('''
        INSERT INTO placements
        (email_id, company, role, ctc, deadline, eligibility, link, calendar_event_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        email_id,
        data.get('company', 'Unknown'),
        data.get('role', 'N/A'),
        data.get('ctc', 'N/A'),
        data.get('deadline', 'N/A'),
        data.get('eligibility', 'N/A'),
        data.get('link', ''),
        event_id,
        datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()


def authenticate():
    creds = None

    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return creds


def fetch_placement_emails(creds):
    service = build('gmail', 'v1', credentials=creds)

    query = (
        "(subject:(placement OR recruitment OR hiring OR internship OR drive OR opening OR campus) "
        "OR from:(placement OR career OR hr OR recruit)) is:unread newer_than:7d"
    )

    results = service.users().messages().list(
        userId='me',
        q=query,
        maxResults=10
    ).execute()

    messages = results.get('messages', [])
    print(f"Found {len(messages)} potential placement emails")

    return messages, service


def _extract_body(payload):
    plain, html = [], []

    def walk(part):
        mime = part.get('mimeType', '')
        data = part.get('body', {}).get('data')
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
                if mime == 'text/plain':
                    plain.append(decoded)
                elif mime == 'text/html':
                    html.append(decoded)
            except Exception:
                pass
        for sub in part.get('parts', []) or []:
            walk(sub)

    walk(payload)

    if plain:
        return '\n'.join(plain).strip()
    if html:
        text = '\n'.join(html)
        text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    return ''


def get_email_content(service, email_id):
    msg = service.users().messages().get(
        userId='me', id=email_id, format='full'
    ).execute()

    headers = msg['payload']['headers']

    subject = next(
        (h['value'] for h in headers if h['name'] == 'Subject'),
        'No Subject'
    )
    sender = next(
        (h['value'] for h in headers if h['name'] == 'From'),
        'Unknown'
    )

    body = _extract_body(msg['payload']) or msg.get('snippet', '')

    return {'subject': subject, 'sender': sender, 'body': body}


def parse_with_ai(email_data):
    prompt = f"""Analyze this email and extract placement/job details.
Return ONLY valid JSON, no markdown or extra text.

Email Subject: {email_data['subject']}
Email From: {email_data['sender']}
Email Body: {email_data['body'][:4000]}

Return this JSON structure:
{{
    "is_placement": true or false,
    "company": "company name or null",
    "role": "job role or null",
    "ctc": "salary/CTC like '8 LPA' or null",
    "deadline": "deadline in YYYY-MM-DD HH:MM format or null",
    "eligibility": "eligibility criteria or null",
    "link": "registration link or empty string"
}}

Rules:
- is_placement = true ONLY if this is a real job/placement opportunity
- Extract specific deadline if mentioned
- For deadline, use 24-hour format (e.g., "2026-05-05 23:59")
- If only date given (no time), use "23:59" as time
- Be precise, no guessing"""

    try:
        response = ai_model.generate_content(prompt)
        text = response.text.strip()

        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        text = text.strip()

        parsed = json.loads(text)
        for k, v in list(parsed.items()):
            if isinstance(v, str) and v.strip().lower() in ('null', 'none', 'n/a', ''):
                parsed[k] = None
        return parsed

    except Exception as e:
        print(f"AI parsing error: {e}")
        return None


def add_to_calendar(placement, creds):
    service = build('calendar', 'v3', credentials=creds)

    try:
        deadline_str = placement.get('deadline')
        if not deadline_str or deadline_str == 'N/A':
            print("No deadline found, skipping calendar")
            return None

        deadline = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
    except Exception as e:
        print(f"Invalid deadline format: {e}")
        return None

    if deadline < datetime.now():
        print(f"Deadline already passed: {deadline_str}")
        return None

    event = {
        'summary': f"APPLY: {placement['company']} - {placement.get('role', 'Position')}",
        'description': f"""PLACEMENT OPPORTUNITY

Company: {placement.get('company', 'N/A')}
Role: {placement.get('role', 'N/A')}
CTC: {placement.get('ctc', 'N/A')}
Eligibility: {placement.get('eligibility', 'N/A')}

DEADLINE: {placement['deadline']}

Apply Here: {placement.get('link', 'Check email for link')}

DON'T MISS THIS DEADLINE!""",

        'start': {
            'dateTime': deadline.isoformat(),
            'timeZone': 'Asia/Kolkata',
        },
        'end': {
            'dateTime': (deadline + timedelta(minutes=30)).isoformat(),
            'timeZone': 'Asia/Kolkata',
        },

        'reminders': {
            'useDefault': False,
            'overrides': [
                {'method': 'email', 'minutes': 24 * 60},
                {'method': 'popup', 'minutes': 24 * 60},
                {'method': 'popup', 'minutes': 6 * 60},
                {'method': 'popup', 'minutes': 60},
                {'method': 'popup', 'minutes': 15},
            ],
        },

        'colorId': '11',
    }

    try:
        created_event = service.events().insert(
            calendarId='primary',
            body=event
        ).execute()

        print(f"Calendar event created: {placement['company']}")
        print(f"  Link: {created_event.get('htmlLink')}")
        return created_event['id']

    except Exception as e:
        print(f"Calendar error: {e}")
        return None


def check_emails():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking emails...")

    try:
        creds = authenticate()
        messages, service = fetch_placement_emails(creds)

        new_count = 0

        for msg in messages:
            email_id = msg['id']

            if is_email_processed(email_id):
                continue

            email_data = get_email_content(service, email_id)
            print(f"  Processing: {email_data['subject'][:60]}...")

            parsed = parse_with_ai(email_data)

            if not parsed:
                continue

            if not parsed.get('is_placement'):
                save_placement(email_id, {'company': 'Not a placement'})
                continue

            event_id = add_to_calendar(parsed, creds)

            save_placement(email_id, parsed, event_id)

            new_count += 1

            print(f"  Added: {parsed['company']} - {parsed.get('role', '')}")
            print(f"     Deadline: {parsed.get('deadline', 'N/A')}")

        print(f"Done. Added {new_count} new placements to calendar")

    except Exception as e:
        print(f"Error in check_emails: {e}")


def send_summary_to_calendar():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Creating daily summary...")

    try:
        creds = authenticate()
        service = build('calendar', 'v3', credentials=creds)

        conn = sqlite3.connect('placements.db')
        upcoming = conn.execute('''
            SELECT company, role, deadline, link FROM placements
            WHERE deadline != 'N/A' AND deadline IS NOT NULL
            AND company != 'Not a placement'
            ORDER BY deadline LIMIT 5
        ''').fetchall()
        conn.close()

        if not upcoming:
            return

        future_placements = []
        for p in upcoming:
            try:
                dl = datetime.strptime(p[2], "%Y-%m-%d %H:%M")
                if dl > datetime.now():
                    future_placements.append(p)
            except Exception:
                continue

        if not future_placements:
            return

        description = "UPCOMING PLACEMENT DEADLINES:\n\n"
        for i, p in enumerate(future_placements, 1):
            description += f"{i}. {p[0]} - {p[1]}\n"
            description += f"   Deadline: {p[2]}\n"
            description += f"   Link: {p[3]}\n\n"

        now = datetime.now()
        summary_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now > summary_time:
            summary_time = now + timedelta(minutes=5)

        event = {
            'summary': "Daily Placement Summary",
            'description': description,
            'start': {
                'dateTime': summary_time.isoformat(),
                'timeZone': 'Asia/Kolkata',
            },
            'end': {
                'dateTime': (summary_time + timedelta(minutes=15)).isoformat(),
                'timeZone': 'Asia/Kolkata',
            },
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'popup', 'minutes': 0},
                ],
            },
            'colorId': '7',
        }

        service.events().insert(calendarId='primary', body=event).execute()
        print("Daily summary added to calendar")

    except Exception as e:
        print(f"Error in summary: {e}")


if __name__ == "__main__":
    print("=" * 60)
    print("smartalerts - Google Calendar Bot")
    print("=" * 60)
    print()
    print("This bot will:")
    print("  - Read placement emails from Gmail")
    print("  - Use AI to extract details")
    print("  - Add deadlines to Google Calendar")
    print("  - Set multiple reminders")
    print()
    print("=" * 60)

    init_database()

    schedule.every(CHECK_INTERVAL).minutes.do(check_emails)
    schedule.every().day.at("09:00").do(send_summary_to_calendar)

    check_emails()

    print(f"\nBot running... checking every {CHECK_INTERVAL} minutes")
    print("Check your Google Calendar for placement events!")
    print("Press Ctrl+C to stop\n")

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            print("\n\nBot stopped. Goodbye!")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)
