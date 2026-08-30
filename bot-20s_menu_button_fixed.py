import re
import asyncio
import json

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
)
from telegram.error import BadRequest
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
MAX_PRICE = 0.025  # Small safety margin above the 0.024 price currently being returned

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

# If no code has arrived by this long after a number is issued, the bot
# proactively nudges the user (unlike REPLACEMENT_WAIT_SECONDS above, which
# only unlocks a button — this one actually sends a message on its own).
REMINDER_SECONDS = 600  # 10 minutes

# The operator cascade for issuing numbers: index 0 is the original paid
# purchase, indices 1 and 2 are the free replacements. If a provider has no
# code confirmed (after the wait) or has no numbers available at all, the
# bot moves to the next one automatically. Customer-facing text never names
# the specific provider — it's purely an internal ordering.
OPERATOR_SEQUENCE = ["airtel", "safaricom", "telkom"]

# Only two free replacements allowed per original paid purchase — one
# original number plus two replacements, matching len(OPERATOR_SEQUENCE) - 1.
MAX_REPLACEMENTS = len(OPERATOR_SEQUENCE) - 1

PAYSTACK_BASE_URL = "https://api.paystack.co"

# How long to wait for the user to approve the M-Pesa STK push prompt.
PAYMENT_POLL_INTERVAL_SECONDS = 5
PAYMENT_POLL_MAX_ATTEMPTS = 12  # 12 * 5s = 60s total


# ─────────────────────────────────────────────────────────────────────────
# Single-screen navigation
#
# The bot maintains one "screen" message per user and edits it in place as
# they navigate, instead of every step spawning a new bubble. This works
# uniformly whether the update came from a button press (query is set, so
# we edit directly via query.edit_message_text — the fast path) or from a
# typed message like the phone number or the payment-polling loop (query is
# None, so we edit via the tracked message id instead). Falls back to
# sending a fresh message only when there's nothing to edit yet, or the
# edit fails (message too old/deleted, or past Telegram's 48-hour window).
# ─────────────────────────────────────────────────────────────────────────

# Persistent reply keyboard docked below the text-input box. Unlike the
# inline buttons attached to individual messages, this stays visible no
# matter how far the user scrolls or how many screens get edited in place —
# it's the "always one tap away" way to restart without typing /start.
PERSISTENT_KEYBOARD = ReplyKeyboardMarkup(
    [["🏠 START"]],
    resize_keyboard=True,
    is_persistent=True,
)

# Small inline button offered inline on screens too, so a restart is never
# more than one tap away even before the persistent keyboard below has been
# set up (e.g. very first message from a brand new user).
START_BUTTON_ROW = [InlineKeyboardButton("🏠 START", callback_data="back_main")]

DEAD_END_REMINDER = "\n\n👉 Press 🏠 START below to begin again."


def _with_start_button(keyboard):
    """Appends the inline START row to an existing keyboard, or creates one."""
    return (keyboard + [START_BUTTON_ROW]) if keyboard else [START_BUTTON_ROW]


def _reminder_job_name(chat_id):
    return f"reminder10_{chat_id}"


def _cancel_reminder_job(context, chat_id):
    """Removes any pending 10-minute reminder for this chat — called before
    scheduling a new one, so replacing a number never leaves two reminders
    ticking at once."""
    for job in context.application.job_queue.get_jobs_by_name(_reminder_job_name(chat_id)):
        job.schedule_removal()


def _schedule_reminder_job(context, chat_id, activation_id):
    """
    Schedules the 10-minute no-code nudge for a freshly issued number.
    Assumes a private 1:1 chat, where chat_id doubles as user_id — needed so
    PTB scopes context.user_data correctly inside the job callback below.
    """
    _cancel_reminder_job(context, chat_id)
    context.application.job_queue.run_once(
        remind_if_no_code,
        when=REMINDER_SECONDS,
        chat_id=chat_id,
        user_id=chat_id,
        name=_reminder_job_name(chat_id),
        data={"activation_id": activation_id},
    )


