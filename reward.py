# ============================================================
# reward.py
# TEMP MAIL BOT - REWARD / REFERRAL SYSTEM
# ============================================================

from decimal import Decimal, InvalidOperation

# Reward amounts (bot.py এর সাথে মিল রেখে)
REFERRAL_REWARD = Decimal("0.00158")
EMAIL_CODE_REWARD = Decimal("0.00130")


def money(value):
    """Safely convert a value to Decimal."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def referral_reward_amount():
    """Return reward amount for one successful referral."""
    return REFERRAL_REWARD


def email_code_reward_amount():
    """Return reward amount for one unique email/code reward."""
    return EMAIL_CODE_REWARD


def calculate_referral_reward(count=1):
    """Calculate total referral reward."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0

    if count < 0:
        count = 0

    return REFERRAL_REWARD * count


def calculate_email_reward(count=1):
    """Calculate total email-code reward."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0

    if count < 0:
        count = 0

    return EMAIL_CODE_REWARD * count


def format_reward(amount):
    """Format reward/balance nicely for Telegram."""
    amount = money(amount)
    return f"{amount:.5f}"


def reward_summary(referrals=0, emails=0):
    """Return a simple reward summary."""
    referral_total = calculate_referral_reward(referrals)
    email_total = calculate_email_reward(emails)
    total = referral_total + email_total

    return {
        "referrals": int(referrals),
        "emails": int(emails),
        "referral_reward": referral_total,
        "email_reward": email_total,
        "total": total,
        "formatted_referral_reward": format_reward(referral_total),
        "formatted_email_reward": format_reward(email_total),
        "formatted_total": format_reward(total),
    }


# ------------------------------------------------------------
# IMPORTANT:
# database.py / bot.py should be responsible for actually saving
# rewards to SQLite.
# This file only keeps reward rules/calculations in one place.
# ------------------------------------------------------------

__all__ = [
    "REFERRAL_REWARD",
    "EMAIL_CODE_REWARD",
    "money",
    "referral_reward_amount",
    "email_code_reward_amount",
    "calculate_referral_reward",
    "calculate_email_reward",
    "format_reward",
    "reward_summary",
    ]
