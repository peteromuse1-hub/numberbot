import re
import asyncio
import json

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import requests
import time
import os
from datetime import datetime, timezone, timedelta

# Kenya / East Africa Time is a fixed UTC+3 offset with no daylight saving,
# so a plain fixed offset is more reliable here than relying on the host's
# tzdata (which minimal containers on Railway sometimes lack).
EAT = timezone(timedelta(hours=3), name="EAT")

# Matches Kenyan mobile numbers like 0712345678 or +254712345678
# Matches Kenyan mobile numbers, both the older 07xxxxxxxx (Safaricom/Airtel)
# and newer 01xxxxxxxx ranges, in local or +254 format.
PHONE_RE = re.compile(r"^(?:\+254|0)[71]\d{8}$")

# Secrets are read from environment variables — never hardcode them.
# Set these before running, e.g.:
#   export BOT_TOKEN="your_telegram_bot_token"
#   export HEROSMS_API_KEY="your_herosms_api_key"
#   export PAYSTACK_SECRET_KEY="your_paystack_secret_key"
BOT_TOKEN = os.getenv("BOT_TOKEN")
HEROSMS_API_KEY = os.getenv("HEROSMS_API_KEY")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
HEROSMS_URL = "https://hero-sms.com/stubs/handler_api.php"

if not BOT_TOKEN or not HEROSMS_API_KEY or not PAYSTACK_SECRET_KEY:
    raise RuntimeError(
        "Missing required environment variables. "
        "Set BOT_TOKEN, HEROSMS_API_KEY, and PAYSTACK_SECRET_KEY before running."
    )

# Your selected settings
COUNTRY = 8
SERVICE = "vi"
MAX_PRICE = 0.022

# 20 minutes
VALIDITY_MINUTES = 20

# What you charge the user, in Kenyan Shillings, per number.
# Override with an env var if you want to change price without editing code.
PRICE_KES = float(os.getenv("PRICE_KES", "1"))  # Testing price — raise this before going live.

# Where the "Help" button sends users when they can't get a code at all.
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@YourUsernameHere")

# A free replacement number becomes available this long after purchase,
# if no code has arrived — reachable via the HELP button, not pushed automatically.
REPLACEMENT_WAIT_SECONDS = 210  # 3.5 minutes

# Only one free replacement allowed per original paid purchase.
MAX_REPLACEMENTS = 1

PAYSTACK_BASE_URL = "https://api.paystack.co"

# How long to wait for the user to approve the M-Pesa STK push prompt.
PAYMENT_POLL_INTERVAL_SECONDS = 5
PAYMENT_POLL_MAX_ATTEMPTS = 12  # 12 * 5s = 60s total


def initiate_mpesa_charge(phone: str, amount_kes: float):
    """
    Starts an M-Pesa STK push via Paystack for the given phone number.
    Returns (reference, error). If error is not None, reference is None.
    """
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }

    # Paystack requires an email even for mobile money charges; we don't
    # collect one from the user, so we synthesize a placeholder.
    normalized_phone = phone if phone.startswith("+254") else "+254" + phone.lstrip("0")

    payload = {
        "email": f"{normalized_phone.lstrip('+')}@example.com",
        "amount": int(round(amount_kes * 100)),  # Paystack expects the amount in kobo/cents
        "currency": "KES",
        "mobile_money": {
            "phone": normalized_phone,
            "provider": "mpesa",
        },
    }

    try:
        response = requests.post(
            f"{PAYSTACK_BASE_URL}/charge",
            json=payload,
            headers=headers,
            timeout=15,
        )
        data = response.json()
    except Exception as e:
        return None, f"Connection error contacting Paystack: {e}"

    if not data.get("status"):
        return None, data.get("message", "Payment could not be started.")

    reference = data.get("data", {}).get("reference")
    if not reference:
        return None, "Paystack did not return a payment reference."

    return reference, None


