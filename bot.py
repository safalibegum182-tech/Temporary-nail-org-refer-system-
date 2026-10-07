# ============================================================
# bot.py
# TEMP MAIL TELEGRAM BOT (Updated)
# ============================================================

import asyncio
import html
import json
import logging
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CopyTextButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, ADMIN_IDS

from database import (
    init_db,
    save_user,
    get_language,
    set_language,
    save_mailbox,
    get_mailbox,
    get_all_users,
)


# ============================================================
# SETTINGS
# ============================================================

API_BASE = "https://smails.dev/api"

POLL_SECONDS = 1
MAX_MESSAGES = 3

EMAIL_REWARD = 0.00340
REFERRAL_REWARD = 0.00750

MIN_WITHDRAW = 1.00

DHAKA_TZ = ZoneInfo("Asia/Dhaka")

REWARD_DB = "rewards.db"


# ============================================================
# LOGG
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# MEMORY
# ============================================================

SEEN_MESSAGES = {}
KNOWN_MAILBOX = {}


# ============================================================
# REWARD DATABASE
# ============================================================

def reward_db():
    conn = sqlite3.connect(
        REWARD_DB,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_reward_db():
    conn = reward_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reward_users (
                user_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                total_referrals INTEGER NOT NULL DEFAULT 0,
                total_email_rewards INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rewarded_messages (
                user_id INTEGER NOT NULL,
                message_id TEXT NOT NULL,
                code TEXT,
                amount REAL NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS referrals (
                referred_user_id INTEGER PRIMARY KEY,
                referrer_user_id INTEGER NOT NULL,
                reward REAL NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS withdraw_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                method TEXT NOT NULL,
                destination TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                processed_at TEXT
            );
            """
        )

        columns = conn.execute(
            "PRAGMA table_info(withdraw_requests)"
        ).fetchall()
        column_names = {row["name"] for row in columns}

        if "reject_reason" not in column_names:
            conn.execute(
                "ALTER TABLE withdraw_requests ADD COLUMN reject_reason TEXT"
            )

        if "balance_zeroed" not in column_names:
            conn.execute(
                """
                ALTER TABLE withdraw_requests
                ADD COLUMN balance_zeroed INTEGER NOT NULL DEFAULT 0
                """
            )

        conn.commit()
    finally:
        conn.close()


def ensure_reward_user(user_id):
    conn = reward_db()
    try:
        row = conn.execute(
            "SELECT user_id FROM reward_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if not row:
            conn.execute(
                """
                INSERT INTO reward_users
                (user_id, balance, referral_code, created_at)
                VALUES (?, 0, ?, ?)
                """,
                (
                    user_id,
                    str(user_id),
                    datetime.now(DHAKA_TZ).isoformat(),
                ),
            )
            conn.commit()
    finally:
        conn.close()


def get_balance(user_id):
    ensure_reward_user(user_id)
    conn = reward_db()
    try:
        row = conn.execute(
            "SELECT balance FROM reward_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return float(row["balance"]) if row else 0.0
    finally:
        conn.close()


def get_total_referrals(user_id):
    ensure_reward_user(user_id)
    conn = reward_db()
    try:
        row = conn.execute(
            "SELECT total_referrals FROM reward_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["total_referrals"]) if row else 0
    finally:
        conn.close()


def get_all_reward_users():
    conn = reward_db()
    try:
        rows = conn.execute(
            """
            SELECT
                user_id,
                balance,
                total_referrals,
                total_email_rewards,
                referred_by,
                created_at
            FROM reward_users
            ORDER BY balance DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def reset_all_balances(reason: str):
    conn = reward_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE reward_users
            SET balance = 0,
                total_referrals = 0,
                referred_by = NULL
            """
        )
        conn.execute("DELETE FROM referrals")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_single_user(user_id: int, reason: str):
    conn = reward_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE reward_users
            SET balance = 0,
                total_referrals = 0,
                referred_by = NULL
            WHERE user_id = ?
            """,
            (user_id,),
        )
        conn.execute(
            "DELETE FROM referrals WHERE referred_user_id = ?",
            (user_id,),
        )
        conn.execute(
            "DELETE FROM referrals WHERE referrer_user_id = ?",
            (user_id,),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_email_reward(user_id, message_id, code):
    ensure_reward_user(user_id)
    conn = reward_db()
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO rewarded_messages
            (user_id, message_id, code, amount, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                str(message_id),
                str(code) if code else None,
                EMAIL_REWARD,
                datetime.now(DHAKA_TZ).isoformat(),
            ),
        )

        if cursor.rowcount == 0:
            conn.rollback()
            return False

        conn.execute(
            """
            UPDATE reward_users
            SET balance = balance + ?,
                total_email_rewards = total_email_rewards + 1
            WHERE user_id = ?
            """,
            (EMAIL_REWARD, user_id),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_referral(referrer_id, referred_user_id):
    if not referrer_id or not referred_user_id:
        return False
    if int(referrer_id) == int(referred_user_id):
        return False

    ensure_reward_user(referrer_id)
    ensure_reward_user(referred_user_id)

    conn = reward_db()
    try:
        existing = conn.execute(
            "SELECT referred_user_id FROM referrals WHERE referred_user_id = ?",
            (referred_user_id,),
        ).fetchone()
        if existing:
            return False

        row = conn.execute(
            "SELECT referred_by FROM reward_users WHERE user_id = ?",
            (referred_user_id,),
        ).fetchone()
        if row and row["referred_by"]:
            return False

        conn.execute(
            """
            INSERT INTO referrals
            (referred_user_id, referrer_user_id, reward, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                referred_user_id,
                referrer_id,
                REFERRAL_REWARD,
                datetime.now(DHAKA_TZ).isoformat(),
            ),
        )

        conn.execute(
            "UPDATE reward_users SET referred_by = ? WHERE user_id = ?",
            (referrer_id, referred_user_id),
        )

        conn.execute(
            """
            UPDATE reward_users
            SET balance = balance + ?,
                total_referrals = total_referrals + 1
            WHERE user_id = ?
            """,
            (REFERRAL_REWARD, referrer_id),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_withdraw_request(user_id, method, destination, amount):
    ensure_reward_user(user_id)
    amount = float(amount)
    conn = reward_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT balance FROM reward_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        balance = float(row["balance"]) if row else 0.0

        if balance < MIN_WITHDRAW:
            conn.rollback()
            return None, "MINIMUM"
        if amount <= 0 or amount > balance:
            conn.rollback()
            return None, "BALANCE"

        cursor = conn.execute(
            """
            INSERT INTO withdraw_requests
            (user_id, method, destination, amount, status, created_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?)
            """,
            (
                user_id,
                str(method),
                str(destination),
                amount,
                datetime.now(DHAKA_TZ).isoformat(),
            ),
        )
        request_id = cursor.lastrowid

        conn.execute(
            "UPDATE reward_users SET balance = balance - ? WHERE user_id = ?",
            (amount, user_id),
        )
        conn.commit()
        return request_id, "OK"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_withdraw_request(request_id):
    conn = reward_db()
    try:
        return conn.execute(
            "SELECT * FROM withdraw_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
    finally:
        conn.close()


def update_withdraw_status(request_id, status):
    conn = reward_db()
    try:
        cursor = conn.execute(
            """
            UPDATE withdraw_requests
            SET status = ?, processed_at = ?
            WHERE id = ? AND status = 'PENDING'
            """,
            (status, datetime.now(DHAKA_TZ).isoformat(), request_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def reject_withdraw_final(request_id, reason, zero_balance=False):
    conn = reward_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id, amount, status FROM withdraw_requests WHERE id = ?",
            (request_id,),
        ).fetchone()

        if not row or str(row["status"]) != "PENDING":
            conn.rollback()
            return False

        user_id = int(row["user_id"])
        amount = float(row["amount"])

        if zero_balance:
            conn.execute(
                "UPDATE reward_users SET balance = 0 WHERE user_id = ?",
                (user_id,),
            )
            balance_zeroed = 1
        else:
            conn.execute(
                "UPDATE reward_users SET balance = balance + ? WHERE user_id = ?",
                (amount, user_id),
            )
            balance_zeroed = 0

        cursor = conn.execute(
            """
            UPDATE withdraw_requests
            SET status = 'REJECTED',
                reject_reason = ?,
                balance_zeroed = ?,
                processed_at = ?
            WHERE id = ? AND status = 'PENDING'
            """,
            (
                str(reason),
                balance_zeroed,
                datetime.now(DHAKA_TZ).isoformat(),
                request_id,
            ),
        )

        if cursor.rowcount == 0:
            conn.rollback()
            return False

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
# LANGUAGES
# ============================================================

TEXT = {
    "en": {
        "welcome":
            "📧 <b>TEMP MAIL BOT</b>\n\n"
            "👋 Welcome!\n\n"
            "⚡ Fast temporary email receiver\n"
            "📩 Receive verification emails\n"
            "🔐 Codes are detected automatically.\n\n"
            "🌐 Select your language:",
        "language_ok":
            "✅ <b>Language selected!</b>\n\n"
            "⚡ Creating your temporary email...",
        "generating":
            "⚡ <b>Creating your temporary email...</b>",
        "created":
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "   📧  NEW TEMP EMAIL\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n\n"
            "📮 <b>Email:</b>\n"
            "<code>{email}</code>\n\n"
            "🟢 <b>Status:</b> Active\n\n"
            "📩 New emails will be detected automatically.",
        "generate": "➕ Generate New",
        "inbox": "📥 Inbox",
        "refresh": "🔄 Refresh",
        "refer_btn": "👥 Refer System",
        "checking": "🔎 <b>Checking your inbox...</b>",
        "empty": "📭 <b>Inbox is empty.</b>\n\nNo messages received yet.",
        "no_mailbox":
            "⚠️ <b>No temporary email found.</b>\n\n"
            "Please generate a new email first.",
        "api_error": "❌ <b>Something went wrong.</b>\n\nPlease try again later.",
        "new_mail":
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "      📨 NEW EMAIL\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n\n"
            "🌐 <b>Source:</b> Email\n"
            "👤 <b>From:</b> {sender}\n"
            "📌 <b>Subject:</b> {subject}\n"
            "🕐 <b>Time:</b> {date}\n\n"
            "{content}",
        "earned": "💰 <b>You earned:</b> <code>{amount}</code>",
        "message_content": "💬 <b>Message:</b>\n{body}",
        "verification":
            "🔐 <b>VERIFICATION CODE</b>\n\n"
            "🔢 <b>Code:</b> <code>{code}</code>",
        "confirm_link_text":
            "🔗 <b>CONFIRM LINK</b>\n\n"
            "📌 Please press the Confirm button below.",
        "copy_code": "📋 Copy Code",
        "confirm_btn": "✅ Confirm",
        "language": "🌐 <b>Select your language:</b>",
        "help":
            "📚 <b>HELP</b>\n\n"
            "➕ Generate New — Create email\n"
            "📥 Inbox — View emails\n"
            "🔄 Refresh — Check emails\n"
            "👥 Refer System — Refer & earn\n"
            "💰 /balance — Check balance\n"
            "💸 /withdraw — Withdraw\n\n"
            "/start — Start\n"
            "/language — Language\n"
            "/inbox — Inbox\n"
            "/refresh — Refresh\n"
            "/help — Help\n"
            "/stats — Statistics",
        "about":
            "📧 <b>TEMP MAIL</b>\n\n"
            "Fast disposable email receiver.\n\n"
            "⚡ Powered by Smails API\n"
            "🔒 No API key required",
        "stats":
            "📊 <b>BOT STATS</b>\n\n"
            "👥 Total Users: <b>{users}</b>\n"
            "📧 Active Mailboxes: <b>{mailboxes}</b>\n"
            "⚡ Auto Inbox: <b>ON</b>\n"
            "🔄 Polling: <b>{seconds}s</b>",
        "admin_only":
            "🔐 <b>ADMIN ONLY</b>\n\n"
            "This command is available only for administrators.",
        "broadcast_start":
            "📢 <b>Broadcast Mode</b>\n\n"
            "Use:\n<code>/broadcast Your message</code>",
        "broadcast_done":
            "✅ <b>Broadcast completed!</b>\n\n"
            "📤 Sent: <b>{sent}</b>\n"
            "❌ Failed: <b>{failed}</b>",
        "admin_panel":
            "👑 <b>ADMIN PANEL</b>\n\n"
            "🔐 You have administrator access.",
        "refer":
            "👥 <b>REFER & EARN</b>\n\n"
            "💰 Per successful referral: <b>{reward}</b>\n"
            "👥 Total referrals: <b>{refs}</b>\n\n"
            "🔗 <b>Your referral link:</b>\n"
            "<code>{link}</code>",
        "balance":
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "      💰 BALANCE\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n\n"
            "💵 Balance: <b>{balance}</b>\n"
            "👥 Referrals: <b>{refs}</b>\n"
            "📩 Email rewards: <b>{email_reward}</b>\n\n"
            "💸 Minimum withdrawal: <b>${minimum:.2f}</b>",
        "withdraw_button": "💸 Withdraw",
        "withdraw_choose":
            "💸 <b>Withdraw</b>\n\nPlease select Withdraw method:",
        "withdraw_amount":
            "💵 <b>Withdraw Amount</b>\n\n"
            "Your balance: <b>${balance:.5f}</b>\n\n"
            "Send the amount you want to withdraw:",
        "withdraw_address":
            "📥 <b>{method}</b>\n\n"
            "Send your Binance ID or BEP20 address:",
        "withdraw_invalid": "⚠️ Invalid value. Please try again.",
        "withdraw_min":
            "⚠️ <b>Minimum withdrawal is $1.00.</b>\n\n"
            "Your current balance: <b>${balance:.5f}</b>",
        "withdraw_balance":
            "⚠️ <b>Insufficient balance.</b>\n\n"
            "Your current balance: <b>${balance:.5f}</b>",
        "withdraw_sent":
            "✅ <b>Withdrawal request submitted!</b>\n\n"
            "💵 Amount: <b>${amount:.5f}</b>\n"
            "💳 Method: <b>{method}</b>\n"
            "📥 Destination: <code>{destination}</code>\n\n"
            "⏳ Waiting for admin approval.",
        "withdraw_congratulations":
            "🎉 <b>Congratulations!</b>\n\n"
            "Your withdrawal of <b>${amount:.5f}</b> has been approved.\n\n"
            "💳 Method: <b>{method}</b>\n"
            "📥 Destination: <code>{destination}</code>",
        "withdraw_rejected":
            "❌ <b>Withdrawal Rejected</b>\n\n"
            "Your withdrawal request of <b>${amount:.5f}</b> was rejected.\n\n"
            "📌 <b>Reason:</b>\n{reason}\n\n"
            "{balance_message}",
        "reject_select": "⚠️ <b>Please Select Your Reason</b>",
        "reject_bangla": "🇧🇩 Bangla",
        "reject_english": "🇺🇸 English",
        "reject_hindi": "🇮🇳 Hindi",
        "reject_custom": "✏️ Custom",
        "reject_custom_prompt":
            "✏️ <b>Custom Rejection Reason</b>\n\n"
            "Please write the rejection reason:",
        "balance_zero_question":
            "⚠️ <b>Balance Action</b>\n\n"
            "Do you want to make this user's balance <b>0</b>?",
        "balance_yes": "✅ Yes",
        "balance_no": "❌ No",
        "admin_rejected_zero":
            "❌ Request #{request_id} rejected.\n"
            "💰 User balance has been set to 0.",
        "admin_rejected_refund":
            "❌ Request #{request_id} rejected.\n"
            "💰 Withdrawal amount has been refunded.",
        "admin_approved": "✅ Request #{request_id} approved.",
        "approve": "✅ Accept",
        "reject": "❌ Reject",
        "reset_notice":
            "⚠️ <b>Account Reset Notice</b>\n\n"
            "Your account has been reset by Admin.\n\n"
            "📌 <b>Reason:</b>\n{reason}\n\n"
            "💰 Your Balance is now <b>0</b>.\n"
            "🔄 Referral system has also been reset.",
        "reset_notice_single":
            "⚠️ <b>Account Reset Notice</b>\n\n"
            "Your account has been reset by Admin.\n\n"
            "📌 <b>Reason:</b>\n{reason}\n\n"
            "💰 Your Balance is now <b>0</b>.",
    },

    "bn": {
        "welcome":
            "📧 <b>TEMP MAIL BOT</b>\n\n"
            "👋 স্বাগতম!\n\n"
            "⚡ দ্রুত Temporary Email receiver\n"
            "📩 Verification Email গ্রহণ করুন\n"
            "🔐 Code automatically detect হবে।\n\n"
            "🌐 ভাষা নির্বাচন করুন:",
        "language_ok":
            "✅ <b>ভাষা নির্বাচন সফল!</b>\n\n"
            "⚡ Temporary Email তৈরি হচ্ছে...",
        "generating": "⚡ <b>Temporary Email তৈরি হচ্ছে...</b>",
        "created":
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "   📧 নতুন TEMP EMAIL\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n\n"
            "📮 <b>Email:</b>\n"
            "<code>{email}</code>\n\n"
            "🟢 <b>Status:</b> Active\n\n"
            "📩 নতুন Email এলে Automatic দেখাবে।",
        "generate": "➕ নতুন তৈরি করুন",
        "inbox": "📥 ইনবক্স",
        "refresh": "🔄 রিফ্রেশ",
        "refer_btn": "👥 রেফার সিস্টেম",
        "checking": "🔎 <b>আপনার Inbox check করা হচ্ছে...</b>",
        "empty": "📭 <b>Inbox খালি।</b>\n\nএখনো কোনো Message আসেনি।",
        "no_mailbox":
            "⚠️ <b>কোনো Temporary Email পাওয়া যায়নি।</b>\n\n"
            "প্রথমে নতুন Email তৈরি করুন।",
        "api_error": "❌ <b>সমস্যা হয়েছে।</b>\n\nকিছুক্ষণ পর আবার চেষ্টা করুন।",
        "new_mail":
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "      📨 নতুন EMAIL\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n\n"
            "🌐 <b>Source:</b> Email\n"
            "👤 <b>From:</b> {sender}\n"
            "📌 <b>Subject:</b> {subject}\n"
            "🕐 <b>সময়:</b> {date}\n\n"
            "{content}",
        "earned": "💰 <b>আপনি পেয়েছেন:</b> <code>{amount}</code>",
        "message_content": "💬 <b>Message:</b>\n{body}",
        "verification":
            "🔐 <b>VERIFICATION CODE</b>\n\n"
            "🔢 <b>Code:</b> <code>{code}</code>",
        "confirm_link_text":
            "🔗 <b>CONFIRM LINK</b>\n\n"
            "📌 নিচের Confirm বাটন চাপুন।",
        "copy_code": "📋 Code Copy করুন",
        "confirm_btn": "✅ Confirm",
        "language": "🌐 <b>আপনার ভাষা নির্বাচন করুন:</b>",
        "help":
            "📚 <b>HELP</b>\n\n"
            "➕ নতুন তৈরি করুন — নতুন Email\n"
            "📥 ইনবক্স — Email দেখুন\n"
            "🔄 রিফ্রেশ — নতুন Email check\n"
            "👥 রেফার সিস্টেম — Refer & earn\n"
            "💰 /balance — Balance\n"
            "💸 /withdraw — Withdraw\n\n"
            "/start — Start\n"
            "/language — ভাষা\n"
            "/inbox — Inbox\n"
            "/refresh — Refresh\n"
            "/help — Help\n"
            "/stats — Statistics",
        "about":
            "📧 <b>TEMP MAIL</b>\n\n"
            "দ্রুত Temporary Email receiver।\n\n"
            "⚡ Smails API দ্বারা পরিচালিত\n"
            "🔒 API key প্রয়োজন নেই",
        "stats":
            "📊 <b>BOT STATS</b>\n\n"
            "👥 Total Users: <b>{users}</b>\n"
            "📧 Active Mailboxes: <b>{mailboxes}</b>\n"
            "⚡ Auto Inbox: <b>ON</b>\n"
            "🔄 Checking: <b>{seconds}s</b>",
        "admin_only":
            "🔐 <b>ADMIN ONLY</b>\n\n"
            "এই Command শুধুমাত্র Administrator-এর জন্য।",
        "broadcast_start":
            "📢 <b>Broadcast Mode</b>\n\n"
            "ব্যবহার করুন:\n<code>/broadcast আপনার Message</code>",
        "broadcast_done":
            "✅ <b>Broadcast সম্পন্ন!</b>\n\n"
            "📤 পাঠানো হয়েছে: <b>{sent}</b>\n"
            "❌ Failed: <b>{failed}</b>",
        "admin_panel":
            "👑 <b>ADMIN PANEL</b>\n\n"
            "🔐 আপনার Administrator access আছে।",
        "refer":
            "👥 <b>REFER & EARN</b>\n\n"
            "💰 প্রতি সফল Refer: <b>{reward}</b>\n"
            "👥 মোট Refer: <b>{refs}</b>\n\n"
            "🔗 <b>আপনার Referral Link:</b>\n"
            "<code>{link}</code>",
        "balance":
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "      💰 BALANCE\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n\n"
            "💵 Balance: <b>{balance}</b>\n"
            "👥 Referrals: <b>{refs}</b>\n"
            "📩 Email rewards: <b>{email_reward}</b>\n\n"
            "💸 Minimum withdrawal: <b>${minimum:.2f}</b>",
        "withdraw_button": "💸 Withdraw",
        "withdraw_choose":
            "💸 <b>Withdraw</b>\n\nWithdraw method নির্বাচন করুন:",
        "withdraw_amount":
            "💵 <b>Withdraw Amount</b>\n\n"
            "আপনার Balance: <b>${balance:.5f}</b>\n\n"
            "যত টাকা Withdraw করবেন amount পাঠান:",
        "withdraw_address":
            "📥 <b>{method}</b>\n\n"
            "আপনার Binance ID অথবা BEP20 address পাঠান:",
        "withdraw_invalid": "⚠️ সঠিক তথ্য দিন। আবার চেষ্টা করুন।",
        "withdraw_min":
            "⚠️ <b>Minimum withdrawal $1.00.</b>\n\n"
            "আপনার বর্তমান Balance: <b>${balance:.5f}</b>",
        "withdraw_balance":
            "⚠️ <b>Balance পর্যাপ্ত নয়।</b>\n\n"
            "আপনার বর্তমান Balance: <b>${balance:.5f}</b>",
        "withdraw_sent":
            "✅ <b>Withdrawal request পাঠানো হয়েছে!</b>\n\n"
            "💵 Amount: <b>${amount:.5f}</b>\n"
            "💳 Method: <b>{method}</b>\n"
            "📥 Destination: <code>{destination}</code>\n\n"
            "⏳ Admin approval-এর অপেক্ষায়।",
        "withdraw_congratulations":
            "🎉 <b>Congratulations!</b>\n\n"
            "আপনার <b>${amount:.5f}</b> Withdrawal approved হয়েছে।\n\n"
            "💳 Method: <b>{method}</b>\n"
            "📥 Destination: <code>{destination}</code>",
        "withdraw_rejected":
            "❌ <b>Withdrawal Rejected</b>\n\n"
            "আপনার <b>${amount:.5f}</b> Withdrawal reject হয়েছে।\n\n"
            "📌 <b>Reject করার কারণ:</b>\n{reason}\n\n"
            "{balance_message}",
        "reject_select":
            "⚠️ <b>Please Select Your Reason</b>\n\n"
            "Withdrawal কেন reject করবেন তার কারণ নির্বাচন করুন।",
        "reject_bangla": "🇧🇩 Bangla",
        "reject_english": "🇺🇸 English",
        "reject_hindi": "🇮🇳 Hindi",
        "reject_custom": "✏️ Custom",
        "reject_custom_prompt":
            "✏️ <b>Custom Rejection Reason</b>\n\n"
            "অনুগ্রহ করে rejection reason লিখুন:",
        "balance_zero_question":
            "⚠️ <b>Balance Action</b>\n\n"
            "আপনি কি এই User-এর balance <b>0</b> করতে চান?",
        "balance_yes": "✅ Yes",
        "balance_no": "❌ No",
        "admin_rejected_zero":
            "❌ Request #{request_id} rejected.\n"
            "💰 User-এর balance 0 করা হয়েছে।",
        "admin_rejected_refund":
            "❌ Request #{request_id} rejected.\n"
            "💰 Withdrawal amount ফেরত দেওয়া হয়েছে।",
        "admin_approved": "✅ Request #{request_id} approved.",
        "approve": "✅ Accept",
        "reject": "❌ Reject",
        "reset_notice":
            "⚠️ <b>Account Reset Notice</b>\n\n"
            "আপনার অ্যাকাউন্ট Admin দ্বারা Reset করা হয়েছে।\n\n"
            "📌 <b>Reason:</b>\n{reason}\n\n"
            "💰 আপনার Balance এখন <b>0</b>।\n"
            "🔄 Referral সিস্টেমও রিসেট হয়েছে।",
        "reset_notice_single":
            "⚠️ <b>Account Reset Notice</b>\n\n"
            "আপনার অ্যাকাউন্ট Admin দ্বারা Reset করা হয়েছে।\n\n"
            "📌 <b>Reason:</b>\n{reason}\n\n"
            "💰 আপনার Balance এখন <b>0</b>।",
    },

    "hi": {
        "welcome":
            "📧 <b>TEMP MAIL BOT</b>\n\n"
            "👋 Swagat hai!\n\n"
            "⚡ Fast temporary email receiver\n"
            "📩 Verification emails receive karein\n"
            "🔐 Code automatically detect hoga.\n\n"
            "🌐 Language select karein:",
        "language_ok":
            "✅ <b>Language selected!</b>\n\n"
            "⚡ Temporary Email create ho raha hai...",
        "generating": "⚡ <b>Temporary Email create ho raha hai...</b>",
        "created":
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "   📧 NEW TEMP EMAIL\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n\n"
            "📮 <b>Email:</b>\n"
            "<code>{email}</code>\n\n"
            "🟢 <b>Status:</b> Active\n\n"
            "📩 New emails automatically dikhenge.",
        "generate": "➕ Naya Generate",
        "inbox": "📥 Inbox",
        "refresh": "🔄 Refresh",
        "refer_btn": "👥 Refer System",
        "checking": "🔎 <b>Inbox check ho raha hai...</b>",
        "empty": "📭 <b>Inbox empty hai.</b>",
        "no_mailbox":
            "⚠️ <b>Koi Temporary Email nahi mila.</b>\n\n"
            "Pehle naya Email generate karein.",
        "api_error": "❌ <b>Kuch problem ho gayi.</b>\n\nBaad mein try karein.",
        "new_mail":
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "      📨 NEW EMAIL\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n\n"
            "🌐 <b>Source:</b> Email\n"
            "👤 <b>From:</b> {sender}\n"
            "📌 <b>Subject:</b> {subject}\n"
            "🕐 <b>Time:</b> {date}\n\n"
            "{content}",
        "earned": "💰 <b>You earned:</b> <code>{amount}</code>",
        "message_content": "💬 <b>Message:</b>\n{body}",
        "verification":
            "🔐 <b>VERIFICATION CODE</b>\n\n"
            "🔢 <b>Code:</b> <code>{code}</code>",
        "confirm_link_text":
            "🔗 <b>CONFIRM LINK</b>\n\n"
            "📌 Niche Confirm button dabayein.",
        "copy_code": "📋 Copy Code",
        "confirm_btn": "✅ Confirm",
        "language": "🌐 <b>Language select karein:</b>",
        "help":
            "📚 <b>HELP</b>\n\n"
            "➕ Naya Generate — Email\n"
            "📥 Inbox — Emails\n"
            "🔄 Refresh — Check\n"
            "👥 Refer System — Refer & earn\n"
            "💰 /balance — Balance\n"
            "💸 /withdraw — Withdraw\n\n"
            "/start /language /inbox /refresh /help /stats",
        "about":
            "📧 <b>TEMP MAIL</b>\n\n"
            "Fast Temporary Email receiver.\n\n"
            "⚡ Smails API se powered",
        "stats":
            "📊 <b>BOT STATS</b>\n\n"
            "👥 Total Users: <b>{users}</b>\n"
            "📧 Active Mailboxes: <b>{mailboxes}</b>\n"
            "⚡ Auto Inbox: <b>ON</b>\n"
            "🔄 Checking: <b>{seconds}s</b>",
        "admin_only":
            "🔐 <b>ADMIN ONLY</b>\n\n"
            "Yeh command sirf administrator ke liye hai.",
        "broadcast_start":
            "📢 <b>Broadcast Mode</b>\n\n"
            "Use: <code>/broadcast Your message</code>",
        "broadcast_done":
            "✅ <b>Broadcast completed!</b>\n\n"
            "📤 Sent: <b>{sent}</b>\n"
            "❌ Failed: <b>{failed}</b>",
        "admin_panel":
            "👑 <b>ADMIN PANEL</b>\n\n"
            "🔐 Administrator access active.",
        "refer":
            "👥 <b>REFER & EARN</b>\n\n"
            "💰 Per successful referral: <b>{reward}</b>\n"
            "👥 Total referrals: <b>{refs}</b>\n\n"
            "🔗 <b>Your referral link:</b>\n"
            "<code>{link}</code>",
        "balance":
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "      💰 BALANCE\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n\n"
            "💵 Balance: <b>{balance}</b>\n"
            "👥 Referrals: <b>{refs}</b>\n"
            "📩 Email rewards: <b>{email_reward}</b>\n\n"
            "💸 Minimum withdrawal: <b>${minimum:.2f}</b>",
        "withdraw_button": "💸 Withdraw",
        "withdraw_choose":
            "💸 <b>Withdraw</b>\n\nPlease select Withdraw method:",
        "withdraw_amount":
            "💵 <b>Withdraw Amount</b>\n\n"
            "Your balance: <b>${balance:.5f}</b>\n\n"
            "Send withdrawal amount:",
        "withdraw_address":
            "📥 <b>{method}</b>\n\n"
            "Send Binance ID or BEP20 address:",
        "withdraw_invalid": "⚠️ Invalid value. Please try again.",
        "withdraw_min":
            "⚠️ <b>Minimum withdrawal is $1.00.</b>\n\n"
            "Current balance: <b>${balance:.5f}</b>",
        "withdraw_balance":
            "⚠️ <b>Insufficient balance.</b>\n\n"
            "Current balance: <b>${balance:.5f}</b>",
        "withdraw_sent":
            "✅ <b>Withdrawal request submitted!</b>\n\n"
            "💵 Amount: <b>${amount:.5f}</b>\n"
            "💳 Method: <b>{method}</b>\n"
            "📥 Destination: <code>{destination}</code>\n\n"
            "⏳ Waiting for admin approval.",
        "withdraw_congratulations":
            "🎉 <b>Congratulations!</b>\n\n"
            "Your withdrawal of <b>${amount:.5f}</b> has been approved.\n\n"
            "💳 Method: <b>{method}</b>\n"
            "📥 Destination: <code>{destination}</code>",
        "withdraw_rejected":
            "❌ <b>Withdrawal Rejected</b>\n\n"
            "Your withdrawal of <b>${amount:.5f}</b> was rejected.\n\n"
            "📌 <b>Reason:</b>\n{reason}\n\n"
            "{balance_message}",
        "reject_select":
            "⚠️ <b>Please Select Your Reason</b>\n\n"
            "Withdrawal reject karne ka reason select karein.",
        "reject_bangla": "🇧🇩 Bangla",
        "reject_english": "🇺🇸 English",
        "reject_hindi": "🇮🇳 Hindi",
        "reject_custom": "✏️ Custom",
        "reject_custom_prompt":
            "✏️ <b>Custom Rejection Reason</b>\n\n"
            "Please rejection reason likhein:",
        "balance_zero_question":
            "⚠️ <b>Balance Action</b>\n\n"
            "Kya aap is User ka balance <b>0</b> karna chahte hain?",
        "balance_yes": "✅ Yes",
        "balance_no": "❌ No",
        "admin_rejected_zero":
            "❌ Request #{request_id} rejected.\n"
            "💰 User balance 0 kar diya gaya hai.",
        "admin_rejected_refund":
            "❌ Request #{request_id} rejected.\n"
            "💰 Withdrawal amount refund kar diya gaya hai.",
        "admin_approved": "✅ Request #{request_id} approved.",
        "approve": "✅ Accept",
        "reject": "❌ Reject",
        "reset_notice":
            "⚠️ <b>Account Reset Notice</b>\n\n"
            "Aapka account Admin dwara Reset kar diya gaya hai.\n\n"
            "📌 <b>Reason:</b>\n{reason}\n\n"
            "💰 Aapka Balance ab <b>0</b> hai.\n"
            "🔄 Referral system bhi reset ho gaya hai.",
        "reset_notice_single":
            "⚠️ <b>Account Reset Notice</b>\n\n"
            "Aapka account Admin dwara Reset kar diya gaya hai.\n\n"
            "📌 <b>Reason:</b>\n{reason}\n\n"
            "💰 Aapka Balance ab <b>0</b> hai.",
    },
}


REJECTION_REASONS = {
    "bn":
        "⚠️ সতর্কতা: আপনার অ্যাকাউন্টে অস্বাভাবিক কার্যকলাপ "
        "শনাক্ত হয়েছে। স্বয়ংক্রিয় বা অপব্যবহারমূলক কোড "
        "রিকোয়েস্টের কারণে আপনার ব্যালেন্স পর্যালোচনা/সমন্বয় "
        "করা হতে পারে। অনুগ্রহ করে এই কার্যকলাপ বন্ধ করুন, "
        "অন্যথায় পরবর্তী ব্যবস্থা নেওয়া হতে পারে।",
    "en":
        "⚠️ Warning: Unusual activity has been detected on "
        "your account. Your balance may be reviewed or "
        "adjusted due to automated or abusive code requests. "
        "Please stop this activity, otherwise further action "
        "may be taken.",
    "hi":
        "⚠️ Chetawani: Aapke account mein asamanya activity pai gayi hai. "
        "Automatic ya misuse wale code request ki wajah se aapke balance ki "
        "review ya adjustment kiya ja sakta hai. Kripya is activity ko band "
        "karein, warna aage karwai ki ja sakti hai",
}


# ============================================================
# KEYBOARDS
# ============================================================

def language_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🇺🇸 English", callback_data="lang_en")],
            [InlineKeyboardButton("🇧🇩 বাংলা", callback_data="lang_bn")],
            [InlineKeyboardButton("🇮🇳 Hindi", callback_data="lang_hi")],
        ]
    )


def main_keyboard(lang):
    t = TEXT[lang]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t["generate"], callback_data="generate"),
                InlineKeyboardButton(t["inbox"], callback_data="inbox"),
            ],
            [InlineKeyboardButton(t["refresh"], callback_data="refresh")],
        ]
    )


def reply_main_keyboard(lang):
    t = TEXT[lang]
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(t["generate"]), KeyboardButton(t["inbox"])],
            [KeyboardButton(t["refresh"]), KeyboardButton(t["refer_btn"])],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def code_keyboard(code, lang):
    t = TEXT[lang]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t["generate"], callback_data="generate"),
                InlineKeyboardButton(
                    t["copy_code"],
                    copy_text=CopyTextButton(text=str(code)),
                ),
            ],
            [InlineKeyboardButton(t["refresh"], callback_data="refresh")],
        ]
    )


def confirm_link_keyboard(url, lang):
    t = TEXT[lang]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t["confirm_btn"],
                    url=url,
                )
            ],
            [
                InlineKeyboardButton(t["generate"], callback_data="generate"),
                InlineKeyboardButton(t["refresh"], callback_data="refresh"),
            ],
        ]
    )


def balance_keyboard(lang):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    TEXT[lang]["withdraw_button"],
                    callback_data="withdraw",
                )
            ]
        ]
    )


def withdraw_method_keyboard(lang):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🆔 Binance ID",
                    callback_data="withdraw_method_binance",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔐 BEP20",
                    callback_data="withdraw_method_bep20",
                )
            ],
        ]
    )


def admin_withdraw_keyboard(request_id, lang="en"):
    t = TEXT[lang]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t["approve"],
                    callback_data=f"withdraw_accept:{request_id}",
                ),
                InlineKeyboardButton(
                    t["reject"],
                    callback_data=f"withdraw_reject:{request_id}",
                ),
            ]
        ]
    )


def rejection_reason_keyboard(request_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🇧🇩 Bangla",
                    callback_data=f"reject_reason_bn:{request_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🇺🇸 English",
                    callback_data=f"reject_reason_en:{request_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🇮🇳 Hindi",
                    callback_data=f"reject_reason_hi:{request_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ Custom",
                    callback_data=f"reject_reason_custom:{request_id}",
                )
            ],
        ]
    )


def balance_zero_keyboard(request_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Yes",
                    callback_data=f"withdraw_zero_yes:{request_id}",
                ),
                InlineKeyboardButton(
                    "❌ No",
                    callback_data=f"withdraw_zero_no:{request_id}",
                ),
            ]
        ]
    )


def dashboard_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Reset All", callback_data="dash_reset_all"),
                InlineKeyboardButton("🎯 Custom Reset", callback_data="dash_custom_reset"),
            ],
            [
                InlineKeyboardButton("📢 Broadcast", callback_data="dash_broadcast"),
                InlineKeyboardButton("📊 User Data", callback_data="dash_user_data"),
            ],
        ]
    )


def reset_all_confirm_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm", callback_data="dash_reset_all_confirm"),
                InlineKeyboardButton("❌ Reject", callback_data="dash_reset_all_reject"),
            ]
        ]
    )


# ============================================================
# HELPERS
# ============================================================

def user_lang(user_id):
    try:
        lang = get_language(user_id)
    except Exception:
        lang = None
    return lang if lang in TEXT else "en"


def safe(value):
    if value is None:
        return ""
    return html.escape(str(value))


def is_admin(user_id):
    try:
        return int(user_id) in [int(x) for x in ADMIN_IDS]
    except Exception:
        return False


def dhaka_time():
    return datetime.now(DHAKA_TZ).strftime("%d %b %Y, %I:%M:%S %p")


# ============================================================
# IMPROVED CODE + LINK DETECTION
# ============================================================

def extract_code(text):
    if not text:
        return None

    text = str(text)

    patterns = [
        r"(?:verification|verify|verification\s*code|"
        r"verification\s*number|otp|one[\s-]*time[\s-]*"
        r"password|login\s*code|security\s*code|"
        r"confirmation\s*code|your\s*code|code\s*is|"
        r"otp\s*is|pin\s*is|is\s*:?\s*)\D{0,40}(\d{4,8})",

        r"(?:code|pin|otp)\s*[:=\-]?\s*(\d{4,8})",
        r"(?:is|:)\s*(\d{4,8})\b",
        r"\b(\d{6})\b",
        r"\b(\d{5})\b",
        r"\b(\d{4})\b",
        r"\b(\d{8})\b",
        r"\b(\d{7})\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            code = match.group(1)
            if code and code.isdigit() and 4 <= len(code) <= 8:
                return code

    return None


def extract_confirm_link(text):
    if not text:
        return None

    text = str(text)

    # Common confirmation / verification links
    patterns = [
        r"(https?://[^\s<>\"']+(?:confirm|verify|activate|validation|click)[^\s<>\"']*)",
        r"(https?://[^\s<>\"']+/[^\s<>\"']*)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            url = match.group(1).rstrip(".,);]")
            if len(url) > 15:
                return url

    return None


def get_message_id(item):
    for value in (
        item.get("id"),
        item.get("messageId"),
        item.get("_id"),
        item.get("uid"),
    ):
        if value is not None:
            return str(value)

    raw = "|".join(
        [
            str(item.get("subject", "")),
            str(item.get("date", "")),
            str(item.get("createdAt", "")),
            str(item.get("from", "")),
            str(item.get("intro", "")),
            str(item.get("text", "")),
            str(item.get("body", "")),
            str(item.get("content", "")),
        ]
    )
    return raw


# ============================================================
# API
# ============================================================

async def api_request(method, endpoint, token=None):
    url = API_BASE + endpoint
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    timeout = aiohttp.ClientTimeout(total=10, connect=4, sock_read=7)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, headers=headers) as response:
                if response.status >= 400:
                    body = await response.text()
                    logger.error("API HTTP %s: %s", response.status, body[:500])
                    return None

                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type.lower():
                    return await response.json(content_type=None)

                text = await response.text()
                try:
                    return json.loads(text)
                except Exception:
                    logger.error("Invalid API JSON: %s", text[:500])
                    return None
    except asyncio.TimeoutError:
        logger.warning("API timeout: %s", url)
        return None
    except Exception as error:
        logger.error("API error: %s", error)
        return None


async def create_mailbox():
    return await api_request("POST", "/mailbox")


async def get_messages(token):
    return await api_request("GET", "/mailbox/messages", token)


def extract_messages(data):
    if not data:
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    messages = data.get("messages")
    if isinstance(messages, list):
        return messages
    nested = data.get("data")
    if isinstance(nested, dict):
        messages = nested.get("messages")
        if isinstance(messages, list):
            return messages
    if isinstance(nested, list):
        return nested
    return []


def parse_sender(item):
    sender_data = (
        item.get("from")
        or item.get("sender")
        or item.get("senderAddress")
        or item.get("fromAddress")
        or ""
    )
    if isinstance(sender_data, dict):
        sender = (
            sender_data.get("address")
            or sender_data.get("email")
            or sender_data.get("mail")
            or sender_data.get("name")
            or ""
        )
    else:
        sender = str(sender_data)
    if not sender:
        sender = (
            item.get("fromEmail")
            or item.get("senderEmail")
            or item.get("email")
            or "Unknown"
        )
    return str(sender)


def parse_mail(item):
    sender = parse_sender(item)
    subject = item.get("subject") or "(No Subject)"
    body = (
        item.get("text")
        or item.get("body")
        or item.get("intro")
        or item.get("content")
        or item.get("html")
        or ""
    )
    if isinstance(body, dict):
        body = body.get("text") or body.get("plain") or body.get("content") or ""
    return str(sender), str(subject), str(body)


# ============================================================
# BUILD EMAIL MESSAGE (IMPROVED)
# ============================================================

def build_mail_message(item, lang, reward_added):
    t = TEXT[lang]
    sender, subject, body = parse_mail(item)

    full_text = f"{subject}\n{body}"

    code = extract_code(full_text)
    confirm_link = extract_confirm_link(full_text)

    # Priority 1: Code found → only Code + Reward
    if code:
        amount = EMAIL_REWARD if reward_added else 0.0
        content = (
            t["verification"].format(code=safe(code))
            + "\n\n"
            + t["earned"].format(amount=f"{amount:.5f}")
        )
        keyboard = code_keyboard(code, lang)

    # Priority 2: Confirm link found → Confirm button
    elif confirm_link:
        content = t["confirm_link_text"]
        keyboard = confirm_link_keyboard(confirm_link, lang)

    # Priority 3: Normal message
    else:
        content = t["message_content"].format(body=safe(body[:1500]))
        keyboard = main_keyboard(lang)

    message_text = t["new_mail"].format(
        sender=safe(sender),
        subject=safe(subject),
        date=safe(dhaka_time()),
        content=content,
    )
    return message_text, keyboard, code


async def send_auto_mail(bot, user_id, item, lang):
    message_id = get_message_id(item)
    full_text = (
        f"{item.get('subject', '')}\n"
        f"{item.get('text', '')}\n"
        f"{item.get('body', '')}\n"
        f"{item.get('intro', '')}\n"
        f"{item.get('content', '')}"
    )
    code = extract_code(full_text)

    reward_added = False
    if code and message_id:
        reward_added = add_email_reward(user_id, message_id, code)

    message_text, keyboard, _ = build_mail_message(item, lang, reward_added)
    await bot.send_message(
        chat_id=user_id,
        text=message_text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def send_inbox_mail(bot, user_id, item, lang):
    message_id = get_message_id(item)
    full_text = (
        f"{item.get('subject', '')}\n"
        f"{item.get('text', '')}\n"
        f"{item.get('body', '')}\n"
        f"{item.get('intro', '')}\n"
        f"{item.get('content', '')}"
    )
    code = extract_code(full_text)

    reward_added = False
    if code and message_id:
        reward_added = add_email_reward(user_id, message_id, code)

    message_text, keyboard, _ = build_mail_message(item, lang, reward_added)
    await bot.send_message(
        chat_id=user_id,
        text=message_text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def generate_new(message, user_id, lang):
    t = TEXT[lang]
    loading = await message.reply_text(t["generating"], parse_mode="HTML")
    mailbox = await create_mailbox()

    if not mailbox:
        await loading.edit_text(t["api_error"], parse_mode="HTML")
        return False

    data = mailbox
    if isinstance(mailbox.get("data"), dict):
        data = mailbox["data"]

    email = (
        data.get("address")
        or data.get("email")
        or mailbox.get("address")
        or mailbox.get("email")
    )
    token = (
        data.get("token")
        or data.get("accessToken")
        or mailbox.get("token")
        or mailbox.get("accessToken")
    )

    if not email or not token:
        logger.error("Mailbox response missing email/token: %s", mailbox)
        await loading.edit_text(t["api_error"], parse_mode="HTML")
        return False

    save_mailbox(user_id, email, token)
    SEEN_MESSAGES[user_id] = set()
    KNOWN_MAILBOX[user_id] = token
    ensure_reward_user(user_id)

    await loading.edit_text(
        t["created"].format(email=safe(email)),
        reply_markup=main_keyboard(lang),
        parse_mode="HTML",
    )
    await message.reply_text(
        "📩 Receive Codes & Earn Rewards! 💰",
        reply_markup=reply_main_keyboard(lang),
    )
    return True


# ============================================================
# START
# ============================================================

async def start(update, context):
    user = update.effective_user
    if not user or not update.message:
        return

    user_id = user.id
    save_user(user_id, user.username)
    ensure_reward_user(user_id)

    if context.args:
        try:
            referrer_id = int(str(context.args[0]).strip())
            if referrer_id != user_id:
                referral_created = create_referral(referrer_id, user_id)
                if referral_created:
                    try:
                        new_balance = get_balance(referrer_id)
                        total_refs = get_total_referrals(referrer_id)
                        await context.bot.send_message(
                            chat_id=referrer_id,
                            text=(
                                "🎉 <b>NEW REFERRAL!</b>\n\n"
                                "👤 New user joined!\n\n"
                                "💰 <b>Referral Bonus:</b>\n"
                                f"<code>+{REFERRAL_REWARD:.5f}</code>\n\n"
                                "💵 <b>Your Balance:</b>\n"
                                f"<code>{new_balance:.5f}</code>\n\n"
                                "👥 <b>Total Referrals:</b>\n"
                                f"<code>{total_refs}</code>\n\n"
                                "✅ Bonus has been added!"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as error:
                        logger.warning("Referral notification failed: %s", error)
        except Exception as error:
            logger.warning("Referral processing error: %s", error)

    lang = get_language(user_id)
    if not lang:
        await update.message.reply_text(
            TEXT["en"]["welcome"],
            reply_markup=language_keyboard(),
            parse_mode="HTML",
        )
        return

    lang = user_lang(user_id)
    mailbox = get_mailbox(user_id)

    if mailbox:
        await update.message.reply_text(
            TEXT[lang]["created"].format(
                email=safe(mailbox.get("email", ""))
            ),
            reply_markup=main_keyboard(lang),
            parse_mode="HTML",
        )
        await update.message.reply_text(
            "📩 Receive Codes & Earn Rewards! 💰",
            reply_markup=reply_main_keyboard(lang),
        )
    else:
        await generate_new(update.message, user_id, lang)


async def show_inbox(message, user_id, lang):
    t = TEXT[lang]
    mailbox = get_mailbox(user_id)
    if not mailbox:
        await message.reply_text(
            t["no_mailbox"],
            reply_markup=main_keyboard(lang),
            parse_mode="HTML",
        )
        return

    loading = await message.reply_text(t["checking"], parse_mode="HTML")
    data = await get_messages(mailbox.get("token"))
    if not data:
        await loading.edit_text(t["api_error"], parse_mode="HTML")
        return

    messages = extract_messages(data)
    if not messages:
        await loading.edit_text(
            t["empty"],
            reply_markup=main_keyboard(lang),
            parse_mode="HTML",
        )
        return

    try:
        await loading.delete()
    except Exception:
        pass

    messages = list(reversed(messages))
    for item in messages[:MAX_MESSAGES]:
        try:
            await send_inbox_mail(message.get_bot(), user_id, item, lang)
        except Exception as error:
            logger.error("Inbox message error: %s", error)


# ============================================================
# COMMANDS
# ============================================================

async def refresh_command(update, context):
    user_id = update.effective_user.id
    await show_inbox(update.message, user_id, user_lang(user_id))


async def inbox_command(update, context):
    user_id = update.effective_user.id
    await show_inbox(update.message, user_id, user_lang(user_id))


async def language_command(update, context):
    user_id = update.effective_user.id
    lang = user_lang(user_id)
    await update.message.reply_text(
        TEXT[lang]["language"],
        reply_markup=language_keyboard(),
        parse_mode="HTML",
    )


async def help_command(update, context):
    user_id = update.effective_user.id
    lang = user_lang(user_id)
    await update.message.reply_text(
        TEXT[lang]["help"],
        reply_markup=main_keyboard(lang),
        parse_mode="HTML",
    )


async def about_command(update, context):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(
            "🚫 <b>Access Denied!</b>\n\n"
            "⚠️ This command is available for administrators only.",
            parse_mode="HTML",
        )
        return
    lang = user_lang(user_id)
    await update.message.reply_text(TEXT[lang]["about"], parse_mode="HTML")


async def refer_command(update, context):
    user_id = update.effective_user.id
    lang = user_lang(user_id)
    ensure_reward_user(user_id)
    try:
        bot_username = context.bot.username
        if not bot_username:
            me = await context.bot.get_me()
            bot_username = me.username
        link = f"https://t.me/{bot_username}?start={user_id}"
        refs = get_total_referrals(user_id)
        await update.message.reply_text(
            TEXT[lang]["refer"].format(
                reward=f"{REFERRAL_REWARD:.5f}",
                refs=refs,
                link=safe(link),
            ),
            parse_mode="HTML",
        )
    except Exception as error:
        logger.error("Refer error: %s", error)
        await update.message.reply_text(TEXT[lang]["api_error"], parse_mode="HTML")


async def balance_command(update, context):
    user_id = update.effective_user.id
    lang = user_lang(user_id)
    balance = get_balance(user_id)
    refs = get_total_referrals(user_id)
    await update.message.reply_text(
        TEXT[lang]["balance"].format(
            balance=f"{balance:.5f}",
            refs=refs,
            email_reward=f"{EMAIL_REWARD:.5f}",
            minimum=MIN_WITHDRAW,
        ),
        reply_markup=balance_keyboard(lang),
        parse_mode="HTML",
    )


async def withdraw_command(update, context):
    user_id = update.effective_user.id
    lang = user_lang(user_id)
    t = TEXT[lang]
    balance = get_balance(user_id)

    if balance < MIN_WITHDRAW:
        await update.message.reply_text(
            t["withdraw_min"].format(balance=balance),
            parse_mode="HTML",
        )
        return

    context.user_data.pop("withdraw_method", None)
    context.user_data.pop("withdraw_amount", None)
    context.user_data.pop("waiting_withdraw_destination", None)

    await update.message.reply_text(
        t["withdraw_choose"],
        reply_markup=withdraw_method_keyboard(lang),
        parse_mode="HTML",
    )


async def withdraw_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    lang = user_lang(user_id)
    t = TEXT[lang]
    await query.answer()

    balance = get_balance(user_id)
    if balance < MIN_WITHDRAW:
        await query.message.reply_text(
            t["withdraw_min"].format(balance=balance),
            parse_mode="HTML",
        )
        return

    await query.message.reply_text(
        t["withdraw_choose"],
        reply_markup=withdraw_method_keyboard(lang),
        parse_mode="HTML",
    )


async def choose_withdraw_method(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    lang = user_lang(user_id)
    t = TEXT[lang]
    await query.answer()

    balance = get_balance(user_id)
    if balance < MIN_WITHDRAW:
        await query.message.reply_text(
            t["withdraw_min"].format(balance=balance),
            parse_mode="HTML",
        )
        return

    if query.data == "withdraw_method_binance":
        method = "Binance ID"
    else:
        method = "BEP20"

    context.user_data["withdraw_method"] = method
    context.user_data["withdraw_amount"] = balance
    context.user_data["waiting_withdraw_destination"] = True

    await query.message.reply_text(
        t["withdraw_address"].format(method=method),
        parse_mode="HTML",
    )


async def notify_admins_about_withdraw(
    bot, request_id, user_id, method, destination, amount
):
    admin_text = (
        "💸 <b>NEW WITHDRAWAL REQUEST</b>\n\n"
        f"🆔 Request ID: <code>#{request_id}</code>\n"
        f"👤 User ID: <code>{user_id}</code>\n"
        f"💵 Amount: <b>${amount:.5f}</b>\n"
        f"💳 Method: <b>{safe(method)}</b>\n"
        f"📥 Destination:\n<code>{safe(destination)}</code>\n\n"
        f"🕐 {dhaka_time()}"
    )
    keyboard = admin_withdraw_keyboard(request_id, "en")
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception as error:
            logger.error("Could not notify admin %s: %s", admin_id, error)


# ============================================================
# DASHBOARD
# ============================================================

async def dashboard_command(update, context):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(
            "🚫 <b>Access Denied!</b>\n\n"
            "⚠️ This command is available for administrators only.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        "👑 <b>ADMIN DASHBOARD</b>\n\n"
        "নিচের বাটনগুলো ব্যবহার করুন:",
        reply_markup=dashboard_keyboard(),
        parse_mode="HTML",
    )


async def dashboard_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data or ""

    if not is_admin(user_id):
        await query.answer("Admin only.", show_alert=True)
        return

    await query.answer()

    if data == "dash_reset_all":
        await query.message.reply_text(
            "⚠️ <b>Reset All</b>\n\n"
            "সব ইউজারের Balance 0 হয়ে যাবে এবং Referral সিস্টেম রিসেট হবে।\n\n"
            "আপনি কি নিশ্চিত?",
            reply_markup=reset_all_confirm_keyboard(),
            parse_mode="HTML",
        )
        return

    if data == "dash_reset_all_reject":
        await query.message.reply_text("❌ Reset All বাতিল করা হয়েছে।")
        return

    if data == "dash_reset_all_confirm":
        context.user_data["waiting_reset_all_reason"] = True
        await query.message.reply_text(
            "📝 <b>Reason লিখুন</b>\n\n"
            "কেন সব ইউজার রিসেট করতে চাচ্ছেন? Reason লিখে পাঠান:",
            parse_mode="HTML",
        )
        return

    if data == "dash_custom_reset":
        context.user_data["waiting_custom_reset_chatid"] = True
        await query.message.reply_text(
            "🎯 <b>Custom Reset</b>\n\n"
            "যে ইউজারের Balance 0 করতে চান তার <b>Chat ID</b> পাঠান:",
            parse_mode="HTML",
        )
        return

    if data == "dash_broadcast":
        context.user_data["waiting_dashboard_broadcast"] = True
        await query.message.reply_text(
            "📢 <b>Broadcast (সব User)</b>\n\n"
            "যে মেসেজ সব ইউজারকে পাঠাতে চান সেটা লিখে পাঠান:",
            parse_mode="HTML",
        )
        return

    if data == "dash_user_data":
        await query.message.reply_text("📊 User Data লোড হচ্ছে...")
        try:
            users = get_all_reward_users()
        except Exception as e:
            await query.message.reply_text(f"❌ Error: {e}")
            return

        if not users:
            await query.message.reply_text("কোনো ইউজার পাওয়া যায়নি।")
            return

        chunk = ""
        count = 0
        for u in users:
            uid = u["user_id"]
            balance = float(u["balance"])
            refs = int(u["total_referrals"] or 0)

            username = "N/A"
            full_name = "N/A"
            try:
                chat = await context.bot.get_chat(uid)
                username = f"@{chat.username}" if chat.username else "N/A"
                full_name = chat.full_name or "N/A"
            except Exception:
                pass

            line = (
                f"👤 <b>{safe(full_name)}</b>\n"
                f"🆔 <code>{uid}</code>\n"
                f"🔗 {safe(username)}\n"
                f"👥 Refer: <b>{refs}</b>\n"
                f"💰 Balance: <b>{balance:.5f}</b>\n"
                f"{'─' * 28}\n"
            )

            if len(chunk) + len(line) > 3800:
                await query.message.reply_text(chunk, parse_mode="HTML")
                chunk = ""
                await asyncio.sleep(0.3)

            chunk += line
            count += 1

        if chunk:
            await query.message.reply_text(chunk, parse_mode="HTML")

        await query.message.reply_text(
            f"✅ মোট <b>{count}</b> জন ইউজার দেখানো হয়েছে।",
            parse_mode="HTML",
        )
        return


# ============================================================
# /broadcast = Single User Message (Chat ID → Message)
# ============================================================

async def broadcast_command(update, context):
    """এখন /broadcast = একজন User কে মেসেজ পাঠানো"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "🚫 <b>Access Denied!</b>\n\n"
            "⚠️ This command is available for administrators only.",
            parse_mode="HTML",
        )
        return

    context.user_data.pop("waiting_broadcast_chatid", None)
    context.user_data.pop("waiting_broadcast_message", None)
    context.user_data.pop("broadcast_target", None)

    context.user_data["waiting_broadcast_chatid"] = True

    await update.message.reply_text(
        "📨 <b>Single User Message</b>\n\n"
        "যে ১ জন User-কে মেসেজ পাঠাতে চান তার <b>Chat ID</b> পাঠান:",
        parse_mode="HTML",
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):
    user = update.effective_user
    if not user or not update.message:
        return

    user_id = user.id
    lang = user_lang(user_id)
    t = TEXT[lang]
    text = update.message.text.strip()

    if is_admin(user_id):

        # Custom rejection reason
        custom_request_id = context.user_data.get("waiting_custom_reject_reason")
        if custom_request_id:
            request_id = int(custom_request_id)
            if not text:
                await update.message.reply_text("⚠️ Please write a rejection reason.")
                return
            if len(text) > 2000:
                await update.message.reply_text(
                    "⚠️ Rejection reason is too long.\nPlease keep it under 2000 characters."
                )
                return

            context.user_data[f"reject_reason_{request_id}"] = text
            context.user_data.pop("waiting_custom_reject_reason", None)

            await update.message.reply_text(
                "✅ <b>Custom reason saved.</b>\n\n"
                "⚠️ <b>Do you want to make this user's balance 0?</b>",
                reply_markup=balance_zero_keyboard(request_id),
                parse_mode="HTML",
            )
            return

        # Reset All Reason
        if context.user_data.get("waiting_reset_all_reason"):
            reason = text.strip()
            if not reason:
                await update.message.reply_text("⚠️ Reason লিখুন।")
                return

            context.user_data.pop("waiting_reset_all_reason", None)

            try:
                reset_all_balances(reason)
            except Exception as e:
                await update.message.reply_text(f"❌ Reset failed: {e}")
                return

            users = get_all_users()
            sent = 0
            for uid in users:
                try:
                    user_lang_code = user_lang(uid)
                    notice = TEXT[user_lang_code]["reset_notice"].format(
                        reason=safe(reason)
                    )
                    await context.bot.send_message(
                        chat_id=uid,
                        text=notice,
                        parse_mode="HTML",
                    )
                    sent += 1
                    await asyncio.sleep(0.05)
                except Exception:
                    pass

            await update.message.reply_text(
                f"✅ <b>Reset All সম্পন্ন!</b>\n\n"
                f"📌 Reason: {safe(reason)}\n"
                f"📤 Notification পাঠানো হয়েছে: <b>{sent}</b> জন",
                parse_mode="HTML",
            )
            return

        # Custom Reset: Chat ID
        if context.user_data.get("waiting_custom_reset_chatid"):
            try:
                target_id = int(text.strip())
            except ValueError:
                await update.message.reply_text("⚠️ সঠিক Chat ID দিন (শুধু সংখ্যা)।")
                return

            context.user_data["waiting_custom_reset_chatid"] = False
            context.user_data["custom_reset_target"] = target_id
            context.user_data["waiting_custom_reset_reason"] = True

            await update.message.reply_text(
                f"🎯 Target User: <code>{target_id}</code>\n\n"
                "এখন <b>Reason</b> লিখুন কেন রিসেট করতে চাচ্ছেন:",
                parse_mode="HTML",
            )
            return

        # Custom Reset: Reason
        if context.user_data.get("waiting_custom_reset_reason"):
            reason = text.strip()
            target_id = context.user_data.get("custom_reset_target")

            if not reason or not target_id:
                await update.message.reply_text("⚠️ কিছু সমস্যা হয়েছে। আবার চেষ্টা করুন।")
                context.user_data.clear()
                return

            context.user_data.pop("waiting_custom_reset_reason", None)
            context.user_data.pop("custom_reset_target", None)

            try:
                reset_single_user(target_id, reason)
            except Exception as e:
                await update.message.reply_text(f"❌ Reset failed: {e}")
                return

            try:
                user_lang_code = user_lang(target_id)
                notice = TEXT[user_lang_code]["reset_notice_single"].format(
                    reason=safe(reason)
                )
                await context.bot.send_message(
                    chat_id=target_id,
                    text=notice,
                    parse_mode="HTML",
                )
            except Exception:
                pass

            await update.message.reply_text(
                f"✅ <b>Custom Reset সম্পন্ন!</b>\n\n"
                f"👤 User: <code>{target_id}</code>\n"
                f"📌 Reason: {safe(reason)}\n"
                f"💰 Balance = 0 করা হয়েছে।",
                parse_mode="HTML",
            )
            return

        # Dashboard Broadcast (সব User)
        if context.user_data.get("waiting_dashboard_broadcast"):
            broadcast_text = text.strip()
            if not broadcast_text:
                await update.message.reply_text("⚠️ মেসেজ লিখুন।")
                return

            context.user_data.pop("waiting_dashboard_broadcast", None)

            users = get_all_users()
            sent = 0
            failed = 0
            for uid in users:
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=broadcast_text,
                        parse_mode="HTML",
                    )
                    sent += 1
                    await asyncio.sleep(0.05)
                except Exception:
                    failed += 1

            await update.message.reply_text(
                f"✅ <b>Broadcast সম্পন্ন!</b>\n\n"
                f"📤 Sent: <b>{sent}</b>\n"
                f"❌ Failed: <b>{failed}</b>",
                parse_mode="HTML",
            )
            return

        # /broadcast = Single User: Chat ID
        if context.user_data.get("waiting_broadcast_chatid"):
            try:
                target_id = int(text.strip())
            except ValueError:
                await update.message.reply_text(
                    "⚠️ সঠিক Chat ID দিন (শুধু সংখ্যা)।"
                )
                return

            context.user_data["waiting_broadcast_chatid"] = False
            context.user_data["broadcast_target"] = target_id
            context.user_data["waiting_broadcast_message"] = True

            await update.message.reply_text(
                f"✅ Target User: <code>{target_id}</code>\n\n"
                "এখন যে <b>মেসেজ</b> পাঠাতে চান সেটা লিখে পাঠান:",
                parse_mode="HTML",
            )
            return

        # /broadcast = Single User: Message
        if context.user_data.get("waiting_broadcast_message"):
            target_id = context.user_data.get("broadcast_target")
            message_text = text.strip()

            if not target_id or not message_text:
                await update.message.reply_text("⚠️ কিছু সমস্যা হয়েছে। আবার চেষ্টা করুন।")
                context.user_data.clear()
                return

            context.user_data.pop("waiting_broadcast_message", None)
            context.user_data.pop("broadcast_target", None)

            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=message_text,
                    parse_mode="HTML",
                )
                await update.message.reply_text(
                    f"✅ <b>মেসেজ সফলভাবে পাঠানো হয়েছে!</b>\n\n"
                    f"👤 User: <code>{target_id}</code>",
                    parse_mode="HTML",
                )
            except Exception as e:
                await update.message.reply_text(
                    f"❌ মেসেজ পাঠানো যায়নি।\n\n"
                    f"Error: {safe(str(e))}",
                    parse_mode="HTML",
                )
            return

    # Withdraw destination
    if context.user_data.get("waiting_withdraw_destination"):
        method = context.user_data.get("withdraw_method")
        amount = float(context.user_data.get("withdraw_amount", 0))

        if method not in ("Binance ID", "BEP20"):
            context.user_data.clear()
            await update.message.reply_text(t["withdraw_invalid"], parse_mode="HTML")
            return

        destination = text
        if not destination or len(destination) > 150:
            await update.message.reply_text(t["withdraw_invalid"], parse_mode="HTML")
            return

        if method == "BEP20":
            if not re.fullmatch(r"0x[a-fA-F0-9]{40}", destination):
                await update.message.reply_text(
                    "⚠️ Invalid BEP20 address.\n\nExample format: <code>0x...</code>",
                    parse_mode="HTML",
                )
                return

        if method == "Binance ID":
            if not re.fullmatch(r"[A-Za-z0-9._@-]{3,100}", destination):
                await update.message.reply_text(t["withdraw_invalid"], parse_mode="HTML")
                return

        current_balance = get_balance(user_id)
        if current_balance < MIN_WITHDRAW:
            context.user_data.clear()
            await update.message.reply_text(
                t["withdraw_min"].format(balance=current_balance),
                parse_mode="HTML",
            )
            return

        amount = current_balance
        request_id, result = create_withdraw_request(
            user_id, method, destination, amount
        )

        if result == "MINIMUM":
            context.user_data.clear()
            await update.message.reply_text(
                t["withdraw_min"].format(balance=current_balance),
                parse_mode="HTML",
            )
            return

        if result != "OK" or not request_id:
            await update.message.reply_text(t["api_error"], parse_mode="HTML")
            return

        context.user_data.clear()
        await update.message.reply_text(
            t["withdraw_sent"].format(
                amount=amount,
                method=safe(method),
                destination=safe(destination),
            ),
            parse_mode="HTML",
        )
        await notify_admins_about_withdraw(
            context.bot, request_id, user_id, method, destination, amount
        )
        return

    # Reply keyboard
    if text in [
        t["generate"],
        "➕ Generate New",
        "➕ নতুন তৈরি করুন",
        "➕ Naya Generate",
    ]:
        await generate_new(update.message, user_id, lang)
        return

    if text in [t["inbox"], "📥 Inbox", "📥 ইনবক্স"]:
        await show_inbox(update.message, user_id, lang)
        return

    if text in [t["refresh"], "🔄 Refresh", "🔄 রিফ্রেশ"]:
        await show_inbox(update.message, user_id, lang)
        return

    if text in [
        t["refer_btn"],
        "👥 Refer System",
        "👥 রেফার সিস্টেম",
        "/refer",
    ]:
        await refer_command(update, context)
        return


# ============================================================
# ADMIN WITHDRAW
# ============================================================

async def admin_withdraw_action(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    data = query.data or ""
    try:
        action, request_id_text = data.split(":", 1)
        request_id = int(request_id_text)
    except Exception:
        await query.answer("Invalid request.", show_alert=True)
        return

    request = get_withdraw_request(request_id)
    if not request:
        await query.answer("Request not found.", show_alert=True)
        return

    current_status = str(request["status"])
    if current_status != "PENDING":
        await query.answer(f"Already {current_status}.", show_alert=True)
        return

    user_id = int(request["user_id"])
    amount = float(request["amount"])
    method = str(request["method"])
    destination = str(request["destination"])

    if action == "withdraw_accept":
        success = update_withdraw_status(request_id, "APPROVED")
        if not success:
            await query.answer("Request already processed.", show_alert=True)
            return

        await query.answer("Withdrawal approved.", show_alert=True)

        try:
            user_lang_code = user_lang(user_id)
            await context.bot.send_message(
                chat_id=user_id,
                text=TEXT[user_lang_code]["withdraw_congratulations"].format(
                    amount=amount,
                    method=safe(method),
                    destination=safe(destination),
                ),
                parse_mode="HTML",
            )
        except Exception as error:
            logger.error("Approval notification failed: %s", error)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        try:
            await query.message.reply_text(f"✅ Request #{request_id} approved.")
        except Exception:
            pass
        return

    if action == "withdraw_reject":
        await query.answer()
        try:
            await query.edit_message_reply_markup(
                reply_markup=rejection_reason_keyboard(request_id)
            )
        except Exception as error:
            logger.error("Could not show rejection keyboard: %s", error)

        try:
            await query.message.reply_text(
                TEXT["en"]["reject_select"],
                reply_markup=rejection_reason_keyboard(request_id),
                parse_mode="HTML",
            )
        except Exception as error:
            logger.error("Could not send rejection reason menu: %s", error)
        return


async def handle_rejection_reason(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    data = query.data or ""
    try:
        prefix, request_id_text = data.split(":", 1)
        request_id = int(request_id_text)
    except Exception:
        await query.answer("Invalid request.", show_alert=True)
        return

    request = get_withdraw_request(request_id)
    if not request:
        await query.answer("Request not found.", show_alert=True)
        return

    if str(request["status"]) != "PENDING":
        await query.answer("This request is already processed.", show_alert=True)
        return

    if prefix == "reject_reason_custom":
        await query.answer()
        context.user_data["waiting_custom_reject_reason"] = request_id
        await query.message.reply_text(
            TEXT["en"]["reject_custom_prompt"],
            parse_mode="HTML",
        )
        return

    if prefix.startswith("reject_reason_"):
        reason_lang = prefix.replace("reject_reason_", "", 1)
        if reason_lang not in ("bn", "en", "hi"):
            await query.answer("Invalid reason.", show_alert=True)
            return

        reason = REJECTION_REASONS[reason_lang]
        context.user_data[f"reject_reason_{request_id}"] = reason
        context.user_data[f"reject_reason_lang_{request_id}"] = reason_lang

        await query.answer("Reason selected.", show_alert=False)
        await query.message.reply_text(
            TEXT["en"]["balance_zero_question"],
            reply_markup=balance_zero_keyboard(request_id),
            parse_mode="HTML",
        )
        return


async def handle_balance_action(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    data = query.data or ""
    try:
        action, request_id_text = data.split(":", 1)
        request_id = int(request_id_text)
    except Exception:
        await query.answer("Invalid request.", show_alert=True)
        return

    request = get_withdraw_request(request_id)
    if not request:
        await query.answer("Request not found.", show_alert=True)
        return

    if str(request["status"]) != "PENDING":
        await query.answer("This request is already processed.", show_alert=True)
        return

    user_id = int(request["user_id"])
    amount = float(request["amount"])

    reason = context.user_data.get(f"reject_reason_{request_id}")
    if not reason:
        await query.answer("Please select a rejection reason first.", show_alert=True)
        return

    if action == "withdraw_zero_yes":
        success = reject_withdraw_final(request_id, reason, zero_balance=True)
        if not success:
            await query.answer("Request already processed.", show_alert=True)
            return

        await query.answer("Rejected. Balance set to 0.", show_alert=True)
        balance_message = "💰 Your balance has been set to <b>0</b>."

        try:
            user_lang_code = user_lang(user_id)
            await context.bot.send_message(
                chat_id=user_id,
                text=TEXT[user_lang_code]["withdraw_rejected"].format(
                    amount=amount,
                    reason=safe(reason),
                    balance_message=balance_message,
                ),
                parse_mode="HTML",
            )
        except Exception as error:
            logger.error("Reject notification failed: %s", error)

        context.user_data.pop(f"reject_reason_{request_id}", None)
        context.user_data.pop(f"reject_reason_lang_{request_id}", None)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        try:
            await query.message.reply_text(
                TEXT["en"]["admin_rejected_zero"].format(request_id=request_id)
            )
        except Exception:
            pass
        return

    if action == "withdraw_zero_no":
        success = reject_withdraw_final(request_id, reason, zero_balance=False)
        if not success:
            await query.answer("Request already processed.", show_alert=True)
            return

        await query.answer("Rejected. Withdrawal amount refunded.", show_alert=True)
        balance_message = (
            "💰 The withdrawal amount has been <b>returned to your balance</b>."
        )

        try:
            user_lang_code = user_lang(user_id)
            await context.bot.send_message(
                chat_id=user_id,
                text=TEXT[user_lang_code]["withdraw_rejected"].format(
                    amount=amount,
                    reason=safe(reason),
                    balance_message=balance_message,
                ),
                parse_mode="HTML",
            )
        except Exception as error:
            logger.error("Reject notification failed: %s", error)

        context.user_data.pop(f"reject_reason_{request_id}", None)
        context.user_data.pop(f"reject_reason_lang_{request_id}", None)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        try:
            await query.message.reply_text(
                TEXT["en"]["admin_rejected_refund"].format(request_id=request_id)
            )
        except Exception:
            pass
        return


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def callback_handler(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data or ""

    if data.startswith("dash_"):
        await dashboard_callback(update, context)
        return

    if data.startswith("withdraw_accept:") or data.startswith("withdraw_reject:"):
        await admin_withdraw_action(update, context)
        return

    if data.startswith("reject_reason_"):
        await handle_rejection_reason(update, context)
        return

    if data.startswith("withdraw_zero_yes:") or data.startswith("withdraw_zero_no:"):
        await handle_balance_action(update, context)
        return

    if data in ("withdraw_method_binance", "withdraw_method_bep20"):
        await choose_withdraw_method(update, context)
        return

    if data.startswith("lang_"):
        lang = data.replace("lang_", "", 1)
        if lang not in TEXT:
            lang = "en"
        await query.answer()
        set_language(user_id, lang)
        ensure_reward_user(user_id)
        try:
            await query.edit_message_text(
                TEXT[lang]["language_ok"],
                parse_mode="HTML",
            )
        except Exception:
            pass
        await generate_new(query.message, user_id, lang)
        return

    if data == "generate":
        await query.answer()
        lang = user_lang(user_id)
        await generate_new(query.message, user_id, lang)
        return

    if data == "inbox":
        await query.answer()
        lang = user_lang(user_id)
        await show_inbox(query.message, user_id, lang)
        return

    if data == "refresh":
        await query.answer()
        lang = user_lang(user_id)
        await show_inbox(query.message, user_id, lang)
        return

    if data == "withdraw":
        await withdraw_callback(update, context)
        return

    await query.answer()


# ============================================================
# STATS / ADMIN
# ============================================================

async def stats_command(update, context):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(
            "🚫 <b>Access Denied!</b>\n\n"
            "⚠️ This command is available for administrators only.",
            parse_mode="HTML",
        )
        return

    lang = user_lang(user_id)
    try:
        users = get_all_users()
        total_users = len(users)
        total_mailboxes = 0
        for uid in users:
            try:
                mailbox = get_mailbox(uid)
                if mailbox:
                    total_mailboxes += 1
            except Exception:
                continue
    except Exception as error:
        logger.error("Stats error: %s", error)
        total_users = 0
        total_mailboxes = 0

    await update.message.reply_text(
        TEXT[lang]["stats"].format(
            users=total_users,
            mailboxes=total_mailboxes,
            seconds=POLL_SECONDS,
        ),
        parse_mode="HTML",
    )


async def admin_only(update):
    user_id = update.effective_user.id
    lang = user_lang(user_id)
    await update.message.reply_text(
        TEXT[lang]["admin_only"],
        parse_mode="HTML",
    )


async def admin_command(update, context):
    if not is_admin(update.effective_user.id):
        await admin_only(update)
        return
    lang = user_lang(update.effective_user.id)
    await update.message.reply_text(
        TEXT[lang]["admin_panel"],
        parse_mode="HTML",
    )


# ============================================================
# AUTO INBOX
# ============================================================

async def auto_inbox_job(context):
    try:
        users = get_all_users()
    except Exception as error:
        logger.error("Could not load users: %s", error)
        return

    for user_id in users:
        try:
            mailbox = get_mailbox(user_id)
            if not mailbox:
                continue

            token = mailbox.get("token")
            if not token:
                continue

            if KNOWN_MAILBOX.get(user_id) != token:
                KNOWN_MAILBOX[user_id] = token
                SEEN_MESSAGES[user_id] = set()

            data = await get_messages(token)
            if not data:
                continue

            messages = extract_messages(data)
            if not messages:
                continue

            seen = SEEN_MESSAGES.setdefault(user_id, set())
            lang = user_lang(user_id)

            for item in messages:
                message_id = get_message_id(item)
                if not message_id or message_id in seen:
                    continue
                seen.add(message_id)
                try:
                    await send_auto_mail(context.bot, user_id, item, lang)
                    logger.info("New email sent automatically -> %s", user_id)
                except Exception as error:
                    logger.error("Auto send error user=%s: %s", user_id, error)

            if len(seen) > 200:
                SEEN_MESSAGES[user_id] = set(list(seen)[-100:])

        except Exception as error:
            logger.error("Auto inbox error user=%s: %s", user_id, error)

        await asyncio.sleep(0.05)


async def error_handler(update, context):
    logger.error("Unhandled error", exc_info=context.error)


async def post_init(application):
    init_reward_db()
    if not application.job_queue:
        logger.error("JobQueue unavailable.")
        logger.error("Install: python-telegram-bot[job-queue]")
        return

    application.job_queue.run_repeating(
        auto_inbox_job,
        interval=POLL_SECONDS,
        first=2,
        name="auto-inbox",
    )
    logger.info("Automatic inbox started: every %s seconds", POLL_SECONDS)
# ============================================================
# MAIN
# ============================================================

def main():
    init_db()
    init_reward_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("inbox", inbox_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("refer", refer_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("withdraw", withdraw_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("dashboard", dashboard_command))

    application.add_handler(CallbackQueryHandler(callback_handler))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(error_handler)

    print("🤖 Temp Mail Bot is running...")
    print(f"📩 Auto inbox: every {POLL_SECONDS}s")

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
