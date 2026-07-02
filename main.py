import os
import json
import random
import string
import logging
import io
from datetime import datetime, timezone, timedelta

import firebase_admin
from firebase_admin import credentials, db
import openpyxl
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# ──────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(levelname)s │ %(name)s │ %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  Environment Variables
# ──────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"]
ADMIN_ID         = int(os.environ["ADMIN_ID"])
FIREBASE_CONFIG  = os.environ["FIREBASE_CONFIG"]       # JSON string of service account
DATABASE_URL     = os.environ["FIREBASE_DATABASE_URL"]

# ──────────────────────────────────────────────
#  Firebase Init
# ──────────────────────────────────────────────
_cred_dict = json.loads(FIREBASE_CONFIG)
cred = credentials.Certificate(_cred_dict)
firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})

# ──────────────────────────────────────────────
#  Firebase Helpers
# ──────────────────────────────────────────────
def fb_get(path: str):
    return db.reference(path).get()

def fb_set(path: str, value):
    db.reference(path).set(value)

def fb_update(path: str, value: dict):
    db.reference(path).update(value)

def fb_push(path: str, value) -> str:
    ref = db.reference(path).push(value)
    return ref.key

def fb_delete(path: str):
    db.reference(path).delete()

# ──────────────────────────────────────────────
#  Helper: get / create user
# ──────────────────────────────────────────────
def get_user(uid: int) -> dict:
    data = fb_get(f"users/{uid}")
    return data or {}

def ensure_user(uid: int, full_name: str, username: str) -> dict:
    data = get_user(uid)
    if not data:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        data = {
            "uid": uid,
            "name": full_name,
            "username": username or "",
            "balance": 0.0,
            "joined_at": now_str,
            "tasks_approved": 0,
            "tasks_rejected": 0,
            "tasks_pending": 0,
            "referrer": None,
            "referral_count": 0,
            "referral_earned": 0.0,
        }
        fb_set(f"users/{uid}", data)
    return data

# ──────────────────────────────────────────────
#  Helper: global settings
# ──────────────────────────────────────────────
DEFAULT_PRICE    = 0.0350
DEFAULT_PASSWORD = "Ax@1234"
REFERRAL_PCT     = 0.08

def get_setting(key: str, default):
    val = fb_get(f"settings/{key}")
    return val if val is not None else default

def set_setting(key: str, value):
    fb_set(f"settings/{key}", value)

def get_price()    -> float: return float(get_setting("price", DEFAULT_PRICE))
def get_password() -> str:   return str(get_setting("password", DEFAULT_PASSWORD))

# ──────────────────────────────────────────────
#  Helper: total users count
# ──────────────────────────────────────────────
def total_users() -> int:
    users = fb_get("users") or {}
    return len(users)

# ──────────────────────────────────────────────
#  Helper: random name generator
# ──────────────────────────────────────────────
FIRST_NAMES = [
    "Alex","Jordan","Morgan","Taylor","Casey","Jamie","Drew","Riley",
    "Avery","Quinn","Blake","Cameron","Dana","Emery","Finley","Hayden",
    "Jesse","Kendall","Logan","Mackenzie","Noah","Parker","Reese","Sage",
    "Skyler","Sydney","Tatum","Tristan","Wynne","Zion"
]
LAST_NAMES = [
    "Smith","Johnson","Brown","Williams","Jones","Garcia","Martinez",
    "Davis","Lopez","Wilson","Moore","Taylor","Anderson","Thomas",
    "Jackson","White","Harris","Martin","Thompson","Young","Lewis",
    "Walker","Hall","Allen","Young","King","Wright","Scott","Green"
]

def random_name():
    fn = random.choice(FIRST_NAMES)
    ln = random.choice(LAST_NAMES)
    return fn, ln

# ──────────────────────────────────────────────
#  Helper: mono‑spaced number/ID format
# ──────────────────────────────────────────────
def mono(value) -> str:
    return f"`{value}`"

# ──────────────────────────────────────────────
#  Keyboards
# ──────────────────────────────────────────────
MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📬 Dashboard")],
        [KeyboardButton("💰 Balance"), KeyboardButton("📋 Tasks")],
        [KeyboardButton("👥 Invite Friends"), KeyboardButton("🏆 Leaderboard")],
        [KeyboardButton("📥 Withdraw"), KeyboardButton("👤 Profile")],
        [KeyboardButton("☎️ Support")],
    ],
    resize_keyboard=True,
)