def verify_paystack_charge(reference: str):
    """
    Checks the status of a previously-initiated charge.
    Returns one of: "success", "failed", "pending", or "error".
    """
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}

    try:
        response = requests.get(
            f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}",
            headers=headers,
            timeout=15,
        )
        data = response.json()
    except Exception:
        return "error"

    if not data.get("status"):
        return "error"

    tx_status = data.get("data", {}).get("status")

    if tx_status == "success":
        return "success"
    elif tx_status in ("failed", "abandoned", "reversed"):
        return "failed"
    else:
        return "pending"


async def edit_main_menu(message):
    user = message.from_user
    display_name = f"@{user.username}" if user.username else user.first_name

    keyboard = [
        [InlineKeyboardButton("🟣 GET NUMBER", callback_data="get_number")],
        [
            InlineKeyboardButton("🔵 MY NUMBER", callback_data="my_number"),
            InlineKeyboardButton("🟢 CHECK STATUS", callback_data="check_status"),
        ],
        [InlineKeyboardButton("🔴 HELP", callback_data="help")],
    ]

    text = (
        f"👋 Hi {display_name}!\n\n"
        f"Get a temporary number for just KES {PRICE_KES:.0f}.\n\n"
        "⏱️ You have 20 minutes from the time of purchase to use your number."
    )

    try:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Prefer their Telegram username; fall back to first name if they don't have one set.
    display_name = f"@{user.username}" if user.username else user.first_name

    await edit_main_menu(update.message)


