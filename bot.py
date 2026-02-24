import os
import re
import json
import random
import sqlite3
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, CallbackQueryHandler,
    Filters, CallbackContext
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import threading

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = "8720485406:AAGGarhFS26QnNHlHXE-XdN9x_W5Hf7lFnM"
GROQ_API_KEY   = "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"
GROQ_URL       = "https://api.groq.com/openai/v1/chat/completions"
IST            = ZoneInfo("Asia/Kolkata")
DB_PATH        = os.path.expanduser("~/nova/nova.db")

# ─── AI CALL (direct HTTP — no groq library needed) ──────────────────────────
def ask_groq(messages):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": "llama3-70b-8192",
        "max_tokens": 500,
        "messages": messages
    }
    try:
        r = httpx.post(GROQ_URL, json=body, headers=headers, timeout=30)
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Hmm, something went wrong on my end. Try again in a sec! ({e})"

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id   INTEGER PRIMARY KEY,
        name      TEXT DEFAULT 'friend',
        xp        INTEGER DEFAULT 0,
        level     INTEGER DEFAULT 1,
        streak    INTEGER DEFAULT 0,
        mood      TEXT DEFAULT 'neutral',
        history   TEXT DEFAULT '[]'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER,
        text        TEXT,
        priority    TEXT DEFAULT 'medium',
        remind_at   TEXT,
        done        INTEGER DEFAULT 0,
        created_at  TEXT
    )""")
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"user_id": row[0], "name": row[1], "xp": row[2],
                "level": row[3], "streak": row[4], "mood": row[5],
                "history": json.loads(row[6])}
    return None

def upsert_user(user_id, name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO users (user_id, name) VALUES (?,?)
                 ON CONFLICT(user_id) DO UPDATE SET name=excluded.name""",
              (user_id, name))
    conn.commit()
    conn.close()

def update_user(user_id, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for key, val in kwargs.items():
        if key == "history":
            val = json.dumps(val)
        c.execute(f"UPDATE users SET {key}=? WHERE user_id=?", (val, user_id))
    conn.commit()
    conn.close()

def get_tasks(user_id, done=0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE user_id=? AND done=?", (user_id, done))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "user_id": r[1], "text": r[2], "priority": r[3],
             "remind_at": r[4], "done": r[5]} for r in rows]

def add_task_db(user_id, text, priority="medium", remind_at=None):
    now = datetime.now(IST).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (user_id, text, priority, remind_at, created_at) VALUES (?,?,?,?,?)",
              (user_id, text, priority, remind_at, now))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