def task_menu(price: float) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(f"🌟 Create Facebook - ${price:.4f}")]],
        resize_keyboard=True,
    )

def start_cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("▶️ Start")], [KeyboardButton("Cancel ❌")]],
        resize_keyboard=True,
    )

def confirm_cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Account Registered ✅")], [KeyboardButton("Cancel ❌")]],
        resize_keyboard=True,
    )

def withdraw_method_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("💎 USDT-BEP20")], [KeyboardButton("Cancel ❌")]],
        resize_keyboard=True,
    )

# ──────────────────────────────────────────────
#  State Keys (stored in context.user_data)
# ──────────────────────────────────────────────
STATE_KEY = "state"
STATES = {
    "IDLE":              "idle",
    "TASK_EMAIL":        "task_email",
    "TASK_CONFIRM":      "task_confirm",
    "WITHDRAW_METHOD":   "withdraw_method",
    "WITHDRAW_ADDRESS":  "withdraw_address",
    "WITHDRAW_AMOUNT":   "withdraw_amount",
}

# ──────────────────────────────────────────────
#  /start
# ──────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    uid    = user.id
    name   = user.full_name or "Friend"
    uname  = f"@{user.username}" if user.username else "N/A"

    # Handle referral
    referrer_id = None
    if ctx.args:
        try:
            referrer_id = int(ctx.args[0])
            if referrer_id == uid:
                referrer_id = None
        except ValueError:
            referrer_id = None

    existing = get_user(uid)
    is_new   = not existing

    udata = ensure_user(uid, name, uname)

    # Save referrer only if new user
    if is_new and referrer_id:
        ref_data = get_user(referrer_id)
        if ref_data:
            fb_update(f"users/{uid}", {"referrer": referrer_id})
            old_count = ref_data.get("referral_count", 0)
            fb_update(f"users/{referrer_id}", {"referral_count": old_count + 1})

    welcome = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💼  Welcome Back\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n\n"
        f"Hello, {name} 👋\n\n"
        "Complete tasks and earn real money.\n"
        "Use the menu below to get started.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(welcome, reply_markup=MAIN_MENU)

    # Notify admin of new user
    if is_new:
        total = total_users()
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        admin_msg = (
            "🔔 *New User Joined!*\n\n"
            f"👤 Name:      `{name}`\n"
            f"🆔 ID:          `{uid}`\n"
            f"📛 Username: `{uname}`\n"
            f"🕐 Time:       `{now_str}`\n\n"
            f"👥 Total Users: `{total}`"
        )
        try:
            await ctx.bot.send_message(
                ADMIN_ID, admin_msg, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.warning(f"Admin notify failed: {e}")

    ctx.user_data[STATE_KEY] = STATES["IDLE"]

# ──────────────────────────────────────────────
#  📬 Dashboard
# ──────────────────────────────────────────────
async def handle_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    udata = get_user(uid)
    if not udata:
        await update.message.reply_text("⚠️ Please send /start first.")
        return

    balance   = udata.get("balance", 0.0)
    approved  = udata.get("tasks_approved", 0)
    rejected  = udata.get("tasks_rejected", 0)
    pending   = udata.get("tasks_pending", 0)

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📬  Dashboard\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰  Balance:         `${balance:.4f}`\n\n"
        "📋  Task Overview\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⏳  Pending:         `{pending}`\n"
        f"✅  Approved:       `{approved}`\n"
        f"❌  Rejected:         `{rejected}`\n\n"
        f"📊  Total Submitted: `{approved + rejected + pending}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 Keep completing tasks to grow your earnings!"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_MENU)

# ──────────────────────────────────────────────
#  💰 Balance
# ──────────────────────────────────────────────
async def handle_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    udata = get_user(uid)
    if not udata:
        await update.message.reply_text("⚠️ Please send /start first.")
        return

    balance = udata.get("balance", 0.0)
    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💰  Your Balance\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💵  Available Balance\n"
        f"       `${balance:.4f}`\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📥 Withdraw anytime from the main menu.\n"
        "📋 Complete more tasks to increase balance!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_MENU)

