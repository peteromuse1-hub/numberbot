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


def format_eat_time(timestamp: float) -> str:
    """Format an EAT timestamp in a user-friendly 12-hour AM/PM format."""
    return datetime.fromtimestamp(timestamp, EAT).strftime("%I:%M:%S %p").lstrip("0")


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟣 GET NUMBER", callback_data="get_number")],
        [
            InlineKeyboardButton("🔵 MY NUMBER", callback_data="my_number"),
            InlineKeyboardButton("🟢 CHECK STATUS", callback_data="check_status"),
        ],
        [InlineKeyboardButton("🔴 HELP", callback_data="help")],
    ])


async def edit_ui_message(context, text, reply_markup=None, parse_mode=None):
    """Edit the bot's current UI bubble instead of creating another one."""
    chat_id = context.user_data.get("ui_chat_id")
    message_id = context.user_data.get("ui_message_id")

    if chat_id and message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return True
        except Exception:
            pass

    return False


def remember_ui_message(message, context):
    context.user_data["ui_chat_id"] = message.chat_id
    context.user_data["ui_message_id"] = message.message_id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    display_name = f"@{user.username}" if user.username else user.first_name

    sent = await update.message.reply_text(
        f"👋 Hi {display_name}!\n\n"
        f"Get a temporary number for just KES {PRICE_KES:.0f}.\n\n"
        "⏱️ You have 20 minutes from the time of purchase to use your number.",
        reply_markup=main_menu_keyboard(),
    )
    remember_ui_message(sent, context)