async def remind_if_no_code(context: ContextTypes.DEFAULT_TYPE):
    """
    Fires 10 minutes after a number is issued. Stays silent if the user
    already has their code, or if this number has since been replaced
    (activation_id no longer matches — a fresh reminder was already
    scheduled for the new one).
    """
    chat_id = context.job.chat_id
    activation_id = context.job.data["activation_id"]

    if context.user_data.get("activation_id") != activation_id:
        return
    if context.user_data.get("code_received"):
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text="⏰ 10 minutes have passed since your purchase and no code has "
             "arrived yet.\n\nTap 🟢 CHECK STATUS below to check, or 🔴 HELP "
             "for a free replacement number.",
        reply_markup=InlineKeyboardMarkup(number_nav_keyboard(context)),
    )


async def render(context, chat_id, text, keyboard=None, parse_mode=None, query=None, force_new=False, dead_end=False):
    """
    Returns the Message object that ended up holding this content (or None),
    so callers that need a reliable timestamp — e.g. anchoring the 20-minute
    expiry window to Telegram's own server clock rather than local time —
    can use it.

    force_new=True skips both edit paths and always sends a fresh message —
    used for moments that should visibly appear as a new bubble below
    whatever the user just did (e.g. /start, or the final purchase
    confirmation appearing below the phone number they typed), rather than
    silently editing an older message elsewhere in the chat.

    dead_end=True marks a screen that otherwise offers the user no further
    action (an error, an expiry notice, an exhausted-replacements message,
    etc.) — it appends a short reminder and an inline START button so the
    user is never left staring at a screen with nowhere to go.
    """
    if dead_end:
        text = text + DEAD_END_REMINDER
        keyboard = _with_start_button(keyboard)

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    # Telegram does not provide a configurable animation for message edits.
    # Serialize in-place edits and give the client a very short settling period
    # so rapid screen changes feel smoother instead of flickering.
    render_locks = context.application.bot_data.setdefault("_render_locks", {})
    lock = render_locks.setdefault(chat_id, asyncio.Lock())

    if not force_new:
        async with lock:
            await asyncio.sleep(0.12)

    if not force_new and query is not None:
        try:
            result = await query.edit_message_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode
            )
            context.user_data["screen_message_id"] = query.message.message_id
            return result if hasattr(result, "message_id") else query.message
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                context.user_data["screen_message_id"] = query.message.message_id
                return query.message
            # Any other edit failure — fall through and try the stored-id
            # path, then finally send fresh.
        except Exception:
            pass

    if not force_new:
        screen_id = context.user_data.get("screen_message_id")
        if screen_id:
            try:
                result = await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=screen_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                return result if hasattr(result, "message_id") else None
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    return None
                # Fall through to send a new message.
            except Exception:
                pass

    sent = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode
    )
    context.user_data["screen_message_id"] = sent.message_id
    return sent


def _server_timestamp(message):
    """
    Pulls a server-side timestamp off a Message object — edit_date if it was
    just edited, otherwise date — falling back to the local clock if neither
    is available. Used to anchor the 20-minute expiry window to Telegram's
    own clock instead of the host machine's, which can drift.
    """
    if message is not None:
        ts = getattr(message, "edit_date", None) or getattr(message, "date", None)
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.timestamp()
    return time.time()


def number_nav_keyboard(context, extra_rows=None):
    """
    The standard 3-button navigation set shown after every number-related
    screen (purchase confirmation, status checks, MY NUMBER, HELP) so the
    user can always keep navigating without hunting for an old button.
    The CHECK STATUS label switches to "CHECK FOR NEW CODE" once a code has
    already been delivered for the current number, since at that point
    they're checking for an additional code rather than the first one.
    """
    check_label = (
        "🟢 CHECK FOR NEW CODE" if context.user_data.get("code_received")
        else "🟢 CHECK STATUS FOR CODE"
    )
    keyboard = [
        [InlineKeyboardButton(check_label, callback_data="check_status")],
        [
            InlineKeyboardButton("🔵 MY NUMBER", callback_data="my_number"),
            InlineKeyboardButton("🔴 HELP", callback_data="help"),
        ],
    ]
    if extra_rows:
        keyboard.extend(extra_rows)
    keyboard.append(START_BUTTON_ROW)
    return keyboard


def main_menu_content(user):
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

    return text, keyboard