# ──────────────────────────────────────────────
#  📋 Tasks
# ──────────────────────────────────────────────
async def handle_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    udata = get_user(uid)
    if not udata:
        await update.message.reply_text("⚠️ Please send /start first.")
        return

    price = get_price()
    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋  Available Tasks\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎯 Choose a task below to start earning!\n\n"
        f"🌟  *Facebook Account Creation*\n"
        f"     💵 Reward: `${price:.4f}`\n"
        f"     ⏰ Review Time: `1 hour`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap the task button below to proceed."
    )
    await update.message.reply_text(
        msg, parse_mode=ParseMode.MARKDOWN, reply_markup=task_menu(price)
    )
    ctx.user_data[STATE_KEY] = STATES["IDLE"]

# ──────────────────────────────────────────────
#  Task: overview → start
# ──────────────────────────────────────────────
async def handle_task_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    price = get_price()

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌟  Task Overview\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌  *Task:*          Facebook Account Creation\n"
        f"💵  *Reward:*      `${price:.4f}` USD\n"
        f"⏰  *Review Time:* `1 hour`\n"
        f"📊  *Status:*       Available ✅\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📝  *Instructions:*\n"
        "  1️⃣ Use the credentials provided\n"
        "  2️⃣ Register the Facebook account\n"
        "  3️⃣ Submit your email or phone number\n"
        "  4️⃣ Wait for admin approval ✅\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Press *▶️ Start* to begin the task."
    )
    await update.message.reply_text(
        msg, parse_mode=ParseMode.MARKDOWN, reply_markup=start_cancel_menu()
    )
    ctx.user_data[STATE_KEY] = "task_overview"

async def handle_task_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fn, ln   = random_name()
    password = get_password()

    ctx.user_data["task_fn"] = fn
    ctx.user_data["task_ln"] = ln
    ctx.user_data[STATE_KEY] = STATES["TASK_EMAIL"]

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📝  Task Credentials\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Use these details to register the Facebook account:\n\n"
        f"👤  First Name : `{fn}`\n"
        f"👤  Last Name  : `{ln}`\n"
        f"🔐  Password   : `{password}`\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📧  *Send Your Email or Phone Number:*\n\n"
        "⚠️  Make sure to use a valid & unused email/number."
    )
    await update.message.reply_text(
        msg, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
    )

async def handle_task_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    email = update.message.text.strip()
    ctx.user_data["task_email"] = email
    ctx.user_data[STATE_KEY] = STATES["TASK_CONFIRM"]

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅  Submission Preview\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📧  Email/Number: `{email}`\n\n"
        "🔍 Please confirm your submission.\n"
        "If everything looks good, tap ✅ below.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(
        msg, parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_cancel_menu()
    )

async def handle_task_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    udata    = get_user(uid)
    name     = udata.get("name", "Unknown")
    uname    = udata.get("username", "N/A")
    fn       = ctx.user_data.get("task_fn", "")
    ln       = ctx.user_data.get("task_ln", "")
    email    = ctx.user_data.get("task_email", "")
    price    = get_price()
    password = get_password()
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Save submission
    sub_key = fb_push("submissions", {
        "uid":       uid,
        "name":      name,
        "username":  uname,
        "first_name": fn,
        "last_name":  ln,
        "password":  password,
        "email":     email,
        "price":     price,
        "status":    "pending",
        "submitted_at": now_str,
    })

    # Save email to emails node
    fb_push("emails", {"email": email, "uid": uid, "submitted_at": now_str})

    # Update user pending count
    old_pending = udata.get("tasks_pending", 0)
    fb_update(f"users/{uid}", {"tasks_pending": old_pending + 1})

    ctx.user_data[STATE_KEY] = STATES["IDLE"]

    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎉  Submission Successful!\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅  Your task has been submitted.\n"
        "⏳  Admin will review it within *1 hour*.\n"
        "💬  You'll receive a notification once reviewed.\n\n"
        "🙏 Thank you for your effort!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=MAIN_MENU,
    )

    # Notify admin
    admin_msg = (
        "📥 *New Task Submission!*\n\n"
        f"👤  Name:       `{name}`\n"
        f"🆔  ID:           `{uid}`\n"
        f"📛  Username: `{uname}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤  First Name: `{fn}`\n"
        f"👤  Last Name:  `{ln}`\n"
        f"🔐  Password:   `{password}`\n"
        f"📧  Email:         `{email}`\n"
        f"💵  Reward:      `${price:.4f}`\n"
        f"🕐  Time:          `{now_str}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔑  Sub Key: `{sub_key}`"
    )
    try:
        await ctx.bot.send_message(ADMIN_ID, admin_msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"Admin notify failed: {e}")