async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_ui_message(query.message, context)

    if query.data == "get_number":
        context.user_data["awaiting_phone"] = True
        await query.edit_message_text(
            "📱 *GET NUMBER*\n\n"
            "Please enter your M-Pesa phone number.\n\n"
            "Example:\n`0712345678`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_main")]
            ]),
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
        await query.edit_message_text(
            f"👋 Hi {query.from_user.first_name}!\n\n"
            f"Get a temporary number for just KES {PRICE_KES:.0f}.\n\n"
            "⏱️ You have 20 minutes from the time of purchase to use your number.",
            reply_markup=main_menu_keyboard(),
        )

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
        await message.reply_text(text, **kwargs)

    activation_id = context.user_data.get("activation_id")
    purchase_time = context.user_data.get("purchase_time")
    replacement_count = context.user_data.get("replacement_count", 0)

    lines = ["🆘 *NEED HELP?*", ""]
    keyboard = []

    if activation_id and purchase_time:
        elapsed = time.time() - purchase_time

        if replacement_count >= MAX_REPLACEMENTS:
            lines.append("You've already used your one free replacement number for this purchase.")
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
                "cancels your current number and issues a new one on a different network."
            )
            keyboard.append([
                InlineKeyboardButton("🟠 GET REPLACEMENT NUMBER", callback_data="request_replacement")
            ])
    else:
        lines.append(f"Need a hand? Message us directly: {ADMIN_CONTACT}")

    lines.append("")
    lines.append(
        "Please include your phone number and the number you were issued "
        "so we can look it up quickly."
    )
    keyboard.append([
        InlineKeyboardButton(
            "⬅️ BACK",
            callback_data="back_number" if activation_id else "back_main"
        )
    ])

    await send(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the M-Pesa phone number after GET NUMBER was pressed."""
    if not context.user_data.get("awaiting_phone"):
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

    # Same bot bubble: phone entered -> payment prompt -> waiting.
    await edit_ui_message(
        context,
        f"💳 *PAYMENT REQUEST*\n\n"
        f"Sending an M-Pesa prompt of KES {PRICE_KES:.0f} to `{phone}`.\n\n"
        "Please approve the prompt on your phone within 60 seconds.\n\n"
        "⏳ Waiting for payment...",
        parse_mode="Markdown",
    )

    reference, error = initiate_mpesa_charge(phone, PRICE_KES)

    if error:
        await edit_ui_message(
            context,
            f"❌ *COULD NOT START PAYMENT*\n\n{error}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟣 TRY AGAIN", callback_data="get_number")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_main")],
            ]),
            parse_mode="Markdown",
        )
        return

    context.user_data["payment_reference"] = reference

    for _ in range(PAYMENT_POLL_MAX_ATTEMPTS):
        await asyncio.sleep(PAYMENT_POLL_INTERVAL_SECONDS)
        status = verify_paystack_charge(reference)

        if status == "success":
            context.user_data["paid"] = True

            # Same bubble: payment received -> checking for a number.
            await edit_ui_message(
                context,
                "✅ *PAYMENT RECEIVED*\n\n⏳ Checking for a number...",
                parse_mode="Markdown",
            )

            await get_number(update.message, context)
            return

        if status == "failed":
            await edit_ui_message(
                context,
                "❌ *PAYMENT FAILED*\n\n"
                "The payment was failed or cancelled. No number was issued.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🟣 TRY AGAIN", callback_data="get_number")],
                    [InlineKeyboardButton("⬅️ BACK", callback_data="back_main")],
                ]),
                parse_mode="Markdown",
            )
            return

    await edit_ui_message(
        context,
        "⌛ *PAYMENT CONFIRMATION TIMED OUT*\n\n"
        "If you approved the prompt, check again shortly. "
        "Otherwise tap TRY AGAIN.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 CHECK STATUS", callback_data="check_status")],
            [InlineKeyboardButton("🟣 TRY AGAIN", callback_data="get_number")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="back_main")],
        ]),
        parse_mode="Markdown",
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
    Requests a number from HeroSMS.
    Progress/results stay in the same Telegram UI bubble.
    """
    if require_payment and not context.user_data.get("paid"):
        if not await edit_ui_message(
            context,
            "❌ Payment not confirmed for this number. Please try again.",
            reply_markup=main_menu_keyboard(),
        ):
            await message.reply_text(
                "❌ Payment not confirmed for this number. Please try again."
            )
        return

    # This is deliberately edited in place.
    await edit_ui_message(
        context,
        "⏳ *Checking for a number...*",
        parse_mode="Markdown",
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
        response = requests.get(HEROSMS_URL, params=params, timeout=15)
        result = response.text.strip()

        if result.startswith("ACCESS_NUMBER:"):
            parts = result.split(":")

            if len(parts) >= 3:
                activation_id = parts[1]
                number = parts[2]

                context.user_data["activation_id"] = activation_id
                context.user_data["number"] = number
                context.user_data["paid"] = False

                if require_payment:
                    context.user_data["replacement_count"] = 0

                # 20-minute clock starts when the number is actually issued.
                purchase_time = time.time()
                expiry_time = purchase_time + (VALIDITY_MINUTES * 60)
                context.user_data["purchase_time"] = purchase_time
                context.user_data["expiry_time"] = expiry_time

                purchased_eat = format_eat_time(purchase_time)
                expires_eat = format_eat_time(expiry_time)

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🟢 CHECK STATUS FOR CODE",
                        callback_data="check_status"
                    )],
                    [
                        InlineKeyboardButton("🔵 MY NUMBER", callback_data="my_number"),
                        InlineKeyboardButton("🔴 HELP", callback_data="help"),
                    ],
                ])

                final_text = (
                    "✅ *NUMBER PURCHASED*\n\n"
                    f"📱 Number: `{number}`\n\n"
                    "⚠️ You have 20 minutes from now to use this number.\n\n"
                    f"🕐 Purchased: {purchased_eat} EAT\n"
                    f"⏰ Expires: {expires_eat} EAT\n\n"
                    "👉 *Tap CHECK STATUS to see if your code has arrived.* "
                    "The code isn't sent automatically — you'll need to check "
                    "yourself. If nothing shows up after 3.5 minutes, tap "
                    "🔴 HELP for a free replacement number."
                )

                if not await edit_ui_message(
                    context,
                    final_text,
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                ):
                    await message.reply_text(
                        final_text,
                        reply_markup=keyboard,
                        parse_mode="Markdown",
                    )
                return

            await edit_ui_message(
                context,
                "❌ The number service returned an unexpected response.",
                reply_markup=main_menu_keyboard(),
            )
            return

        if result == "NO_NUMBERS":
            await edit_ui_message(
                context,
                "❌ *NO NUMBERS AVAILABLE*\n\nPlease try again later.",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
            return

        await edit_ui_message(
            context,
            f"❌ *NUMBER REQUEST FAILED*\n\n{result}",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )

    except Exception as e:
        await edit_ui_message(
            context,
            f"❌ *CONNECTION ERROR*\n\n{e}",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
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
        await message.reply_text(text, **kwargs)

    activation_id = context.user_data.get("activation_id")
    purchase_time = context.user_data.get("purchase_time")
    replacement_count = context.user_data.get("replacement_count", 0)

    if not activation_id or not purchase_time:
        await send(
            "ℹ️ You don't currently have an active number to replace.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if replacement_count >= MAX_REPLACEMENTS:
        await send(
            "❌ You've already used your one free replacement for this purchase.\n\n"
            f"Please message {ADMIN_CONTACT} for further help.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_number")]
            ]),
        )
        return

    elapsed = time.time() - purchase_time
    if elapsed < REPLACEMENT_WAIT_SECONDS:
        remaining = int(REPLACEMENT_WAIT_SECONDS - elapsed)
        mins, secs = divmod(remaining, 60)
        await send(
            f"⏳ Replacements open up {mins}m {secs}s from now — "
            "please wait a little longer or use CHECK STATUS.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟢 CHECK STATUS", callback_data="check_status")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_number")],
            ]),
        )
        return

    await send("🔍 Double-checking your current number for a code first...")

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
        kind, status_text = classify_status(response.text.strip())
    except Exception as e:
        await send(
            f"❌ Couldn't verify your number's status:\n{e}\n\nPlease try again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_number")]
            ]),
        )
        return

    if kind == "code":
        await send(
            f"✅ A code already arrived: `{status_text}`\n\n"
            "No replacement needed — check MY NUMBER.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔵 MY NUMBER", callback_data="my_number")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_number")],
            ]),
            parse_mode="Markdown",
        )
        return

    if kind == "cancelled":
        await send(
            "ℹ️ This number was already cancelled. "
            f"Please message {ADMIN_CONTACT} for help.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_number")]
            ]),
        )
        return

    cancel_activation(activation_id)
    context.user_data["replacement_count"] = replacement_count + 1

    await send(
        "🔄 Confirmed no code yet — getting you a replacement number on "
        "the Telkom network..."
    )

    # purchase_number edits the same UI bubble.
    await purchase_number(message, context, operator="telkom", require_payment=False)