# ─────────────────────────────────────────────────────────────────────────
# Paystack (M-Pesa payment collection)
# ─────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = main_menu_content(update.effective_user)
    # /start is a fresh command — always appear as a new bubble right below
    # it, never silently edit some older screen elsewhere in the chat.
    chat_id = update.effective_chat.id
    await render(context, chat_id, text, keyboard=keyboard, force_new=True)

    # Dock the persistent 🏠 START button below the text box the very first
    # time this user starts, so every start after this one is one tap away
    # with no typing required. Only sent once per user — Telegram keeps a
    # reply keyboard visible indefinitely once set, so re-sending it on
    # every /start would just add noise without changing anything.
    if not context.user_data.get("persistent_keyboard_shown"):
        context.user_data["persistent_keyboard_shown"] = True
        await context.bot.send_message(
            chat_id=chat_id,
            text="Tip: the 🏠 START button below is always there — tap it "
                 "anytime instead of typing /start.",
            reply_markup=PERSISTENT_KEYBOARD,
        )


async def handle_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches taps on the persistent 🏠 START reply-keyboard button."""
    await start(update, context)


async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "get_number":
        context.user_data["awaiting_phone"] = True
        keyboard = [[InlineKeyboardButton("⬅️ BACK", callback_data="back_main")]]
        await render(
            context, chat_id,
            "📱 *GET NUMBER*\n\n"
            "Please enter your M-Pesa phone number.\n\n"
            "Example:\n`0712345678`",
            keyboard=keyboard,
            parse_mode="Markdown",
            query=query,
        )

    elif query.data == "my_number":
        await show_number(context, chat_id, query=query)

    elif query.data == "check_status":
        await check_status(context, chat_id, query=query)

    elif query.data == "help":
        await send_help(context, chat_id, query=query)

    elif query.data == "request_replacement":
        await request_replacement_number(context, chat_id, query=query)

    elif query.data == "back_main":
        context.user_data["awaiting_phone"] = False
        text, keyboard = main_menu_content(query.from_user)
        await render(context, chat_id, text, keyboard=keyboard, query=query)


async def send_help(context, chat_id, query=None):
    activation_id = context.user_data.get("activation_id")
    purchase_time = context.user_data.get("purchase_time")
    operator_index = context.user_data.get("operator_index", 0)

    lines = ["🆘 *NEED HELP?*", ""]
    extra_rows = []

    if activation_id and purchase_time:
        elapsed = time.time() - purchase_time

        if operator_index >= len(OPERATOR_SEQUENCE) - 1:
            lines.append(
                "You've already used your free replacement numbers for this purchase."
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
                "cancels your current number and issues a new number."
            )
            lines.append(f"Having problems? DM {ADMIN_CONTACT} with your number for assistance.")
            extra_rows.append(
                [InlineKeyboardButton("🟠 GET REPLACEMENT NUMBER", callback_data="request_replacement")]
            )
    else:
        lines.append(f"Need a hand? Message us directly: {ADMIN_CONTACT}")

    lines.append("")
    lines.append(
        "Please include your phone number and the number you were issued "
        "so we can look it up quickly."
    )

    keyboard = number_nav_keyboard(context, extra_rows=extra_rows) if activation_id else None

    await render(
        context, chat_id,
        "\n".join(lines),
        keyboard=keyboard,
        parse_mode="Markdown",
        query=query,
        dead_end=not activation_id,
    )


async def handle_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the M-Pesa phone number the user types after pressing GET NUMBER."""
    if not context.user_data.get("awaiting_phone"):
        # Not expecting text input right now; ignore.
        return

    chat_id = update.effective_chat.id
    phone = update.message.text.strip()

    if not PHONE_RE.match(phone):
        await render(
            context, chat_id,
            "❌ That doesn't look like a valid M-Pesa number.\n\n"
            "Please enter it like this:\n0712345678",
            dead_end=True,
        )
        return

    context.user_data["awaiting_phone"] = False
    context.user_data["phone_number"] = phone

    await render(
        context, chat_id,
        f"💳 Sending a payment prompt of KES {PRICE_KES:.0f} to {phone}.\n\n"
        "Please approve the M-Pesa prompt on your phone within 60 seconds."
    )

    reference, error = initiate_mpesa_charge(phone, PRICE_KES)

    if error:
        await render(context, chat_id, f"❌ Could not start payment:\n{error}", dead_end=True)
        return

    context.user_data["payment_reference"] = reference

    for _ in range(PAYMENT_POLL_MAX_ATTEMPTS):
        await asyncio.sleep(PAYMENT_POLL_INTERVAL_SECONDS)
        status = verify_paystack_charge(reference)

        if status == "success":
            context.user_data["paid"] = True
            await render(context, chat_id, "✅ Payment received!")
            await get_number(context, chat_id)
            return

        if status == "failed":
            await render(
                context, chat_id,
                "❌ Payment failed or was cancelled. No number was issued.",
                dead_end=True,
            )
            return

        # status == "pending" or "error": keep polling until attempts run out

    await render(
        context, chat_id,
        "⌛ We didn't receive payment confirmation in time.\n\n"
        "If you approved the prompt, it may still be processing — "
        "otherwise tap START below and try GET NUMBER again.",
        dead_end=True,
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


async def issue_next_operator(context, chat_id, query=None):
    """
    Cancels the current activation (if any) and requests the next operator
    in the cascade — airtel -> safaricom -> telkom. Used both when a
    confirmed no-code replacement is requested via HELP, and automatically
    when HeroSMS reports no numbers available for the current operator.

    Returns True if a replacement was issued, False if the cascade is
    exhausted (all operators already tried) — caller should direct the user
    to admin contact in that case.
    """
    operator_index = context.user_data.get("operator_index", 0)
    if operator_index >= len(OPERATOR_SEQUENCE) - 1:
        return False

    old_activation_id = context.user_data.get("activation_id")
    if old_activation_id:
        cancel_activation(old_activation_id)

    operator_index += 1
    context.user_data["operator_index"] = operator_index
    next_operator = OPERATOR_SEQUENCE[operator_index]

    await render(context, chat_id, "🔄 Getting you a free replacement number...", query=query)
    await purchase_number(context, chat_id, operator=next_operator, require_payment=False, query=query)
    return True


async def purchase_number(context, chat_id, operator=None, require_payment=True, query=None, force_new_on_success=False):
    """
    Requests a number from HeroSMS and reports the result to the user.
    Set require_payment=False for a free replacement (already paid for the original).
    force_new_on_success=True makes the final confirmation a brand-new bubble
    rather than an edit — used only for the original purchase, since it
    should appear below the phone number the user just typed rather than
    silently editing the older "enter phone number" screen above it.
    """
    if require_payment and not context.user_data.get("paid"):
        await render(
            context, chat_id,
            "❌ Payment not confirmed for this number. Please try again.",
            query=query,
            dead_end=True,
        )
        return

    await render(context, chat_id, "⏳ Checking for a number...", query=query)

    params = {
        "api_key": HEROSMS_API_KEY,
        "action": "getNumber",
        "service": SERVICE,
        "country": COUNTRY,
        "maxPrice": MAX_PRICE,
        # Use maxPrice as a ceiling rather than demanding an exact price.
        # fixedPrice=true can reject an otherwise available number when
        # HeroSMS pricing changes by a tiny amount.
        "fixedPrice": "false",
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
                # Fresh number — no code received for it yet, so the CHECK
                # STATUS button should read "FOR CODE" not "FOR NEW CODE".
                context.user_data["code_received"] = False
                if require_payment:
                    # This is the original paid purchase — reset the operator
                    # cascade back to the start (Airtel). A replacement
                    # purchase itself must NOT reset this, otherwise the
                    # 2-replacement cap could never actually bind.
                    context.user_data["operator_index"] = 0

                keyboard = number_nav_keyboard(context)

                # Edit the screen in place (or send fresh, for the original
                # purchase — see force_new_on_success), then anchor the
                # 20-minute window to the timestamp Telegram's own servers
                # stamped on this message — not the local system clock,
                # which can drift.
                sent = await render(
                    context, chat_id,
                    "✅ *NUMBER PURCHASED*\n\n"
                    f"📱 Number: `{number}`\n\n"
                    "⚠️ You have 20 minutes from now to use this number.\n\n"
                    "👉 *Tap CHECK STATUS to see if your code has arrived.* "
                    "The code isn't sent automatically — you'll need to check "
                    "yourself. If nothing shows up after 3.5 minutes, tap "
                    "🔴 HELP for a free replacement number.",
                    keyboard=keyboard,
                    parse_mode="Markdown",
                    query=query,
                    force_new=force_new_on_success,
                )

                purchase_time = _server_timestamp(sent)
                expiry_time = purchase_time + (VALIDITY_MINUTES * 60)

                context.user_data["purchase_time"] = purchase_time
                context.user_data["expiry_time"] = expiry_time

                _schedule_reminder_job(context, chat_id, activation_id)

                purchased_eat = datetime.fromtimestamp(purchase_time, EAT).strftime("%I:%M %p")
                expires_eat = datetime.fromtimestamp(expiry_time, EAT).strftime("%I:%M %p")

                # This clock-times note is a permanent little receipt, kept
                # as its own separate bubble rather than folded into the
                # evolving screen above, so it stays visible even as the
                # screen continues to update through CHECK STATUS / HELP.
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🕐 Purchased: {purchased_eat}\n⏰ Expires: {expires_eat}",
                )

            else:
                await render(
                    context, chat_id,
                    "❌ The number service returned an unexpected response.",
                    query=query,
                    dead_end=True,
                )

        elif result == "NO_NUMBERS":
            cascaded = await issue_next_operator(context, chat_id, query=query)
            if not cascaded:
                await render(
                    context, chat_id,
                    "❌ No numbers are currently available on any network right now.\n\n"
                    f"Please message {ADMIN_CONTACT} for help.",
                    query=query,
                    dead_end=True,
                )

        else:
            await render(context, chat_id, f"❌ Number request failed:\n{result}", query=query, dead_end=True)

    except Exception as e:
        await render(context, chat_id, f"❌ Connection error:\n{e}", query=query, dead_end=True)


async def get_number(context, chat_id, query=None):
    # Original purchase always starts the cascade at the first operator
    # (Airtel). The confirmation should land as a fresh bubble below the
    # phone number the user just typed, not edit the older screen above it.
    await purchase_number(
        context, chat_id, operator=OPERATOR_SEQUENCE[0], require_payment=True,
        query=query, force_new_on_success=True,
    )


async def request_replacement_number(context, chat_id, query=None):
    """
    Reached only via the HELP button, and only once the eligibility checks
    in send_help have already passed — but we re-check here too, since this
    can be triggered by a stale button tap. The user already paid for the
    original number, so this issues the next free replacement in the
    airtel -> safaricom -> telkom cascade, and cancels the old activation
    immediately so it can't still deliver a late code and cause "did I buy
    two numbers?" confusion. Up to two replacements are allowed per
    original purchase.
    """
    activation_id = context.user_data.get("activation_id")
    purchase_time = context.user_data.get("purchase_time")
    operator_index = context.user_data.get("operator_index", 0)

    if not activation_id or not purchase_time:
        await render(
            context, chat_id,
            "ℹ️ You don't currently have an active number to replace.",
            query=query,
            dead_end=True,
        )
        return

    if operator_index >= len(OPERATOR_SEQUENCE) - 1:
        await render(
            context, chat_id,
            "❌ You've already used your free replacement numbers for this purchase.\n\n"
            f"Please message {ADMIN_CONTACT} for further help.",
            query=query,
            dead_end=True,
        )
        return

    elapsed = time.time() - purchase_time
    if elapsed < REPLACEMENT_WAIT_SECONDS:
        remaining = int(REPLACEMENT_WAIT_SECONDS - elapsed)
        mins, secs = divmod(remaining, 60)
        await render(
            context, chat_id,
            f"⏳ Replacements open up {mins}m {secs}s from now — "
            "please wait a little longer or use CHECK STATUS.",
            keyboard=number_nav_keyboard(context),
            query=query,
        )
        return

    # Confirm with HeroSMS that a code genuinely hasn't arrived before
    # handing out a free replacement — don't just trust the clock.
    await render(context, chat_id, "🔍 Double-checking your current number for a code first...", query=query)

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
        await render(
            context, chat_id,
            f"❌ Couldn't verify your number's status:\n{e}\n\nPlease try again.",
            query=query,
            dead_end=True,
        )
        return

    if kind == "code":
        await render(
            context, chat_id,
            f"✅ A code already arrived: `{text}`\n\n"
            "No replacement needed — check MY NUMBER.",
            keyboard=number_nav_keyboard(context),
            parse_mode="Markdown",
            query=query,
        )
        return

    if kind == "cancelled":
        await render(
            context, chat_id,
            f"ℹ️ This number was already cancelled. Please message {ADMIN_CONTACT} for help.",
            query=query,
            dead_end=True,
        )
        return

    # Genuinely no code yet — cascade to the next operator. This also
    # covers the "current provider has no numbers" case automatically,
    # since purchase_number's own NO_NUMBERS handling calls this same
    # cascade helper if the next operator also comes up empty.
    cascaded = await issue_next_operator(context, chat_id, query=query)
    if not cascaded:
        await render(
            context, chat_id,
            "❌ No more replacement numbers available.\n\n"
            f"Please message {ADMIN_CONTACT} for help.",
            query=query,
            dead_end=True,
        )


async def show_number(context, chat_id, query=None):
    number = context.user_data.get("number")
    purchase_time = context.user_data.get("purchase_time")
    expiry_time = context.user_data.get("expiry_time")

    if not number or not purchase_time or not expiry_time:
        await render(context, chat_id, "ℹ️ You don't currently have a number.", query=query, dead_end=True)
        return

    now = time.time()
    remaining = expiry_time - now

    purchased = datetime.fromtimestamp(purchase_time, EAT).strftime("%I:%M %p")
    expires = datetime.fromtimestamp(expiry_time, EAT).strftime("%I:%M %p")

    if remaining <= 0:
        await render(
            context, chat_id,
            "⏰ YOUR NUMBER HAS EXPIRED\n\n"
            f"📱 Number: {number}\n"
            f"🕐 Purchased: {purchased}\n"
            f"⏰ Expired: {expires}",
            query=query,
            dead_end=True,
        )
        return

    minutes = int(remaining // 60)
    seconds = int(remaining % 60)

    await render(
        context, chat_id,
        "📱 YOUR NUMBER\n\n"
        f"{number}\n\n"
        f"🕐 Purchased: {purchased}\n"
        f"⏰ Expires: {expires}\n"
        f"⌛ Time remaining: {minutes}m {seconds}s",
        keyboard=number_nav_keyboard(context),
        query=query,
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


async def handle_status_result(context, chat_id, result, query=None):
    """
    Called when the user taps CHECK STATUS. Requesting a replacement number
    is handled separately via the HELP button.
    """
    kind, text = classify_status(result)

    if kind == "code":
        context.user_data["code_received"] = True
        await render(
            context, chat_id,
            f"✅ Your code is: `{text}`",
            keyboard=number_nav_keyboard(context),
            parse_mode="Markdown",
            query=query,
        )
        return

    if kind == "waiting":
        await render(context, chat_id, text, keyboard=number_nav_keyboard(context), query=query)
        return

    if kind == "cancelled":
        await render(context, chat_id, text, query=query, dead_end=True)
        return

    await render(context, chat_id, text, query=query, dead_end=True)


async def check_status(context, chat_id, query=None):
    activation_id = context.user_data.get("activation_id")
    expiry_time = context.user_data.get("expiry_time")

    if not activation_id:
        await render(context, chat_id, "ℹ️ You don't currently have an active number.", query=query, dead_end=True)
        return

    # Check our 20-minute period first
    if expiry_time and time.time() >= expiry_time:
        await render(context, chat_id, "⏰ Your 20-minute period has expired.", query=query, dead_end=True)
        return

    await render(context, chat_id, "🔄 Checking activation status...", query=query)

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
        await handle_status_result(context, chat_id, result, query=query)

    except Exception as e:
        await render(context, chat_id, f"❌ Status check error:\n{e}", query=query, dead_end=True)


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin/reference command that dumps HeroSMS's full country and service
    lists. Left as plain reply_text rather than folded into the single-screen
    pattern — these are large, one-off data dumps rather than navigation, so
    editing them in place wouldn't serve the "clean screen" goal the same way
    the core purchase/status flow does.
    """
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


async def post_init(application):
    # Configure Telegram's actual menu button next to the typing box.
    await application.bot.set_my_commands([
        BotCommand("start", "Start / main menu"),
        BotCommand("info", "Bot information"),
    ])
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # The 10-minute no-code reminder relies on PTB's JobQueue, which only
    # exists if the optional "job-queue" extra was installed
    # (pip install "python-telegram-bot[job-queue]"). Fail loudly here
    # rather than silently skipping every reminder.
    if app.job_queue is None:
        raise RuntimeError(
            "JobQueue is not available. Install the job-queue extra: "
            'pip install "python-telegram-bot[job-queue]"=="22.8"'
        )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CallbackQueryHandler(button_click))
    # Must be registered before the generic phone-number catch-all below,
    # otherwise a tap on the persistent 🏠 START button would be swallowed
    # as if it were a phone number.
    app.add_handler(MessageHandler(filters.Regex("^🏠 START$"), handle_start_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone_number))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
