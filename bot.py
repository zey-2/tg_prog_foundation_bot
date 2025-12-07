import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.ext import (
    AIORateLimiter,
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


LOG = logging.getLogger(__name__)
DEFAULT_TIMEZONE = "Asia/Singapore"
SESSION_JSON_PATTERN = re.compile(r"\[\s*{.*}\s*\]", re.DOTALL)
INFO_QUERY = 1


@dataclass
class Session:
    id: str
    lecture: str
    session_label: str
    start: datetime
    end: datetime
    mode_location: str
    venue: Optional[str]
    google_map: Optional[str]
    zoom_link: Optional[str]
    meeting_id: Optional[str]
    passcode: Optional[str]

    @property
    def display_date(self) -> str:
        return self.start.strftime("%Y-%m-%d")


@dataclass
class CourseData:
    title: str
    sessions: List[Session]
    qr_attendance_url: Optional[str]
    attendance_check_url: Optional[str]
    carpark_info_url: Optional[str]
    materials_url: Optional[str]


class SubscriberStore:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def subscribe(self, user_id: int, chat_id: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO subscribers (user_id, chat_id, active, created_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    active=1
                """,
                (user_id, chat_id, datetime.utcnow().isoformat()),
            )
            self._conn.commit()

    def unsubscribe(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE subscribers SET active=0 WHERE user_id=?", (user_id,)
            )
            self._conn.commit()

    def is_active(self, user_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT active FROM subscribers WHERE user_id=?", (user_id,)
            )
            row = cur.fetchone()
            return bool(row and row[0])

    def active_chat_ids(self) -> List[int]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT chat_id FROM subscribers WHERE active=1"
            )
            return [row[0] for row in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _parse_time_range(time_range: str) -> tuple[str, str]:
    parts = time_range.split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid time range: {time_range}")
    return parts[0].strip(), parts[1].strip()


def _extract_start_end_times(raw: dict) -> tuple[str, str]:
    if "start_time" in raw and "end_time" in raw:
        return raw["start_time"], raw["end_time"]
    if "time" in raw:
        return _parse_time_range(raw["time"])
    raise ValueError(f"Missing time fields in session entry: {raw}")


def _build_session_id(raw: dict) -> str:
    if raw.get("id"):
        return str(raw["id"])
    pieces = [
        raw.get("lecture", ""),
        raw.get("session", ""),
        raw.get("date", ""),
    ]
    return "-".join(part.strip().replace(" ", "_") for part in pieces if part)


def _parse_sessions_list(payload: Iterable[dict], tz: ZoneInfo) -> List[Session]:
    sessions: List[Session] = []
    for raw in payload:
        start_str, end_str = _extract_start_end_times(raw)
        start_dt = datetime.combine(
            datetime.fromisoformat(raw["date"]).date(),
            datetime.strptime(start_str, "%H:%M").time(),
            tzinfo=tz,
        )
        end_dt = datetime.combine(
            start_dt.date(),
            datetime.strptime(end_str, "%H:%M").time(),
            tzinfo=tz,
        )
        sessions.append(
            Session(
                id=_build_session_id(raw),
                lecture=raw.get("lecture", "Lecture"),
                session_label=raw.get("session", "Session"),
                start=start_dt,
                end=end_dt,
                mode_location=raw.get("mode_location", ""),
                venue=raw.get("venue"),
                google_map=raw.get("google_map"),
                zoom_link=raw.get("zoom_link"),
                meeting_id=raw.get("meeting_id"),
                passcode=raw.get("passcode"),
            )
        )
    sessions.sort(key=lambda s: s.start)
    return sessions


def _find_url_after_keyword(lines: List[str], keyword: str) -> Optional[str]:
    keyword_lower = keyword.lower()
    for idx, line in enumerate(lines):
        if keyword_lower in line.lower():
            if idx + 1 < len(lines):
                candidate = lines[idx + 1].strip()
                if candidate.startswith("http"):
                    return candidate
    return None


def load_course_data_legacy(content: str, tz: ZoneInfo) -> CourseData:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    title = lines[0] if lines else "Course"

    match = SESSION_JSON_PATTERN.search(content)
    if not match:
        raise ValueError("Unable to locate sessions JSON block in course_info.txt")

    sessions = _parse_sessions_list(json.loads(match.group(0)), tz)
    return CourseData(
        title=title,
        sessions=sessions,
        qr_attendance_url=_find_url_after_keyword(
            lines, "QR code for marking attendance"
        ),
        attendance_check_url=_find_url_after_keyword(lines, "checking attendance"),
        carpark_info_url=_find_url_after_keyword(lines, "Carpark Charges"),
        materials_url=_find_url_after_keyword(lines, "Course Materials"),
    )


def load_course_data_json(content: str, tz: ZoneInfo) -> CourseData:
    data = json.loads(content)
    sessions_raw = data.get("sessions", [])
    if not isinstance(sessions_raw, list) or not sessions_raw:
        raise ValueError("JSON course data must include a non-empty 'sessions' list.")
    sessions = _parse_sessions_list(sessions_raw, tz)
    return CourseData(
        title=data.get("title", "Course"),
        sessions=sessions,
        qr_attendance_url=data.get("attendance_qr_url"),
        attendance_check_url=data.get("attendance_check_url"),
        carpark_info_url=data.get("carpark_info_url"),
        materials_url=data.get("materials_url"),
    )


def load_course_data(path: str, tz: ZoneInfo) -> CourseData:
    content = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() == ".json" or content.lstrip().startswith("{"):
        return load_course_data_json(content, tz)
    return load_course_data_legacy(content, tz)


def format_session_line(session: Session, tz: ZoneInfo) -> str:
    start_str = session.start.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    end_str = session.end.astimezone(tz).strftime("%H:%M")
    location = session.venue if session.venue else session.mode_location
    return (
        f"- {session.lecture} [{session.session_label}] | "
        f"{start_str} to {end_str} ({tz.key}) | {location}"
    )


def format_session_detail(session: Session, tz: ZoneInfo) -> str:
    lines = [
        f"{session.lecture} - {session.session_label}",
        f"Date & time: {session.start.astimezone(tz).strftime('%A, %Y-%m-%d %H:%M')} "
        f"to {session.end.astimezone(tz).strftime('%H:%M')} ({tz.key})",
        f"Mode/Location: {session.mode_location}",
    ]
    if session.venue:
        lines.append(f"Venue: {session.venue}")
    if session.zoom_link:
        lines.append(f"Zoom: {session.zoom_link}")
    if session.meeting_id:
        lines.append(f"Meeting ID: {session.meeting_id}")
    if session.passcode:
        lines.append(f"Passcode: {session.passcode}")
    if session.google_map:
        lines.append(f"Map: {session.google_map}")
    return "\n".join(lines)


def build_link_keyboard_with_options(
    session: Session,
    course: CourseData,
    include_zoom: bool = False,
    include_materials: bool = False,
    include_attendance: bool = True,
) -> Optional[InlineKeyboardMarkup]:
    rows: List[List[InlineKeyboardButton]] = []
    current_row: List[InlineKeyboardButton] = []
    mode_lower = session.mode_location.lower()
    is_online = bool(session.zoom_link) or "zoom" in mode_lower or "online" in mode_lower

    def add_button(text: str, url: Optional[str]) -> None:
        nonlocal current_row
        if not url:
            return
        current_row.append(InlineKeyboardButton(text, url=url))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []

    if include_zoom:
        add_button("Join Zoom", session.zoom_link)
    add_button("Map", session.google_map)
    if include_materials:
        add_button("Materials", course.materials_url)
    if not is_online:
        add_button("Carpark Info", course.carpark_info_url)
    if include_attendance:
        add_button("Attendance QR", course.qr_attendance_url)
        add_button("Attendance Check", course.attendance_check_url)

    if current_row:
        rows.append(current_row)

    return InlineKeyboardMarkup(rows) if rows else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: SubscriberStore = context.application.bot_data["store"]
    tz: ZoneInfo = context.application.bot_data["tz"]
    course: CourseData = context.application.bot_data["course_data"]

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    store.subscribe(user.id, chat.id)
    welcome = [
        f"Hi {user.first_name or 'there'}! You are subscribed to {course.title}.",
        "You'll get reminders 30 minutes before each session and when it ends.",
        "Commands: /next, /schedule, /info <lecture|date>, /stop to unsubscribe.",
        f"All times are shown in {tz.key}.",
    ]
    await update.message.reply_text("\n".join(welcome))


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: SubscriberStore = context.application.bot_data["store"]
    user = update.effective_user
    if not user:
        return
    store.unsubscribe(user.id)
    await update.message.reply_text("You have been unsubscribed from reminders.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    course: CourseData = context.application.bot_data["course_data"]
    tz: ZoneInfo = context.application.bot_data["tz"]
    lines = [
        f"This bot shares reminders for {course.title}.",
        "Commands:",
        "/start - subscribe to reminders",
        "/stop - unsubscribe",
        "/next - show the next upcoming session",
        "/schedule - list all sessions",
        "/materials - get course materials link",
        "/info - then enter a lecture or date (e.g., 'Lecture 3' or '2025-12-13')",
        f"Times are shown in {tz.key}.",
    ]
    await update.message.reply_text("\n".join(lines))


async def materials_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    course: CourseData = context.application.bot_data["course_data"]
    if course.materials_url:
        await update.message.reply_text(f"Course materials: {course.materials_url}")
    else:
        await update.message.reply_text("No materials link is configured.")


def find_sessions_by_query(course: CourseData, query: str) -> List[Session]:
    q = query.strip().lower()
    if not q:
        return []
    matches: List[Session] = []
    for session in course.sessions:
        if (
            q in session.lecture.lower()
            or q in session.session_label.lower()
            or q in session.display_date
        ):
            matches.append(session)
    return matches


async def info_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Which lecture or date do you want details for?\n"
        "Example: Lecture 3 or 2025-12-13\n"
        "Send /cancel to stop."
    )
    return INFO_QUERY


async def info_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    course: CourseData = context.application.bot_data["course_data"]
    tz: ZoneInfo = context.application.bot_data["tz"]
    query = (update.message.text or "").strip()
    matches = find_sessions_by_query(course, query)
    if not matches:
        await update.message.reply_text(
            "No matching sessions found. Try another lecture/date or send /cancel."
        )
        return INFO_QUERY
    lines = [format_session_detail(session, tz) for session in matches]
    # Use attendance buttons when replying from /info
    keyboard = build_link_keyboard_with_options(
        matches[0],
        course,
        include_zoom=False,
        include_materials=False,
        include_attendance=True,
    )
    await update.message.reply_text(
        "\n\n".join(lines),
        reply_markup=keyboard,
    )
    return ConversationHandler.END


async def info_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled. Send /info to search again.")
    return ConversationHandler.END


async def next_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    course: CourseData = context.application.bot_data["course_data"]
    tz: ZoneInfo = context.application.bot_data["tz"]
    now = datetime.now(tz)
    upcoming = [s for s in course.sessions if s.start >= now]
    if not upcoming:
        await update.message.reply_text("No upcoming sessions found.")
        return
    session = upcoming[0]
    message = format_session_detail(session, tz)
    keyboard = build_link_keyboard_with_options(
        session, course, include_zoom=False, include_materials=False, include_attendance=True
    )
    await update.message.reply_text(message, reply_markup=keyboard)


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    course: CourseData = context.application.bot_data["course_data"]
    tz: ZoneInfo = context.application.bot_data["tz"]
    lines = ["Upcoming sessions:"]
    current_date: Optional[str] = None
    for session in course.sessions:
        date_str = session.start.astimezone(tz).strftime("%Y-%m-%d (%A)")
        if date_str != current_date:
            if current_date is not None:
                lines.append("")
            lines.append(date_str)
            current_date = date_str
        lines.append(format_session_line(session, tz))
    await update.message.reply_text("\n".join(lines))


async def send_session_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    session_id: Optional[str] = data.get("session_id")
    kind: str = data.get("kind", "start")
    course: CourseData = context.application.bot_data["course_data"]
    session_map: Dict[str, Session] = context.application.bot_data["session_map"]
    tz: ZoneInfo = context.application.bot_data["tz"]
    store: SubscriberStore = context.application.bot_data["store"]
    dry_run: bool = context.application.bot_data.get("dry_run", False)

    session = session_map.get(session_id)
    if not session:
        LOG.warning("Session not found for reminder: %s", session_id)
        return

    if kind == "before":
        heading = f"{session.lecture} starts in 30 minutes"
    else:
        heading = f"{session.lecture} has ended"
    body = format_session_detail(session, tz)
    message = f"{heading}\n\n{body}"
    keyboard = build_link_keyboard_with_options(
        session, course, include_zoom=False, include_materials=False, include_attendance=True
    )

    chat_ids = store.active_chat_ids()
    if not chat_ids:
        LOG.info("No subscribers to notify for session %s", session_id)
        return

    for chat_id in chat_ids:
        try:
            if dry_run:
                LOG.info("[DRY-RUN] Would send reminder to %s: %s", chat_id, heading)
                continue
            await context.bot.send_message(
                chat_id=chat_id, text=message, reply_markup=keyboard
            )
        except Exception as exc:  # pylint: disable=broad-except
            LOG.warning("Failed to send reminder to %s: %s", chat_id, exc)


def clear_existing_reminders(application: Application) -> None:
    if not application.job_queue:
        return
    for job in application.job_queue.jobs():
        data = job.data if isinstance(job.data, dict) else {}
        if data.get("tag") == "session_reminder":
            job.schedule_removal()


def schedule_reminders(application: Application, course: CourseData, tz: ZoneInfo) -> None:
    if not application.job_queue:
        raise RuntimeError("Job queue is not available.")
    clear_existing_reminders(application)
    now = datetime.now(tz)
    for session in course.sessions:
        before_time = session.start - timedelta(minutes=30)
        if before_time > now:
            application.job_queue.run_once(
                send_session_reminder,
                when=before_time,
                data={"session_id": session.id, "kind": "before", "tag": "session_reminder"},
                name=f"{session.id}-before",
            )
        if session.end > now:
            application.job_queue.run_once(
                send_session_reminder,
                when=session.end,
                data={"session_id": session.id, "kind": "end", "tag": "session_reminder"},
                name=f"{session.id}-end",
            )
    LOG.info("Scheduled reminders for %d sessions", len(course.sessions))


def build_application(token: str, course: CourseData, store: SubscriberStore, tz: ZoneInfo, dry_run: bool) -> Application:
    application = (
        Application.builder()
        .token(token)
        .rate_limiter(AIORateLimiter())
        .build()
    )
    application.bot_data["course_data"] = course
    application.bot_data["session_map"] = {s.id: s for s in course.sessions}
    application.bot_data["store"] = store
    application.bot_data["tz"] = tz
    application.bot_data["dry_run"] = dry_run

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("next", next_session))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("materials", materials_command))
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("info", info_start)],
            states={INFO_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, info_query)]},
            fallbacks=[CommandHandler("cancel", info_cancel)],
        )
    )

    schedule_reminders(application, course, tz)
    return application


async def set_bot_commands(application: Application) -> None:
    commands = [
        BotCommand("materials", "Get course materials link"),
        BotCommand("next", "Show the next upcoming session"),
        BotCommand("schedule", "List all sessions"),
        BotCommand("info", "Look up a lecture or date"),
        BotCommand("help", "Show help and available commands"),
        BotCommand("start", "Subscribe to reminders"),
        BotCommand("stop", "Unsubscribe from reminders"),
    ]
    # Clear old command sets in case Telegram cached previous entries.
    await application.bot.delete_my_commands()
    await application.bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())

    # Set commands for default scope and private chats explicitly.
    await application.bot.set_my_commands(commands)
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())

    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    LOG.info(
        "Bot commands/menu updated; commands: %s",
        [c.command for c in commands],
    )


async def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO
    )
    load_dotenv()
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is required.")

    tz_name = os.environ.get("TIMEZONE", DEFAULT_TIMEZONE)
    tz = ZoneInfo(tz_name)

    course_path = os.environ.get("COURSE_FILE", "course_data.json")
    course_data = load_course_data(course_path, tz)

    store_path = os.environ.get("SUBSCRIBER_DB", "subscribers.db")
    store = SubscriberStore(store_path)

    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    app = build_application(token, course_data, store, tz, dry_run)
    LOG.info("Starting bot in %s (dry_run=%s)", tz.key, dry_run)
    await app.initialize()
    LOG.info("Setting bot commands and menu...")
    await set_bot_commands(app)
    await app.start()
    try:
        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