async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "get_number":
        context.user_data["awaiting_phone"] = True
        keyboard = [[InlineKeyboardButton("⬅️ BACK", callback_data="back_main")]]
        await query.edit_message_text(
            "📱 *GET NUMBER*\n\n"
            "Please enter your M-Pesa phone number.\n\n"
            "Example:\n`0712345678`",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    elif query.data == "my_number":
        await show_number(query.message, context, edit=True)

    elif query.data == "check_status":
        await check_status(query.message, context, edit=True)

    elif query.data == "help":
        await send_help(query.message, context, edit=True)

    elif query.data == "request_replacement":
        await request_replacement_number(query.message, context, edit=True)

    elif query.data == "back_main":
        context.user_data["awaiting_phone"] = False
        await edit_main_menu(query.message)

    elif query.data == "back_number":
        await show_number(query.message, context, edit=True)


async def send_help(message, context, edit=False):
    async def send(text, **kwargs):
        if edit:
            try:
                await message.edit_text(text, **kwargs)
                return
            except Exception:
                pass
        await send(text, **kwargs)

    activation_id = context.user_data.get("activation_id")
    purchase_time = context.user_data.get("purchase_time")
    replacement_count = context.user_data.get("replacement_count", 0)

    lines = ["🆘 *NEED HELP?*", ""]
    keyboard = []

    if activation_id and purchase_time:
        elapsed = time.time() - purchase_time

        if replacement_count >= MAX_REPLACEMENTS:
            lines.append(
                "You've already used your one free replacement number for this purchase."
            )
            lines.append(f"Please message {ADMIN_CONTACT} for further help.")

        elif elapsed < REPLACEMENT_WAIT_SECONDS:
            remaining = int(REPLACEMENT_WAIT_SECONDS - elapsed)
            mins, secs = divmod(remaining, 60)
            lines.append(
                f"If your number hasn't received a code, you can request a free "
                f"replacement in {mins}m {secs}s."
            )
            lines.append(f"Need help right now? Message {ADMIN_CONTACT}")

        else:
            lines.append(
                "Still no code? Grab a free replacement number below — this "
                "cancels your current number and issues a new one on a "
                "different network."
            )
            keyboard.append(
                [InlineKeyboardButton("🟠 GET REPLACEMENT NUMBER", callback_data="request_replacement")]
            )
    else:
        lines.append(f"Need a hand? Message us directly: {ADMIN_CONTACT}")

    lines.append("")
    lines.append(
        "Please include your phone number and the number you were issued "
        "so we can look it up quickly."
    )

    await send(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        parse_mode="Markdown",
    )


async def handle_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the M-Pesa phone number the user types after pressing GET NUMBER."""
    if not context.user_data.get("awaiting_phone"):
        # Not expecting text input right now; ignore.
        return

    phone = update.message.text.strip()

    if not PHONE_RE.match(phone):
        await update.message.reply_text(
            "❌ That doesn't look like a valid M-Pesa number.\n\n"
            "Please enter it like this:\n0712345678"
        )
        return

    context.user_data["awaiting_phone"] = False
    context.user_data["phone_number"] = phone

    await update.message.reply_text(
        f"💳 Sending a payment prompt of KES {PRICE_KES:.0f} to {phone}.\n\n"
        "Please approve the M-Pesa prompt on your phone within 60 seconds."
    )

    reference, error = initiate_mpesa_charge(phone, PRICE_KES)

    if error:
        await update.message.reply_text(
            f"❌ Could not start payment:\n{error}"
        )
        return

    context.user_data["payment_reference"] = reference

    for _ in range(PAYMENT_POLL_MAX_ATTEMPTS):
        await asyncio.sleep(PAYMENT_POLL_INTERVAL_SECONDS)
        status = verify_paystack_charge(reference)

        if status == "success":
            context.user_data["paid"] = True
            await update.message.reply_text("✅ Payment received!")
            await get_number(update.message, context)
            return

        if status == "failed":
            await update.message.reply_text(
                "❌ Payment failed or was cancelled. No number was issued.\n\n"
                "Tap GET NUMBER to try again."
            )
            return

        # status == "pending" or "error": keep polling until attempts run out

    await update.message.reply_text(
        "⌛ We didn't receive payment confirmation in time.\n\n"
        "If you approved the prompt, check CHECK STATUS shortly. "
        "Otherwise tap GET NUMBER to try again."
    )


def cancel_activation(activation_id: str):
    """Tells HeroSMS to cancel a number so it can't receive a late code after we've moved on."""
    try:
        requests.get(
            HEROSMS_URL,
            params={
                "api_key": HEROSMS_API_KEY,
                "action": "setStatus",
                "id": activation_id,
                "status": 8,  # 8 = cancel
            },
            timeout=15,
        )
    except Exception:
        # Best-effort; if this fails we still proceed with issuing a new number.
        pass


async def purchase_number(message, context, operator=None, require_payment=True):
    """
    Requests a number from HeroSMS and reports the result to the user.
    Set require_payment=False for a free replacement (already paid for the original).
    """
    if require_payment and not context.user_data.get("paid"):
        await message.reply_text(
            "❌ Payment not confirmed for this number. Please try again."
        )
        return

    await message.reply_text(
        "⏳ Checking for a number..."
    )

    params = {
        "api_key": HEROSMS_API_KEY,
        "action": "getNumber",
        "service": SERVICE,
        "country": COUNTRY,
        "maxPrice": MAX_PRICE,
        "fixedPrice": "true",
    }
    if operator:
        params["operator"] = operator

    try:
        response = requests.get(
            HEROSMS_URL,
            params=params,
            timeout=15
        )

        result = response.text.strip()

        if result.startswith("ACCESS_NUMBER:"):
            parts = result.split(":")

            if len(parts) >= 3:
                activation_id = parts[1]
                number = parts[2]

                context.user_data["activation_id"] = activation_id
                context.user_data["number"] = number
                # Require a fresh payment before another *paid* number can be issued.
                context.user_data["paid"] = False
                if require_payment:
                    # This is the original paid purchase — reset the replacement
                    # allowance. A replacement purchase itself must NOT reset this,
                    # otherwise the 1-use cap could never actually bind.
                    context.user_data["replacement_count"] = 0

                keyboard = [
                    [
                        InlineKeyboardButton(
                            "🟢 CHECK STATUS FOR CODE",
                            callback_data="check_status"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔵 MY NUMBER",
                            callback_data="my_number"
                        ),
                        InlineKeyboardButton(
                            "🔴 HELP",
                            callback_data="help"
                        ),
                    ]
                ]

                # Send first, then anchor the 20-minute window to the timestamp
                # Telegram's own servers stamped on this message — not the local
                # system clock, which can drift (especially on a phone or a VPS
                # that isn't perfectly NTP-synced).
                sent = await message.reply_text(
                    "✅ *NUMBER PURCHASED*\n\n"
                    f"📱 Number: `{number}`\n\n"
                    "⚠️ You have 20 minutes from now to use this number.\n\n"
                    "👉 *Tap CHECK STATUS to see if your code has arrived.* "
                    "The code isn't sent automatically — you'll need to check "
                    "yourself. If nothing shows up after 3.5 minutes, tap "
                    "🔴 HELP for a free replacement number.",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )

                sent_date = sent.date
                if sent_date.tzinfo is None:
                    sent_date = sent_date.replace(tzinfo=timezone.utc)
                purchase_time = sent_date.timestamp()
                expiry_time = purchase_time + (VALIDITY_MINUTES * 60)

                context.user_data["purchase_time"] = purchase_time
                context.user_data["expiry_time"] = expiry_time

                purchased_eat = datetime.fromtimestamp(purchase_time, EAT).strftime("%H:%M:%S")
                expires_eat = datetime.fromtimestamp(expiry_time, EAT).strftime("%H:%M:%S")

                await message.reply_text(
                    f"🕐 Purchased: {purchased_eat} EAT\n"
                    f"⏰ Expires: {expires_eat} EAT"
                )


            else:
                await message.reply_text(
                    "❌ The number service returned "
                    "an unexpected response."
                )

        elif result == "NO_NUMBERS":
            await message.reply_text(
                "❌ No numbers are currently available."
            )

        else:
            await message.reply_text(
                f"❌ Number request failed:\n{result}"
            )

    except Exception as e:
        await message.reply_text(
            f"❌ Connection error:\n{e}"
        )


async def get_number(message, context):
    await purchase_number(message, context, require_payment=True)


async def request_replacement_number(message, context, edit=False):
    async def send(text, **kwargs):
        if edit:
            try:
                await message.edit_text(text, **kwargs)
                return
            except Exception:
                pass
        await send(text, **kwargs)

    """
    Reached only via the HELP button, and only once the eligibility checks
    in send_help have already passed once — but we re-check here too, since
    this can be triggered by a stale button tap. The user already paid for
    the original number, so this issues one free replacement — on the
    telkom operator under the same viber service, so it's not drawn from
    the same dead pool — and cancels the old activation immediately so it
    can't still deliver a late code and cause "did I buy two numbers?" confusion.
    Only one replacement is allowed per original purchase.
    """
    activation_id = context.user_data.get("activation_id")
    purchase_time = context.user_data.get("purchase_time")
    replacement_count = context.user_data.get("replacement_count", 0)

    if not activation_id or not purchase_time:
        await send(
            "ℹ️ You don't currently have an active number to replace."
        )
        return

    if replacement_count >= MAX_REPLACEMENTS:
        await send(
            "❌ You've already used your one free replacement for this purchase.\n\n"
            f"Please message {ADMIN_CONTACT} for further help."
        )
        return

    elapsed = time.time() - purchase_time
    if elapsed < REPLACEMENT_WAIT_SECONDS:
        remaining = int(REPLACEMENT_WAIT_SECONDS - elapsed)
        mins, secs = divmod(remaining, 60)
        await send(
            f"⏳ Replacements open up {mins}m {secs}s from now — "
            "please wait a little longer or use CHECK STATUS."
        )
        return

    # Confirm with HeroSMS that a code genuinely hasn't arrived before
    # handing out a free replacement — don't just trust the clock.
    await send(
        "🔍 Double-checking your current number for a code first..."
    )

    try:
        response = requests.get(
            HEROSMS_URL,
            params={
                "action": "getStatusV2",
                "id": activation_id,
                "api_key": HEROSMS_API_KEY,
            },
            timeout=15
        )
        kind, text = classify_status(response.text.strip())
    except Exception as e:
        await send(
            f"❌ Couldn't verify your number's status:\n{e}\n\nPlease try again."
        )
        return

    if kind == "code":
        await send(
            f"✅ A code already arrived: `{text}`\n\n"
            "No replacement needed — check MY NUMBER.",
            parse_mode="Markdown",
        )
        return

    if kind == "cancelled":
        await send(
            "ℹ️ This number was already cancelled. "
            f"Please message {ADMIN_CONTACT} for help."
        )
        return

    # Genuinely no code yet — eligible. Cancel the old number immediately
    # so it can't sneak in a late code.
    cancel_activation(activation_id)
    context.user_data["replacement_count"] = replacement_count + 1

    await send(
        "🔄 Confirmed no code yet — getting you a replacement number on "
        "the Telkom network..."
    )

    await purchase_number(message, context, operator="telkom", require_payment=False)


async def show_number(message, context, edit=False):
    async def send(text, **kwargs):
        if edit:
            try:
                await message.edit_text(text, **kwargs)
                return
            except Exception:
                pass
        await send(text, **kwargs)

    number = context.user_data.get("number")
    purchase_time = context.user_data.get("purchase_time")
    expiry_time = context.user_data.get("expiry_time")

    if not number or not purchase_time or not expiry_time:
        await send(
            "ℹ️ You don't currently have a number."
        )
        return

    now = time.time()
    remaining = expiry_time - now

    purchased = datetime.fromtimestamp(
        purchase_time, EAT
    ).strftime("%H:%M:%S")

    expires = datetime.fromtimestamp(
        expiry_time, EAT
    ).strftime("%H:%M:%S")

    if remaining <= 0:
        await send(
            "⏰ YOUR NUMBER HAS EXPIRED\n\n"
            f"📱 Number: {number}\n"
            f"🕐 Purchased: {purchased} EAT\n"
            f"⏰ Expired: {expires} EAT"
        )
        return

    minutes = int(remaining // 60)
    seconds = int(remaining % 60)

    await send(
        "📱 YOUR NUMBER\n\n"
        f"{number}\n\n"
        f"🕐 Purchased: {purchased} EAT\n"
        f"⏰ Expires: {expires} EAT\n"
        f"⌛ Time remaining: {minutes}m {seconds}s"
    )


def extract_code(raw: str) -> str:
    """
    HeroSMS sometimes returns the full SMS text after STATUS_OK: instead of
    just the code. Pull out the actual numeric code rather than showing the
    whole message body.
    """
    match = re.search(r"\b\d{4}\b", raw)
    if match:
        return match.group(0)

    match = re.search(r"\d{4,8}", raw)
    if match:
        return match.group(0)

    return raw.strip()


def classify_status(result: str):
    """
    Turns a raw HeroSMS status response into (kind, text).
    kind is one of: "code", "waiting", "cancelled", "other", "empty".

    Handles multiple API shapes HeroSMS may return:
    - legacy plain text, e.g. "STATUS_OK:1234"
    - flat JSON, e.g. {"status": "STATUS_OK", "code": "1234"}
    - nested JSON (the real shape actually seen), e.g.
      {"verificationType":0,"sms":{"code":"9260","text":"..."},"call":null}
    Without handling the nested shape, this fell through to "other" and
    dumped the entire raw blob at the user instead of a clean 4-digit code.
    """
    raw = result.strip()

    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None

    if isinstance(data, dict):
        status = str(data.get("status", "")).upper()

        code_field = data.get("code") or data.get("sms_code") or data.get("otp")
        text_field = data.get("text") or data.get("message")

        sms_obj = data.get("sms") if isinstance(data.get("sms"), dict) else None
        call_obj = data.get("call") if isinstance(data.get("call"), dict) else None

        if not code_field and sms_obj:
            code_field = sms_obj.get("code")
            text_field = text_field or sms_obj.get("text")

        if not code_field and call_obj:
            code_field = call_obj.get("code")
            text_field = text_field or call_obj.get("text")

        if status == "STATUS_OK" or code_field:
            source = str(code_field) if code_field else (text_field or raw)
            return "code", extract_code(source)
        if status == "STATUS_WAIT_CODE":
            return "waiting", "⏳ No code yet — still waiting for the SMS to arrive."
        if status.startswith("STATUS_WAIT_RETRY"):
            return "waiting", "⏳ The first code was reported as wrong. Waiting for a retry code."
        if status == "STATUS_WAIT_RESEND":
            return "waiting", "⏳ Waiting for the SMS to be resent."
        if status == "STATUS_CANCEL":
            return "cancelled", "❌ This number was cancelled and can no longer receive codes."
        if status:
            return "other", f"📡 Status: {status}"

        # Nested shape with no status field: sms/call both null (or missing
        # code) means nothing has come through yet — not an error.
        if "sms" in data or "call" in data or "verificationType" in data:
            return "waiting", "⏳ No code yet — still waiting for the SMS to arrive."

    return _classify_plain_text(raw)


def _classify_plain_text(result: str):
    """Legacy colon-delimited plain-text format, e.g. 'STATUS_OK:1234'."""
    if result.startswith("STATUS_OK:"):
        return "code", extract_code(result.split(":", 1)[1])
    if result == "STATUS_WAIT_CODE":
        return "waiting", "⏳ No code yet — still waiting for the SMS to arrive."
    if result.startswith("STATUS_WAIT_RETRY"):
        return "waiting", "⏳ The first code was reported as wrong. Waiting for a retry code."
    if result == "STATUS_WAIT_RESEND":
        return "waiting", "⏳ Waiting for the SMS to be resent."
    if result == "STATUS_CANCEL":
        return "cancelled", "❌ This number was cancelled and can no longer receive codes."
    if result:
        return "other", f"📡 Status: {result}"
    return "empty", "ℹ️ No status information is currently available."


async def handle_status_result(send, context, result):
    """
    Called when the user taps CHECK STATUS. `send` is an async function
    taking (text, **kwargs) that delivers the reply. Requesting a
    replacement number is handled separately via the HELP button.
    """
    kind, text = classify_status(result)

    if kind == "code":
        await send(
            f"✅ Your code is: `{text}`",
            parse_mode="Markdown",
        )
        return

    if kind == "waiting":
        keyboard = [
            [InlineKeyboardButton("🟢 CHECK STATUS FOR CODE", callback_data="check_status")],
            [InlineKeyboardButton("🔴 HELP", callback_data="help")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="back_number")],
        ]
        await send(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if kind == "cancelled":
        await send(text)
        return

    await send(text)


async def check_status(message, context, edit=False):
    activation_id = context.user_data.get("activation_id")
    expiry_time = context.user_data.get("expiry_time")

    if not activation_id:
        await message.reply_text(
            "ℹ️ You don't currently have an active number."
        )
        return

    # Check our 20-minute period first
    if expiry_time and time.time() >= expiry_time:
        await message.reply_text(
            "⏰ Your 20-minute period has expired."
        )
        return

    await message.reply_text(
        "🔄 Checking activation status..."
    )

    try:
        response = requests.get(
            HEROSMS_URL,
            params={
                "action": "getStatusV2",
                "id": activation_id,
                "api_key": HEROSMS_API_KEY,
            },
            timeout=15
        )

        result = response.text.strip()

        async def send(text, **kwargs):
            if edit:
                try:
                    await message.edit_text(text, **kwargs)
                    return
                except Exception:
                    pass
            await message.reply_text(text, **kwargs)

        await handle_status_result(send, context, result)

    except Exception as e:
        await message.reply_text(
            f"❌ Status check error:\n{e}"
        )


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        countries_response = requests.get(
            HEROSMS_URL,
            params={
                "api_key": HEROSMS_API_KEY,
                "action": "getCountries"
            },
            timeout=15
        )

        services_response = requests.get(
            HEROSMS_URL,
            params={
                "api_key": HEROSMS_API_KEY,
                "action": "getServicesList"
            },
            timeout=15
        )

        await update.message.reply_text(
            "🌍 COUNTRIES:\n\n"
            + countries_response.text[:3500]
        )

        await update.message.reply_text(
            "📱 SERVICES:\n\n"
            + services_response.text[:3500]
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ Error:\n{e}"
        )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CallbackQueryHandler(button_click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone_number))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
