#!/usr/bin/env python3
"""
Cybertron Academy — Daily Lesson Email Dispatcher
Sends the day's lesson to Shazeema every morning at 9am via cron.
Uses Gmail API (OAuth2) via ~/.config/gmail/send.py infrastructure.
"""

import os
import sys
import glob
import base64
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

# Gmail API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TOKEN_FILE = os.path.expanduser('~/.config/gmail/token.json')
SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
]

LESSON_BASE = os.path.expanduser('~/.synlearns/aariz/lessons')
ACTIVITY_BASE = os.path.expanduser('~/.synlearns/aariz/activities')
RECIPIENTS = ['shaze.nizamudin@gmail.com', 'Fareez541@gmail.com']
FROM_NAME = 'Cybertron Academy'

# Day-of-week to lesson file mapping
DAY_MAP = {
    0: 'day1_monday.md',
    1: 'day2_tuesday.md',
    2: 'day3_wednesday.md',
    3: 'day4_thursday.md',
    4: 'day5_friday.md',
    # 5 (Saturday) and 6 (Sunday) = no lesson
}

# Day-of-week to theme
THEME_MAP = {
    0: 'Letters + Building Challenge',
    1: 'Numbers + Science Observation',
    2: 'Shapes & Patterns + Art',
    3: 'Phonics + Engineering Design',
    4: 'Free Build Friday',
}


def get_gmail_service():
    """Authenticate and return Gmail API service."""
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)


def find_current_week():
    """Find the latest week directory with lessons."""
    week_dirs = sorted(glob.glob(os.path.join(LESSON_BASE, 'week*')))
    if not week_dirs:
        return None
    # Return the latest week that has today's lesson file
    today_dow = datetime.now().weekday()
    lesson_file = DAY_MAP.get(today_dow)
    if not lesson_file:
        return None

    for week_dir in reversed(week_dirs):
        if os.path.exists(os.path.join(week_dir, lesson_file)):
            return week_dir
    return None


def find_activity_sheets(week_dir, day_num):
    """Find printable activity sheets for today's lesson."""
    sheets = []
    # Check for day-specific activities in the week directory
    pattern = os.path.join(week_dir, f'day{day_num}_activity*')
    sheets.extend(glob.glob(pattern))

    # Check the shared activities directory
    pattern = os.path.join(ACTIVITY_BASE, f'week*_day{day_num}*')
    sheets.extend(glob.glob(pattern))

    return sheets


def send_lesson():
    """Send today's lesson email."""
    today = datetime.now()
    dow = today.weekday()

    # No lessons on weekends
    if dow > 4:
        print(f"[{today.isoformat()}] Weekend — no lesson today.")
        return

    # Find lesson file
    week_dir = find_current_week()
    if not week_dir:
        print(f"[{today.isoformat()}] ERROR: No week directory found with today's lesson.")
        sys.exit(1)

    lesson_file = DAY_MAP[dow]
    lesson_path = os.path.join(week_dir, lesson_file)

    if not os.path.exists(lesson_path):
        print(f"[{today.isoformat()}] ERROR: Lesson file not found: {lesson_path}")
        sys.exit(1)

    # Read lesson content
    with open(lesson_path, 'r') as f:
        lesson_content = f.read()

    # Extract title from first heading
    title_line = ''
    for line in lesson_content.split('\n'):
        if line.startswith('## ') and '—' in line:
            title_line = line.replace('## ', '').strip()
            break
        elif line.startswith('### ') and '—' in line:
            title_line = line.replace('### ', '').strip()
            break

    week_name = os.path.basename(week_dir)
    day_num = dow + 1
    theme = THEME_MAP.get(dow, '')

    subject = f"Cybertron Academy — Day {day_num}: {theme} | {today.strftime('%A, %B %d')}"

    # Build email
    service = get_gmail_service()
    profile = service.users().getProfile(userId='me').execute()
    from_email = profile['emailAddress']

    msg = MIMEMultipart()
    msg['From'] = f'{FROM_NAME} <{from_email}>'
    msg['To'] = ', '.join(RECIPIENTS)
    msg['Subject'] = subject

    # Email body — plain text intro + full lesson
    intro = f"""Assalamu alaikum Shazeema,

Here is today's Cybertron Academy lesson for Aariz.

Day {day_num} ({today.strftime('%A')}): {theme}

The full lesson plan is below and also attached as a file you can print or reference on your phone.

Materials needed are listed at the top of the lesson — take a quick look before starting so everything is ready.

Have a wonderful learning day together!

— Cybertron Academy

{'=' * 60}

{lesson_content}
"""

    msg.attach(MIMEText(intro, 'plain'))

    # Attach the lesson as a file
    with open(lesson_path, 'rb') as f:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            'Content-Disposition',
            f'attachment; filename="CybertronAcademy_{today.strftime("%Y%m%d")}_{lesson_file}"'
        )
        msg.attach(part)

    # Attach any activity sheets
    activity_sheets = find_activity_sheets(week_dir, day_num)
    for sheet_path in activity_sheets:
        with open(sheet_path, 'rb') as f:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                'Content-Disposition',
                f'attachment; filename="{os.path.basename(sheet_path)}"'
            )
            msg.attach(part)

    # Send
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(userId='me', body={'raw': raw}).execute()

    print(f"[{today.isoformat()}] Sent lesson: {subject}")
    print(f"  Message ID: {result['id']}")
    print(f"  Recipients: {', '.join(RECIPIENTS)}")
    print(f"  Lesson: {lesson_path}")
    print(f"  Activity sheets: {len(activity_sheets)}")


if __name__ == '__main__':
    send_lesson()