async def show_number(message, context, edit=False):
    number = context.user_data.get("number")
    purchase_time = context.user_data.get("purchase_time")
    expiry_time = context.user_data.get("expiry_time")

    async def send(text, **kwargs):
        if edit:
            try:
                await message.edit_text(text, **kwargs)
                return
            except Exception:
                pass
        await message.reply_text(text, **kwargs)

    if not number or not purchase_time or not expiry_time:
        await send(
            "ℹ️ You don't currently have a number.",
            reply_markup=main_menu_keyboard(),
        )
        return

    remaining = expiry_time - time.time()
    purchased = format_eat_time(purchase_time)
    expires = format_eat_time(expiry_time)

    if remaining <= 0:
        await send(
            "⏰ *YOUR NUMBER HAS EXPIRED*\n\n"
            f"📱 Number: `{number}`\n"
            f"🕐 Purchased: {purchased} EAT\n"
            f"⏰ Expired: {expires} EAT",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟣 GET NEW NUMBER", callback_data="get_number")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_main")],
            ]),
            parse_mode="Markdown",
        )
        return

    minutes = int(remaining // 60)
    seconds = int(remaining % 60)

    await send(
        "📱 *YOUR NUMBER*\n\n"
        f"`{number}`\n\n"
        f"🕐 Purchased: {purchased} EAT\n"
        f"⏰ Expires: {expires} EAT\n"
        f"⌛ Time remaining: {minutes}m {seconds}s",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 CHECK STATUS", callback_data="check_status")],
            [InlineKeyboardButton("🔴 HELP", callback_data="help")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="back_main")],
        ]),
        parse_mode="Markdown",
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
    kind, text = classify_status(result)

    if kind == "code":
        await send(
            f"✅ *YOUR CODE IS:* `{text}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 CHECK AGAIN", callback_data="check_status")],
                [InlineKeyboardButton("🔵 MY NUMBER", callback_data="my_number")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_number")],
            ]),
            parse_mode="Markdown",
        )
        return

    if kind == "waiting":
        await send(
            "⏳ No code yet — still waiting for the SMS to arrive.\n\n"
            "No code yet? Tap 🆘 HELP to request a free replacement number "
            "once 3.5 minutes have passed since purchase.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🟢 CHECK STATUS FOR CODE",
                    callback_data="check_status"
                )],
                [InlineKeyboardButton("🔴 HELP", callback_data="help")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_number")],
            ]),
        )
        return

    await send(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 CHECK AGAIN", callback_data="check_status")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="back_number")],
        ]),
    )


async def check_status(message, context, edit=False):
    activation_id = context.user_data.get("activation_id")
    expiry_time = context.user_data.get("expiry_time")

    async def send(text, **kwargs):
        if edit:
            try:
                await message.edit_text(text, **kwargs)
                return
            except Exception:
                pass
        await message.reply_text(text, **kwargs)

    if not activation_id:
        await send(
            "ℹ️ You don't currently have an active number.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if expiry_time and time.time() >= expiry_time:
        await send(
            "⏰ *Your 20-minute period has expired.*",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟣 GET NEW NUMBER", callback_data="get_number")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_main")],
            ]),
            parse_mode="Markdown",
        )
        return

    # First edit: "Checking..."
    # Second edit: "No code yet..." or the actual code.
    await send(
        "🔄 *Checking activation status...*",
        parse_mode="Markdown",
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

        await handle_status_result(send, context, response.text.strip())

    except Exception as e:
        await send(
            f"❌ Status check error:\n{e}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 CHECK AGAIN", callback_data="check_status")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_number")],
            ]),
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
