# Telegram Course Reminder Bot

Lightweight Telegram bot to share course schedules, send reminders, and provide quick access to Zoom links, maps, and attendance URLs for the NTU Programming Foundations run (or any course data you provide).

## Features
- Reminders 30 minutes before each session and at session end.
- `/schedule`, `/next`, and `/info` commands to view the timetable in Singapore time (configurable).
- Inline buttons for Zoom, maps, carpark info, attendance QR/check, and materials where available.
- Persisted subscriber list in a local SQLite database.
- Dry-run mode for safe verification without sending messages.

## Quick start
1) Install Python 3.11 (or use `environment.yml` with Conda).  
2) Install dependencies: `pip install -r requirements.txt`  
3) Copy `.env.example` to `.env` and fill in values (see Configuration).  
4) Review/edit `course_data.json` for your run’s schedule and links.  
5) Start the bot: `python bot.py`

## Configuration (`.env`)
- `BOT_TOKEN` (required): Telegram bot token from BotFather.
- `TIMEZONE` (optional): IANA name, defaults to `Asia/Singapore`.
- `COURSE_FILE` (optional): Path to the course JSON file, defaults to `course_data.json`.
- `SUBSCRIBER_DB` (optional): SQLite path for subscribers, defaults to `subscribers.db`.
- `DRY_RUN` (optional): `1/true/yes` to log reminders without sending messages.

## Course data format
`course_data.json` holds the schedule and links. Key fields:
- `title`, `attendance_qr_url`, `attendance_check_url`, `carpark_info_url`, `materials_url`
- `sessions`: list of objects with `id`, `lecture`, `session`, `date` (`YYYY-MM-DD`), `start_time`, `end_time`, `mode_location`, and optional `venue`, `google_map`, `zoom_link`, `meeting_id`, `passcode`.

Example session entry:
```json
{
  "id": "lecture-1-2025-12-08",
  "lecture": "Lecture 1",
  "session": "Single Session",
  "date": "2025-12-08",
  "start_time": "19:00",
  "end_time": "22:00",
  "mode_location": "Zoom",
  "zoom_link": "https://example.zoom.us/j/123",
  "meeting_id": "861 0134 7300",
  "passcode": "791053"
}
```

## Bot commands
- `/start` — subscribe to reminders.
- `/stop` — unsubscribe.
- `/next` — show the next upcoming session.
- `/schedule` — list all sessions grouped by date.
- `/info <lecture|date>` — details for a lecture or date (e.g., `Lecture 3` or `2025-12-13`).
- `/materials` — course materials link (if configured).
- `/help` — list available commands.

## Notes
- Reminders are scheduled on startup using the job queue; restarting the bot reloads the schedule and reminders.
- Subscribers are stored in `subscribers.db` (ignored by git by default).