def complete_task_db(task_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tasks SET done=1 WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, name FROM users")
    rows = c.fetchall()
    conn.close()
    return rows

def get_due_reminders():
    now_str = datetime.now(IST).strftime("%Y-%m-%dT%H:%M")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT t.id, t.user_id, t.text, t.priority, u.name
                 FROM tasks t JOIN users u ON t.user_id=u.user_id
                 WHERE t.done=0 AND t.remind_at IS NOT NULL
                 AND substr(t.remind_at,1,16) <= ?""", (now_str,))
    rows = c.fetchall()
    conn.close()
    return rows

def clear_reminder(task_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tasks SET remind_at=NULL WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def get_level_title(level):
    titles = ["Starter","Grinder","Focused","Warrior","Champion","Legend","Unstoppable","GOD MODE"]
    return titles[min(level-1, len(titles)-1)]

def parse_time(text):
    now = datetime.now(IST)
    t = text.lower()
    m = re.search(r'in (\d+)\s*(hour|hr|minute|min)', t)
    if m:
        val = int(m.group(1))
        if "hour" in m.group(2) or "hr" in m.group(2):
            return (now + timedelta(hours=val)).strftime("%Y-%m-%dT%H:%M")
        return (now + timedelta(minutes=val)).strftime("%Y-%m-%dT%H:%M")
    is_tomorrow = "tomorrow" in t
    base = now + timedelta(days=1) if is_tomorrow else now
    m = re.search(r'at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?', t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        period = m.group(3)
        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
        dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= now and not is_tomorrow:
            dt += timedelta(days=1)
        return dt.strftime("%Y-%m-%dT%H:%M")
    return None

SYSTEM_PROMPT = """You are NOVA — an elite AI life coach and task manager on Telegram.
- ALWAYS use the user's first name naturally in your responses
- Talk like a warm, brilliant friend — NOT a corporate chatbot
- Be direct, real, emotionally intelligent, casually funny
- Celebrate wins like you mean it
- Never say "Certainly!", "Of course!", "Sure thing!"
- Use emojis sparingly and meaningfully
- Keep responses to 2-4 short paragraphs max
- Occasionally use light Indian expressions (yaar, arre, etc.) when fitting
- Sound 100% human"""

def nova_reply(user_id, message):
    user = get_user(user_id)
    if not user:
        return "Hey! Send /start first so I know who you are 😊"
    history = user["history"]
    tasks = get_tasks(user_id)
    task_info = ""
    if tasks:
        task_info = "\nUser's pending tasks:\n" + "\n".join([f"- {t['text']}" for t in tasks])
    context = (f"{task_info}\nUser's name: {user['name']}, "
               f"Level {user['level']} ({get_level_title(user['level'])}), "
               f"{user['streak']}-day streak, {user['xp']} XP")
    messages = [{"role": "system", "content": SYSTEM_PROMPT + context}]
    messages += history[-10:]
    messages.append({"role": "user", "content": message})
    reply = ask_groq(messages)
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    if len(history) > 30:
        history = history[-30:]
    update_user(user_id, history=history)
    return reply

# ─── BOT — global reference for scheduler ────────────────────────────────────
bot_ref = None

# ─── SCHEDULED JOBS ──────────────────────────────────────────────────────────
def check_reminders():
    if not bot_ref:
        return
    for (task_id, user_id, text, priority, name) in get_due_reminders():
        try:
            reply = nova_reply(user_id, f"Remind {name} about this task: '{text}'. Be warm, use their name, 2 sentences.")
            emoji = {"high":"🔴","medium":"🟡","low":"🟢"}.get(priority,"🟡")
            bot_ref.send_message(chat_id=user_id,
                text=f"⏰ *Reminder, {name}!*\n\n{emoji} {text}\n\n{reply}",
                parse_mode="Markdown")
            clear_reminder(task_id)
        except Exception as e:
            print(f"Reminder error: {e}")

def morning_message():
    if not bot_ref:
        return
    for (user_id, name) in get_all_users():
        try:
            tasks = get_tasks(user_id)
            reply = nova_reply(user_id,
                f"It's 8 AM in India. Give {name} an energetic good morning. "
                f"They have {len(tasks)} tasks today. Use their name. Max 3 sentences.")
            keyboard = [[InlineKeyboardButton("📋 My Tasks", callback_data="view_tasks"),
                         InlineKeyboardButton("💪 Let's go!", callback_data="motivate")]]
            bot_ref.send_message(chat_id=user_id, text=f"🌅 {reply}",
                reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            print(f"Morning error: {e}")

def midday_message():
    if not bot_ref:
        return
    for (user_id, name) in get_all_users():
        try:
            tasks = get_tasks(user_id)
            if not tasks:
                continue
            reply = nova_reply(user_id,
                f"It's 1 PM in India. Give {name} a casual lunch-break check-in. "
                f"They have {len(tasks)} tasks. Keep it short like a friend texting. 2 sentences.")
            bot_ref.send_message(chat_id=user_id, text=f"☀️ {reply}")
        except Exception as e:
            print(f"Midday error: {e}")

def evening_message():
    if not bot_ref:
        return
    for (user_id, name) in get_all_users():
        try:
            tasks = get_tasks(user_id)
            reply = nova_reply(user_id,
                f"It's 9 PM in India. Give {name} a warm evening wind-down message. "
                f"They have {len(tasks)} tasks still pending. Encouraging, not guilt-tripping. 3 sentences.")
            bot_ref.send_message(chat_id=user_id, text=f"🌙 {reply}")
        except Exception as e:
            print(f"Evening error: {e}")

# ─── COMMAND HANDLERS ────────────────────────────────────────────────────────
def start(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    name = update.effective_user.first_name
    upsert_user(user_id, name)
    keyboard = [
        [InlineKeyboardButton("📋 My Tasks", callback_data="view_tasks"),
         InlineKeyboardButton("➕ Add Task", callback_data="prompt_add")],
        [InlineKeyboardButton("🏆 Progress", callback_data="progress"),
         InlineKeyboardButton("💪 Motivate Me", callback_data="motivate")],
        [InlineKeyboardButton("🌅 Check-in", callback_data="checkin"),
         InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    update.message.reply_text(
        f"Hey {name}! 👋 I'm *NOVA* — your personal AI coach.\n\n"
        f"I'll remind you of tasks in IST 🇮🇳, keep you motivated, and talk to you like a real friend.\n\n"
        f"What are we crushing today, {name}?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

def add_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    name = update.effective_user.first_name
    upsert_user(user_id, name)
    user = get_user(user_id)
    if not context.args:
        update.message.reply_text(
            f"Tell me the task, {name}!\n\n"
            "`/add Call dentist at 3pm`\n"
            "`/add Submit report tomorrow at 9am`\n"
            "`/add Buy groceries in 2 hours`",
            parse_mode="Markdown")
        return
    text = " ".join(context.args)
    priority = "medium"
    if any(w in text.lower() for w in ["urgent","asap","critical","important","must"]):
        priority = "high"
    elif any(w in text.lower() for w in ["maybe","someday","eventually","later"]):
        priority = "low"
    remind_at = parse_time(text)
    add_task_db(user_id, text, priority, remind_at)
    update_user(user_id, xp=user["xp"]+10)
    tasks = get_tasks(user_id)
    emoji = {"high":"🔴","medium":"🟡","low":"🟢"}[priority]
    reminder_line = ""
    if remind_at:
        dt = datetime.fromisoformat(remind_at)
        reminder_line = f"\n⏰ Reminder set: *{dt.strftime('%d %b, %I:%M %p IST')}*"
    else:
        reminder_line = "\n_(Add 'at 5pm' or 'tomorrow at 9am' for a reminder)_"
    update.message.reply_text(
        f"✅ Got it, {name}!\n\n{emoji} *{text}*{reminder_line}\n\n"
        f"You've got {len(tasks)} task(s) active 💪",
        parse_mode="Markdown")

def tasks_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        update.message.reply_text("Use /start first!")
        return
    tasks = get_tasks(user_id)
    if not tasks:
        update.message.reply_text(
            f"List is empty, {user['name']} 🎉\n\nUse `/add <task>` to add goals!",
            parse_mode="Markdown")
        return
    emoji = {"high":"🔴","medium":"🟡","low":"🟢"}
    order = {"high":0,"medium":1,"low":2}
    sorted_tasks = sorted(tasks, key=lambda t: order.get(t["priority"],1))
    msg = f"📋 *{user['name']}'s Tasks* ({len(sorted_tasks)} active)\n\n"
    for t in sorted_tasks:
        rem = ""
        if t["remind_at"]:
            dt = datetime.fromisoformat(t["remind_at"])
            rem = f" ⏰ {dt.strftime('%d %b %I:%M %p')}"
        msg += f"{emoji.get(t['priority'],'🟡')} `#{t['id']}` {t['text']}{rem}\n"
    done = get_tasks(user_id, done=1)
    msg += f"\n✅ Completed: {len(done)}\nUse `/done <id>` to check off"
    update.message.reply_text(msg, parse_mode="Markdown")

def done_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        update.message.reply_text("Use /start first!")
        return
    if not context.args:
        update.message.reply_text(f"Which task, {user['name']}? Use `/done <id>`", parse_mode="Markdown")
        return
    try:
        task_id = int(context.args[0])
    except:
        update.message.reply_text("Give me the task number like `/done 1`", parse_mode="Markdown")
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id=? AND user_id=? AND done=0", (task_id, user_id))
    row = c.fetchone()
    conn.close()
    if not row:
        update.message.reply_text(f"Can't find task #{task_id}, {user['name']}. Check `/tasks`", parse_mode="Markdown")
        return
    task = {"text": row[2], "priority": row[3]}
    complete_task_db(task_id)
    xp_gain = {"high":50,"medium":30,"low":15}.get(task["priority"],30)
    new_xp = user["xp"] + xp_gain
    new_streak = user["streak"] + 1
    new_level = 1 + new_xp // 200
    leveled_up = new_level > user["level"]
    update_user(user_id, xp=new_xp, streak=new_streak, level=new_level)
    msgs = [
        f"That's what I'm talking about, {user['name']}! 🔥",
        f"Let's go {user['name']}! You're on a roll 🚀",
        f"Nailed it, {user['name']} ⚡",
        f"YES {user['name']}! Another one down 💪",
        f"Done. You're making it look easy, {user['name']} 😤"
    ]
    msg = (f"{random.choice(msgs)}\n\n✅ *{task['text']}*\n"
           f"+{xp_gain} XP  |  🔥 {new_streak}-day streak")
    if leveled_up:
        msg += f"\n\n🎉 *LEVEL UP, {user['name']}!* You're Level {new_level} — _{get_level_title(new_level)}_ 🏆"
    update.message.reply_text(msg, parse_mode="Markdown")

def remind_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    name = update.effective_user.first_name
    upsert_user(user_id, name)
    if not context.args:
        update.message.reply_text(
            f"What should I remind you about, {name}?\n\n"
            "`/remind Call mom at 7pm`\n`/remind Meeting tomorrow at 10am`",
            parse_mode="Markdown")
        return
    text = " ".join(context.args)
    remind_at = parse_time(text)
    if not remind_at:
        update.message.reply_text(
            f"Didn't catch a time, {name} 🤔\nTry: `/remind Call mom at 7pm`",
            parse_mode="Markdown")
        return
    add_task_db(user_id, text, "medium", remind_at)
    dt = datetime.fromisoformat(remind_at)
    update.message.reply_text(
        f"⏰ Done, {name}!\n\n*{text}*\n\n📅 {dt.strftime('%d %b %Y, %I:%M %p IST')}",
        parse_mode="Markdown")

def progress_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        update.message.reply_text("Use /start first!")
        return
    xp_in = user["xp"] % 200
    bar = "█" * int(xp_in/20) + "░" * (10 - int(xp_in/20))
    tasks = get_tasks(user_id)
    done = get_tasks(user_id, done=1)
    update.message.reply_text(
        f"🏆 *{user['name']}'s Progress*\n\n"
        f"👤 Level {user['level']} — _{get_level_title(user['level'])}_\n"
        f"⚡ XP: {user['xp']} total\n[{bar}] {xp_in}/200\n\n"
        f"🔥 Streak: {user['streak']} days\n"
        f"✅ Done: {len(done)} tasks\n📋 Active: {len(tasks)} tasks",
        parse_mode="Markdown")

def motivate_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        update.message.reply_text("Use /start first!")
        return
    tasks = get_tasks(user_id)
    reply = nova_reply(user_id,
        f"Give {user['name']} a powerful personal motivational message. "
        f"They have {len(tasks)} tasks and a {user['streak']}-day streak. "
        f"Use their name. Real and human. 3-4 sentences.")
    update.message.reply_text(reply)

def checkin_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        update.message.reply_text("Use /start first!")
        return
    hour = datetime.now(IST).hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
    keyboard = [
        [InlineKeyboardButton("🔥 Energized", callback_data="mood_great"),
         InlineKeyboardButton("😐 Meh", callback_data="mood_neutral")],
        [InlineKeyboardButton("😴 Tired", callback_data="mood_tired"),
         InlineKeyboardButton("😰 Overwhelmed", callback_data="mood_stressed")]
    ]
    update.message.reply_text(
        f"{greeting}, *{user['name']}*! How are you feeling? 💭",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown")

def clear_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    user = get_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE user_id=? AND done=0", (user_id,))
    count = c.rowcount
    conn.commit()
    conn.close()
    name = user["name"] if user else "friend"
    update.message.reply_text(f"Cleared {count} tasks, {name}. Fresh start! 🧹")

def help_cmd(update: Update, context: CallbackContext):
    user = get_user(update.effective_user.id)
    name = user["name"] if user else "friend"
    update.message.reply_text(
        f"🤖 *NOVA Commands, {name}*\n\n"
        "`/add <task>` — Add task (include time!)\n"
        "`/tasks` — View all tasks\n"
        "`/done <id>` — Complete a task\n"
        "`/remind <task> at <time>` — Set reminder\n"
        "`/clear` — Clear all tasks\n"
        "`/progress` — Your XP & level\n"
        "`/checkin` — Mood check-in\n"
        "`/motivate` — Get motivated\n\n"
        "*⏰ Time formats (IST):*\n"
        "`at 9pm` · `at 9:30am` · `tomorrow at 10am` · `in 2 hours`\n\n"
        "*Daily messages:*\n"
        "🌅 8AM · ☀️ 1PM · 🌙 9PM\n\n"
        "Or just *talk to me naturally!* 💬",
        parse_mode="Markdown")

def handle_message(update: Update, context: CallbackContext):
    text = update.message.text
    user_id = update.effective_user.id
    name = update.effective_user.first_name
    upsert_user(user_id, name)
    user = get_user(user_id)
    lower = text.lower()
    triggers = ["remind me to","remind me about","i need to","i have to",
                "don't forget to","add task","need to","have to"]
    for trigger in triggers:
        if trigger in lower:
            task_text = lower.split(trigger, 1)[1].strip().capitalize()
            remind_at = parse_time(text)
            add_task_db(user_id, task_text, "medium", remind_at)
            update_user(user_id, xp=user["xp"]+10)
            rem = ""
            if remind_at:
                dt = datetime.fromisoformat(remind_at)
                rem = f"\n⏰ Reminder: {dt.strftime('%d %b, %I:%M %p IST')}"
            reply = nova_reply(user_id,
                f"I just added '{task_text}' for {name}. Acknowledge briefly and encourage them.")
            update.message.reply_text(
                f"📝 Added, {name}!\n*{task_text}*{rem}\n\n{reply}",
                parse_mode="Markdown")
            return
    context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = nova_reply(user_id, text)
    update.message.reply_text(reply)

def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    name = query.from_user.first_name
    upsert_user(user_id, name)
    user = get_user(user_id)
    data = query.data

    if data == "view_tasks":
        tasks = get_tasks(user_id)
        if not tasks:
            query.message.reply_text(f"No tasks yet, {user['name']}! Use `/add <task>` 🎯", parse_mode="Markdown")
        else:
            emoji = {"high":"🔴","medium":"🟡","low":"🟢"}
            msg = f"📋 *{user['name']}'s Tasks* ({len(tasks)} active)\n\n"
            for t in tasks:
                rem = ""
                if t["remind_at"]:
                    dt = datetime.fromisoformat(t["remind_at"])
                    rem = f" ⏰ {dt.strftime('%d %b %I:%M %p')}"
                msg += f"{emoji.get(t['priority'],'🟡')} `#{t['id']}` {t['text']}{rem}\n"
            msg += "\nUse `/done <id>` to mark complete"
            query.message.reply_text(msg, parse_mode="Markdown")
    elif data == "prompt_add":
        query.message.reply_text(
            f"Use `/add <task>` — include a time like `at 5pm` for reminders! ⏰",
            parse_mode="Markdown")
    elif data == "progress":
        xp_in = user["xp"] % 200
        bar = "█" * int(xp_in/20) + "░" * (10 - int(xp_in/20))
        tasks = get_tasks(user_id)
        done = get_tasks(user_id, done=1)
        query.message.reply_text(
            f"🏆 *{user['name']}'s Progress*\n\n"
            f"👤 Level {user['level']} — _{get_level_title(user['level'])}_\n"
            f"⚡ XP: {user['xp']}\n[{bar}] {xp_in}/200\n\n"
            f"🔥 Streak: {user['streak']} days\n✅ Done: {len(done)}\n📋 Active: {len(tasks)}",
            parse_mode="Markdown")
    elif data == "motivate":
        tasks = get_tasks(user_id)
        reply = nova_reply(user_id,
            f"Give {user['name']} a powerful motivational message. "
            f"{len(tasks)} tasks pending. {user['streak']}-day streak. Use their name. 3 sentences.")
        query.message.reply_text(reply)
    elif data == "checkin":
        hour = datetime.now(IST).hour
        greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
        keyboard = [
            [InlineKeyboardButton("🔥 Energized", callback_data="mood_great"),
             InlineKeyboardButton("😐 Meh", callback_data="mood_neutral")],
            [InlineKeyboardButton("😴 Tired", callback_data="mood_tired"),
             InlineKeyboardButton("😰 Overwhelmed", callback_data="mood_stressed")]
        ]
        query.message.reply_text(
            f"{greeting}, *{user['name']}*! How are you feeling? 💭",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown")
    elif data == "help":
        query.message.reply_text("Use /help to see all commands 📖")
    elif data.startswith("mood_"):
        mood = data.split("_")[1]
        update_user(user_id, mood=mood)
        tasks = get_tasks(user_id)
        prompts = {
            "great":   f"{user['name']} is energized! Channel that energy into {len(tasks)} tasks. Use their name.",
            "neutral": f"{user['name']} is feeling okay. Help them get productive. Grounded tone. Use their name.",
            "tired":   f"{user['name']} is tired. Be understanding, suggest starting small. No toxic positivity. Use their name.",
            "stressed": f"{user['name']} is overwhelmed with {len(tasks)} tasks. Be calm, reassuring. Use their name."
        }
        reply = nova_reply(user_id, prompts[mood])
        query.message.reply_text(reply)

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    global bot_ref
    init_db()

    updater = Updater(TELEGRAM_TOKEN)
    dp = updater.dispatcher
    bot_ref = updater.bot

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("add", add_cmd))
    dp.add_handler(CommandHandler("tasks", tasks_cmd))
    dp.add_handler(CommandHandler("done", done_cmd))
    dp.add_handler(CommandHandler("remind", remind_cmd))
    dp.add_handler(CommandHandler("progress", progress_cmd))
    dp.add_handler(CommandHandler("motivate", motivate_cmd))
    dp.add_handler(CommandHandler("checkin", checkin_cmd))
    dp.add_handler(CommandHandler("clear", clear_cmd))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CallbackQueryHandler(button_handler))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    # Scheduler (IST times)
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(check_reminders, "interval", minutes=1)
    scheduler.add_job(morning_message, CronTrigger(hour=8, minute=0, timezone=IST))
    scheduler.add_job(midday_message,  CronTrigger(hour=13, minute=0, timezone=IST))
    scheduler.add_job(evening_message, CronTrigger(hour=21, minute=0, timezone=IST))
    scheduler.start()

    print("🚀 NOVA is online!")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