# ──────────────────────────────────────────────
#  👥 Invite Friends
# ──────────────────────────────────────────────
async def handle_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    udata = get_user(uid)
    if not udata:
        await update.message.reply_text("⚠️ Please send /start first.")
        return

    bot_info   = await ctx.bot.get_me()
    bot_uname  = bot_info.username
    ref_link   = f"https://t.me/{bot_uname}?start={uid}"
    ref_count  = udata.get("referral_count", 0)
    ref_earned = udata.get("referral_earned", 0.0)
    price      = get_price()
    bonus      = round(price * REFERRAL_PCT, 4)

    # Count invites in last 24h
    invites_24h = 0
    users_all   = fb_get("users") or {}
    now_dt      = datetime.now(timezone.utc)
    for _, u in users_all.items():
        if str(u.get("referrer")) == str(uid):
            try:
                joined = datetime.strptime(u["joined_at"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                if (now_dt - joined).total_seconds() < 86400:
                    invites_24h += 1
            except Exception:
                pass

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👥  Invite Friends\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊  REFERRAL STATS\n"
        f"Total Invites      `{ref_count}`\n"
        f"New (last 24h)   `{invites_24h}`\n"
        f"Total Earned       `${ref_earned:.4f}`\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "💡  HOW IT WORKS\n"
        "Share your link with friends.\n"
        "When they complete a task,\n"
        f"you earn *{int(REFERRAL_PCT*100)}%* of their reward.\n\n"
        f"Bonus per task:  `${bonus:.4f}`\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🔗  YOUR REFERRAL LINK\n"
        f"`{ref_link}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_MENU)

# ──────────────────────────────────────────────
#  🏆 Leaderboard
# ──────────────────────────────────────────────
async def handle_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🏆  Leaderboard\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📴 The leaderboard is currently *offline*.\n\n"
        "Please check back later.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_MENU)

# ──────────────────────────────────────────────
#  📥 Withdraw
# ──────────────────────────────────────────────
async def handle_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    udata = get_user(uid)
    if not udata:
        await update.message.reply_text("⚠️ Please send /start first.")
        return

    balance = udata.get("balance", 0.0)
    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📥  Withdraw Funds\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰  Available Balance: `${balance:.4f}`\n"
        f"📌  Minimum Withdraw:  `$0.50`\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Select your withdrawal method below 👇"
    )
    await update.message.reply_text(
        msg, parse_mode=ParseMode.MARKDOWN, reply_markup=withdraw_method_menu()
    )
    ctx.user_data[STATE_KEY] = STATES["WITHDRAW_METHOD"]

async def handle_withdraw_usdt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data[STATE_KEY] = STATES["WITHDRAW_ADDRESS"]
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💎  USDT-BEP20 Withdrawal\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋  Please enter your *USDT-BEP20 wallet address*:\n\n"
        "⚠️  Double-check your address — wrong address = lost funds!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )

async def handle_withdraw_address(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    ctx.user_data["wd_address"] = address
    ctx.user_data[STATE_KEY]    = STATES["WITHDRAW_AMOUNT"]

    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💵  Enter Withdrawal Amount\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋  Wallet: `{address}`\n\n"
        "💲 Enter the amount you want to withdraw:\n"
        "_(Minimum: $0.50)_\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.MARKDOWN,
    )

async def handle_withdraw_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    udata   = get_user(uid)
    balance = udata.get("balance", 0.0)
    address = ctx.user_data.get("wd_address", "")
    name    = udata.get("name", "")
    uname   = udata.get("username", "N/A")

    try:
        amount = float(update.message.text.strip().replace("$", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Invalid amount. Please enter a number like `0.50`.",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    if amount < 0.50:
        await update.message.reply_text(
            "❌ Minimum withdrawal is `$0.50`. Please enter a higher amount.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if amount > balance:
        await update.message.reply_text(
            f"❌ Insufficient balance.\n"
            f"Available: `${balance:.4f}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fb_push("withdrawals", {
        "uid":        uid,
        "name":       name,
        "username":   uname,
        "method":     "USDT-BEP20",
        "address":    address,
        "amount":     amount,
        "status":     "pending",
        "requested_at": now_str,
    })

    ctx.user_data[STATE_KEY] = STATES["IDLE"]

    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅  Withdrawal Request Submitted!\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💎  Method:  `USDT-BEP20`\n"
        f"📋  Address: `{address}`\n"
        f"💵  Amount:  `${amount:.4f}`\n\n"
        "⏳  Admin will process it shortly.\n"
        "💬  You'll be notified once done!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=MAIN_MENU,
    )

    # Notify admin
    try:
        await ctx.bot.send_message(
            ADMIN_ID,
            "💸 *New Withdrawal Request!*\n\n"
            f"👤  Name:       `{name}`\n"
            f"🆔  ID:           `{uid}`\n"
            f"📛  Username: `{uname}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💎  Method:   `USDT-BEP20`\n"
            f"📋  Address:  `{address}`\n"
            f"💵  Amount:   `${amount:.4f}`\n"
            f"🕐  Time:        `{now_str}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning(f"Admin withdrawal notify failed: {e}")

# ──────────────────────────────────────────────
#  👤 Profile
# ──────────────────────────────────────────────
async def handle_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user  = update.effective_user
    uid   = user.id
    udata = get_user(uid)
    if not udata:
        await update.message.reply_text("⚠️ Please send /start first.")
        return

    name    = udata.get("name", "N/A")
    uname   = udata.get("username", "N/A")
    joined  = udata.get("joined_at", "N/A")
    balance = udata.get("balance", 0.0)
    approved = udata.get("tasks_approved", 0)
    rejected = udata.get("tasks_rejected", 0)
    pending  = udata.get("tasks_pending", 0)

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👤  Your Profile\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🧑  Name:        `{name}`\n"
        f"🆔  User ID:      `{uid}`\n"
        f"📛  Username:   `{uname}`\n"
        f"📅  Joined:        `{joined}`\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰  Balance:      `${balance:.4f}`\n"
        f"✅  Approved:    `{approved}`\n"
        f"❌  Rejected:      `{rejected}`\n"
        f"⏳  Pending:      `{pending}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_MENU)

# ──────────────────────────────────────────────
#  ☎️ Support
# ──────────────────────────────────────────────
async def handle_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📞  Support\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤝 Our team is here to help you.\n\n"
        "Contact us for:\n"
        "• Account or task issues\n"
        "• Rejected submission queries\n"
        "• Withdrawal problems\n"
        "• Any other questions\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📌  Admin Contacts:\n"
        "@axWorker_Admin\n"
        "@axWorker_Admin\n\n"
        "⏰  Response time: within a few hours\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, reply_markup=MAIN_MENU)

# ──────────────────────────────────────────────
#  Cancel (universal)
# ──────────────────────────────────────────────
async def handle_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data[STATE_KEY] = STATES["IDLE"]
    await update.message.reply_text(
        "✅ Action cancelled. Back to main menu.",
        reply_markup=MAIN_MENU,
    )

# ──────────────────────────────────────────────
#  ─── ADMIN COMMANDS ───────────────────────────
# ──────────────────────────────────────────────

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

# ──────────────────────────────────────────────
#  /sub — review submissions with inline buttons
# ──────────────────────────────────────────────
async def cmd_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    subs = fb_get("submissions") or {}
    pending = [(k, v) for k, v in subs.items() if v.get("status") == "pending"]

    if not pending:
        await update.message.reply_text("📭 No pending submissions right now.")
        return

    # Store list in admin context
    ctx.user_data["sub_list"]  = pending
    ctx.user_data["sub_index"] = 0

    await send_sub_card(update, ctx, edit=False)

async def send_sub_card(update_or_query, ctx: ContextTypes.DEFAULT_TYPE, edit=False):
    pending = ctx.user_data.get("sub_list", [])
    idx     = ctx.user_data.get("sub_index", 0)

    if not pending:
        text = "📭 No more pending submissions."
        if edit:
            await update_or_query.edit_message_text(text)
        else:
            await update_or_query.message.reply_text(text)
        return

    idx  = max(0, min(idx, len(pending) - 1))
    k, v = pending[idx]

    uid   = v.get("uid")
    name  = v.get("name", "N/A")
    uname = v.get("username", "N/A")
    fn    = v.get("first_name", "")
    ln    = v.get("last_name", "")
    pwd   = v.get("password", "")
    email = v.get("email", "")
    price = v.get("price", 0.0)
    t     = v.get("submitted_at", "N/A")

    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋  Submission {idx+1}/{len(pending)}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤  Name:         `{name}`\n"
        f"🆔  ID:             `{uid}`\n"
        f"📛  Username:   `{uname}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤  First Name: `{fn}`\n"
        f"👤  Last Name:  `{ln}`\n"
        f"🔐  Password:   `{pwd}`\n"
        f"📧  Email:         `{email}`\n"
        f"💵  Reward:      `${float(price):.4f}`\n"
        f"🕐  Time:          `{t}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ Cancel", callback_data=f"sub_cancel:{k}:{uid}"),
            InlineKeyboardButton("✅ Approve", callback_data=f"sub_approve:{k}:{uid}:{price}"),
        ],
        [
            InlineKeyboardButton("⬅️ Previous", callback_data="sub_prev"),
            InlineKeyboardButton("➡️ Next",     callback_data="sub_next"),
        ],
    ])

    if edit:
        try:
            await update_or_query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
            )
        except Exception:
            await update_or_query.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
            )
    else:
        await update_or_query.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )

