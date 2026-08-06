import re
import asyncio

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
from datetime import datetime

# Matches Kenyan mobile numbers like 0712345678 or +254712345678
PHONE_RE = re.compile(r"^(?:\+254|0)7\d{8}$")

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

# After this many consecutive checks with no code, offer a free replacement number.
MAX_NO_CODE_ATTEMPTS = 3

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Prefer their Telegram username; fall back to first name if they don't have one set.
    display_name = f"@{user.username}" if user.username else user.first_name

    keyboard = [
        [InlineKeyboardButton("📱 GET NUMBER", callback_data="get_number")],
        [InlineKeyboardButton("📋 MY NUMBER", callback_data="my_number")],
        [InlineKeyboardButton("🔄 CHECK STATUS", callback_data="check_status")],
        [InlineKeyboardButton("🆘 HELP", callback_data="help")],
    ]

    await update.message.reply_text(
        f"👋 Hi {display_name}!\n\n"
        f"Get a temporary number for just KES {PRICE_KES:.0f}.\n\n"
        "⏱️ You have 20 minutes from the time "
        "of purchase to use your number.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "get_number":
        context.user_data["awaiting_phone"] = True

        await query.message.reply_text(
            "📱 Please enter your M-Pesa phone number.\n\n"
            "Example:\n0712345678"
        )

    elif query.data == "my_number":
        await show_number(query.message, context)

    elif query.data == "check_status":
        await check_status(query.message, context)

    elif query.data == "help":
        await send_help(query.message, context)

    elif query.data == "request_replacement":
        await request_replacement_number(query.message, context)


async def send_help(message, context):
    await message.reply_text(
        "🆘 NEED HELP?\n\n"
        "If you paid but couldn't get a working code, message us directly "
        f"and we'll sort you out: {ADMIN_CONTACT}\n\n"
        "Please include your phone number and the number you were issued "
        "so we can look it up quickly."
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

                # Record the exact purchase time
                purchase_time = time.time()

                expiry_time = purchase_time + (
                    VALIDITY_MINUTES * 60
                )

                context.user_data["activation_id"] = activation_id
                context.user_data["number"] = number
                context.user_data["purchase_time"] = purchase_time
                context.user_data["expiry_time"] = expiry_time
                context.user_data["no_code_attempts"] = 0
                # Require a fresh payment before another *paid* number can be issued.
                context.user_data["paid"] = False

                # Stop any polling from a previous number before starting fresh.
                old_task = context.user_data.get("poll_task")
                if old_task and not old_task.done():
                    old_task.cancel()

                # Start automatically watching for the code — no need for the
                # user to press CHECK STATUS themselves.
                task = asyncio.create_task(
                    poll_for_code(
                        context.bot,
                        message.chat_id,
                        context,
                        activation_id,
                        expiry_time,
                    )
                )
                context.user_data["poll_task"] = task

                purchased = datetime.fromtimestamp(
                    purchase_time
                ).strftime("%H:%M:%S")

                expires = datetime.fromtimestamp(
                    expiry_time
                ).strftime("%H:%M:%S")

                keyboard = [
                    [
                        InlineKeyboardButton(
                            "🔄 CHECK STATUS",
                            callback_data="check_status"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📋 MY NUMBER",
                            callback_data="my_number"
                        )
                    ]
                ]

                await message.reply_text(
                    "✅ NUMBER PURCHASED\n\n"
                    f"📱 Number: {number}\n\n"
                    f"🕐 Purchased: {purchased}\n"
                    f"⏰ Expires: {expires}\n\n"
                    "⚠️ You have 20 minutes from the "
                    "time of purchase.\n"
                    f"Please use the number before {expires}.",
                    reply_markup=InlineKeyboardMarkup(keyboard)
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


async def request_replacement_number(message, context):
    """
    Called after too many consecutive no-code checks. The user already paid for
    the original number, so this issues a free replacement — on a different
    operator so it's not drawn from the same dead pool — and cancels the old
    activation so it can't still deliver a late code and cause confusion.
    """
    old_activation_id = context.user_data.get("activation_id")

    if old_activation_id:
        cancel_activation(old_activation_id)

    await message.reply_text(
        "🔄 Getting you a replacement number on a different network "
        "since the last one didn't deliver a code..."
    )

    await purchase_number(message, context, operator="telkom", require_payment=False)


async def show_number(message, context):
    number = context.user_data.get("number")
    purchase_time = context.user_data.get("purchase_time")
    expiry_time = context.user_data.get("expiry_time")

    if not number or not purchase_time or not expiry_time:
        await message.reply_text(
            "ℹ️ You don't currently have a number."
        )
        return

    now = time.time()
    remaining = expiry_time - now

    purchased = datetime.fromtimestamp(
        purchase_time
    ).strftime("%H:%M:%S")

    expires = datetime.fromtimestamp(
        expiry_time
    ).strftime("%H:%M:%S")

    if remaining <= 0:
        await message.reply_text(
            "⏰ YOUR NUMBER HAS EXPIRED\n\n"
            f"📱 Number: {number}\n"
            f"🕐 Purchased: {purchased}\n"
            f"⏰ Expired: {expires}"
        )
        return

    minutes = int(remaining // 60)
    seconds = int(remaining % 60)

    await message.reply_text(
        "📱 YOUR NUMBER\n\n"
        f"{number}\n\n"
        f"🕐 Purchased: {purchased}\n"
        f"⏰ Expires: {expires}\n"
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
    Turns a raw HeroSMS status string into (kind, text).
    kind is one of: "code", "waiting", "cancelled", "other", "empty".
    """
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
    Shared logic for both manual CHECK STATUS presses and the automatic
    background poller. `send` is an async function taking (text, **kwargs)
    that delivers the message however the caller needs to.
    Returns True if polling should stop (code received or cancelled).
    """
    kind, text = classify_status(result)

    if kind == "code":
        context.user_data["no_code_attempts"] = 0
        await send(
            "✅ CODE RECEIVED\n\n"
            f"🔑 Your code: `{text}`",
            parse_mode="Markdown",
        )
        return True

    if kind == "waiting":
        attempts = context.user_data.get("no_code_attempts", 0) + 1
        context.user_data["no_code_attempts"] = attempts

        offer_replacement = (
            attempts == MAX_NO_CODE_ATTEMPTS
            or (attempts > MAX_NO_CODE_ATTEMPTS and (attempts - MAX_NO_CODE_ATTEMPTS) % MAX_NO_CODE_ATTEMPTS == 0)
        )

        if offer_replacement:
            keyboard = [
                [InlineKeyboardButton("🔄 GET REPLACEMENT NUMBER", callback_data="request_replacement")],
                [InlineKeyboardButton("🆘 HELP", callback_data="help")],
            ]
            await send(
                f"{text}\n\n"
                f"Still nothing after {attempts} checks. You already paid, "
                "so you can grab a free replacement number on a different "
                "network, or reach out for help.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await send(text)
        return False

    if kind == "cancelled":
        await send(text)
        return True

    await send(text)
    return False


async def poll_for_code(bot, chat_id, context, activation_id, expiry_time):
    """
    Background task started right after a number is purchased. Checks HeroSMS
    periodically and automatically pushes the code to the user the moment it
    arrives, so nobody has to press CHECK STATUS themselves.
    """
    interval = 7  # seconds between checks

    async def send(text, **kwargs):
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)

    while True:
        await asyncio.sleep(interval)

        # A replacement number (or a fresh purchase) has taken over — stop.
        if context.user_data.get("activation_id") != activation_id:
            return

        if time.time() >= expiry_time:
            await send("⏰ Your 20-minute window expired before a code arrived.")
            return

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
        except Exception:
            # Transient network hiccup — just try again next loop.
            continue

        should_stop = await handle_status_result(send, context, result)
        if should_stop:
            return


async def check_status(message, context):
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