async def callback_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "sub_prev":
        ctx.user_data["sub_index"] = max(0, ctx.user_data.get("sub_index", 0) - 1)
        await send_sub_card(query, ctx, edit=True)
        return

    if data == "sub_next":
        pending = ctx.user_data.get("sub_list", [])
        ctx.user_data["sub_index"] = min(len(pending)-1, ctx.user_data.get("sub_index", 0) + 1)
        await send_sub_card(query, ctx, edit=True)
        return

    if data.startswith("sub_cancel:"):
        _, sub_key, uid_str = data.split(":")
        uid = int(uid_str)
        fb_update(f"submissions/{sub_key}", {"status": "cancelled"})

        udata = get_user(uid)
        old_pending  = udata.get("tasks_pending", 0)
        old_rejected = udata.get("tasks_rejected", 0)
        fb_update(f"users/{uid}", {
            "tasks_pending":  max(0, old_pending - 1),
            "tasks_rejected": old_rejected + 1,
        })

        try:
            await ctx.bot.send_message(
                uid,
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "❌  Task Rejected\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "😔 Your task submission has been *rejected* by admin.\n\n"
                "📝 Reason: Does not meet requirements.\n\n"
                "🔄 You can try submitting again anytime!\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        # Remove from local list
        pending = ctx.user_data.get("sub_list", [])
        ctx.user_data["sub_list"] = [(k, v) for k, v in pending if k != sub_key]
        ctx.user_data["sub_index"] = max(0, ctx.user_data.get("sub_index", 0))

        await query.edit_message_text(f"✅ Submission `{sub_key}` cancelled & user notified.",
                                      parse_mode=ParseMode.MARKDOWN)
        return

    if data.startswith("sub_approve:"):
        parts     = data.split(":")
        sub_key   = parts[1]
        uid       = int(parts[2])
        price     = float(parts[3])

        fb_update(f"submissions/{sub_key}", {"status": "approved"})

        udata        = get_user(uid)
        old_pending  = udata.get("tasks_pending", 0)
        old_approved = udata.get("tasks_approved", 0)
        old_balance  = udata.get("balance", 0.0)

        fb_update(f"users/{uid}", {
            "tasks_pending":  max(0, old_pending - 1),
            "tasks_approved": old_approved + 1,
            "balance":        round(old_balance + price, 6),
        })

        # Referral bonus
        referrer_id = udata.get("referrer")
        if referrer_id:
            bonus = round(price * REFERRAL_PCT, 6)
            rdata = get_user(referrer_id)
            if rdata:
                old_rb  = rdata.get("balance", 0.0)
                old_re  = rdata.get("referral_earned", 0.0)
                fb_update(f"users/{referrer_id}", {
                    "balance":         round(old_rb + bonus, 6),
                    "referral_earned": round(old_re + bonus, 6),
                })
                try:
                    await ctx.bot.send_message(
                        referrer_id,
                        f"🎁 *Referral Bonus!*\n\n"
                        f"Your friend completed a task!\n"
                        f"💵 You earned: `${bonus:.4f}`\n"
                        f"💰 Check your balance!",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

        try:
            await ctx.bot.send_message(
                uid,
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅  Task Approved!\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "🎉 Your task has been *approved* by admin!\n\n"
                f"💵  Reward Added:   `${price:.4f}`\n"
                "💰  Check your balance now!\n\n"
                "🙏 Keep completing tasks to earn more!\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        # Remove from local list
        pending = ctx.user_data.get("sub_list", [])
        ctx.user_data["sub_list"] = [(k, v) for k, v in pending if k != sub_key]
        ctx.user_data["sub_index"] = max(0, ctx.user_data.get("sub_index", 0))

        await query.edit_message_text(
            f"✅ Submission `{sub_key}` approved!\n"
            f"💵 `${price:.4f}` added to user `{uid}` wallet.",
            parse_mode=ParseMode.MARKDOWN,
        )

# ──────────────────────────────────────────────
#  /xl — export emails xlsx
# ──────────────────────────────────────────────
async def cmd_xl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    emails_data = fb_get("emails") or {}
    if not emails_data:
        await update.message.reply_text("📭 No emails saved yet.")
        return

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Emails"
    ws.append(["Email / Phone"])

    for _, entry in emails_data.items():
        ws.append([entry.get("email", "")])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    await update.message.reply_document(
        document=buf,
        filename="ax_worker_emails.xlsx",
        caption="📊 All submitted emails/phone numbers.",
    )

# ──────────────────────────────────────────────
#  /wd — withdrawal requests
# ──────────────────────────────────────────────
async def cmd_wd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    wds = fb_get("withdrawals") or {}
    if not wds:
        await update.message.reply_text("📭 No withdrawal requests yet.")
        return

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💸  Withdrawal Requests\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    ]
    for i, (k, v) in enumerate(wds.items(), 1):
        lines.append(
            f"\n🔢  #{i}\n"
            f"👤  Name:     `{v.get('name','N/A')}`\n"
            f"🆔  ID:         `{v.get('uid','N/A')}`\n"
            f"📛  Username: `{v.get('username','N/A')}`\n"
            f"💎  Method:  `{v.get('method','N/A')}`\n"
            f"📋  Address: `{v.get('address','N/A')}`\n"
            f"💵  Amount:  `${float(v.get('amount',0)):.4f}`\n"
            f"📊  Status:   `{v.get('status','N/A')}`\n"
            f"🕐  Time:      `{v.get('requested_at','N/A')}`\n"
            "━━━━━━━━━━━━━━━━━━"
        )

    full = "\n".join(lines)
    # Split if too long
    if len(full) > 4000:
        chunks = [full[i:i+4000] for i in range(0, len(full), 4000)]
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(full, parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────
#  /stats
# ──────────────────────────────────────────────
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    users_data = fb_get("users") or {}
    subs_data  = fb_get("submissions") or {}
    wds_data   = fb_get("withdrawals") or {}
    emails_data = fb_get("emails") or {}

    total_u    = len(users_data)
    total_subs = len(subs_data)
    pending_s  = sum(1 for v in subs_data.values() if v.get("status") == "pending")
    approved_s = sum(1 for v in subs_data.values() if v.get("status") == "approved")
    rejected_s = sum(1 for v in subs_data.values() if v.get("status") == "cancelled")
    total_wds  = len(wds_data)
    total_emails = len(emails_data)

    total_paid = sum(float(v.get("amount", 0)) for v in wds_data.values())

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊  Bot Statistics\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥  Total Users:          `{total_u}`\n\n"
        "📋  Submissions\n"
        f"   📥 Total:              `{total_subs}`\n"
        f"   ⏳ Pending:            `{pending_s}`\n"
        f"   ✅ Approved:          `{approved_s}`\n"
        f"   ❌ Rejected:            `{rejected_s}`\n\n"
        f"📧  Total Emails Saved: `{total_emails}`\n\n"
        f"💸  Withdrawals:          `{total_wds}`\n"
        f"💵  Total Paid Out:      `${total_paid:.4f}`\n\n"
        f"💰  Task Price:           `${get_price():.4f}`\n"
        f"🔐  Task Password:      `{get_password()}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────
#  /pass <password>
# ──────────────────────────────────────────────
async def cmd_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not ctx.args:
        await update.message.reply_text("Usage: `/pass YourNewPassword`", parse_mode=ParseMode.MARKDOWN)
        return

    new_pass = " ".join(ctx.args).strip()
    set_setting("password", new_pass)

    await update.message.reply_text(
        f"✅ *Password updated successfully!*\n\n"
        f"🔐 New Password: `{new_pass}`\n\n"
        "All new task assignments will use this password.",
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────
#  /price <amount>
# ──────────────────────────────────────────────
async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not ctx.args:
        await update.message.reply_text("Usage: `/price 0.0350`", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        new_price = float(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid price. Use a number like `0.0350`.",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    set_setting("price", new_price)

    await update.message.reply_text(
        f"✅ *Task price updated!*\n\n"
        f"💵 New Price: `${new_price:.4f}`\n\n"
        "The task menu will now show the updated price.",
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────
#  /cmd — admin command guide
# ──────────────────────────────────────────────
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ This command is for admins only.")
        return

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛠️  AX Worker — Admin Panel\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 *ALL ADMIN COMMANDS*\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔐 *Bot Control*\n"
        "`/cmd`\n"
        "   → Show this help panel\n\n"
        "`/pass <password>`\n"
        "   → Set a new task password\n"
        "   → Example: `/pass Secure@123`\n\n"
        "`/price <amount>`\n"
        "   → Update task reward price\n"
        "   → Example: `/price 0.0500`\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📊 *Reports & Data*\n"
        "`/stats`\n"
        "   → View full bot statistics\n"
        "   → (Users, submissions, withdrawals, etc.)\n\n"
        "`/xl`\n"
        "   → Download all emails/numbers as Excel file\n\n"
        "`/wd`\n"
        "   → View all pending withdrawal requests\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ *Task Management*\n"
        "`/sub`\n"
        "   → Review pending submissions one by one\n"
        "   → Use inline buttons to Approve / Cancel\n"
        "   → Navigate with Previous / Next\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 *Tips*\n"
        "• Admin notifications arrive automatically\n"
        "• Approving a task adds reward to user wallet\n"
        "• Cancelling sends rejection notice to user\n"
        "• Referral bonus (8%) auto-credited on approval\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────
#  Universal Message Router
# ──────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text.strip() if update.message.text else ""
    state = ctx.user_data.get(STATE_KEY, STATES["IDLE"])

    # ── Main menu buttons ──────────────────────
    if text == "📬 Dashboard":
        await handle_dashboard(update, ctx)
        return
    if text == "💰 Balance":
        await handle_balance(update, ctx)
        return
    if text == "📋 Tasks":
        await handle_tasks(update, ctx)
        return
    if text == "👥 Invite Friends":
        await handle_invite(update, ctx)
        return
    if text == "🏆 Leaderboard":
        await handle_leaderboard(update, ctx)
        return
    if text == "📥 Withdraw":
        await handle_withdraw(update, ctx)
        return
    if text == "👤 Profile":
        await handle_profile(update, ctx)
        return
    if text == "☎️ Support":
        await handle_support(update, ctx)
        return
    if text == "Cancel ❌":
        await handle_cancel(update, ctx)
        return

    # ── Task flow ──────────────────────────────
    price = get_price()
    if text == f"🌟 Create Facebook - ${price:.4f}":
        await handle_task_selected(update, ctx)
        return
    if text == "▶️ Start" and state == "task_overview":
        await handle_task_start(update, ctx)
        return
    if state == STATES["TASK_EMAIL"]:
        await handle_task_email(update, ctx)
        return
    if text == "Account Registered ✅" and state == STATES["TASK_CONFIRM"]:
        await handle_task_confirm(update, ctx)
        return

    # ── Withdraw flow ──────────────────────────
    if text == "💎 USDT-BEP20" and state == STATES["WITHDRAW_METHOD"]:
        await handle_withdraw_usdt(update, ctx)
        return
    if state == STATES["WITHDRAW_ADDRESS"]:
        await handle_withdraw_address(update, ctx)
        return
    if state == STATES["WITHDRAW_AMOUNT"]:
        await handle_withdraw_amount(update, ctx)
        return

    # ── Fallback ───────────────────────────────
    await update.message.reply_text(
        "🤔 I didn't understand that.\nPlease use the menu below 👇",
        reply_markup=MAIN_MENU,
    )

# ──────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", cmd_start))

    # Admin commands
    app.add_handler(CommandHandler("cmd",   cmd_help))
    app.add_handler(CommandHandler("sub",   cmd_sub))
    app.add_handler(CommandHandler("xl",    cmd_xl))
    app.add_handler(CommandHandler("wd",    cmd_wd))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("pass",  cmd_pass))
    app.add_handler(CommandHandler("price", cmd_price))

    # Callback queries (inline buttons)
    app.add_handler(CallbackQueryHandler(callback_sub, pattern="^sub_"))

    # All text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🚀 AX Worker Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
