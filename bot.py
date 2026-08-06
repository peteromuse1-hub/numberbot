from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
import requests
import time
from datetime import datetime, timedelta

BOT_TOKEN = "8186183293:AAFYN9hKXYAbjjS4sltcgFOxJapx3tcjz0Y"
HEROSMS_API_KEY = "20f8c0081fdA8179561b0d7dfA686958"

HEROSMS_URL = "https://hero-sms.com/stubs/handler_api.php"

# Your selected settings
COUNTRY = 8
SERVICE = "vi"
MAX_PRICE = 0.022

# 20 minutes
VALIDITY_MINUTES = 20


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📱 GET NUMBER", callback_data="get_number")],
        [InlineKeyboardButton("📋 MY NUMBER", callback_data="my_number")],
        [InlineKeyboardButton("🔄 CHECK STATUS", callback_data="check_status")],
    ]

    await update.message.reply_text(
        "👋 Welcome!\n\n"
        "Get your temporary number here.\n\n"
        "⏱️ You have 20 minutes from the time "
        "of purchase to use your number.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "get_number":
        await get_number(query, context)

    elif query.data == "my_number":
        await show_number(query, context)

    elif query.data == "check_status":
        await check_status(query, context)


async def get_number(query, context):
    await query.message.reply_text(
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

                await query.message.reply_text(
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
                await query.message.reply_text(
                    "❌ The number service returned "
                    "an unexpected response."
                )

        elif result == "NO_NUMBERS":
            await query.message.reply_text(
                "❌ No numbers are currently available."
            )

        else:
            await query.message.reply_text(
                f"❌ Number request failed:\n{result}"
            )

    except Exception as e:
        await query.message.reply_text(
            f"❌ Connection error:\n{e}"
        )


async def show_number(query, context):
    number = context.user_data.get("number")
    purchase_time = context.user_data.get("purchase_time")
    expiry_time = context.user_data.get("expiry_time")

    if not number or not purchase_time or not expiry_time:
        await query.message.reply_text(
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
        await query.message.reply_text(
            "⏰ YOUR NUMBER HAS EXPIRED\n\n"
            f"📱 Number: {number}\n"
            f"🕐 Purchased: {purchased}\n"
            f"⏰ Expired: {expires}"
        )
        return

    minutes = int(remaining // 60)
    seconds = int(remaining % 60)

    await query.message.reply_text(
        "📱 YOUR NUMBER\n\n"
        f"{number}\n\n"
        f"🕐 Purchased: {purchased}\n"
        f"⏰ Expires: {expires}\n"
        f"⌛ Time remaining: {minutes}m {seconds}s"
    )


async def check_status(query, context):
    activation_id = context.user_data.get("activation_id")
    expiry_time = context.user_data.get("expiry_time")

    if not activation_id:
        await query.message.reply_text(
            "ℹ️ You don't currently have an active number."
        )
        return

    # Check our 20-minute period first
    if expiry_time and time.time() >= expiry_time:
        await query.message.reply_text(
            "⏰ Your 20-minute period has expired."
        )
        return

    await query.message.reply_text(
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

        # We deliberately don't display SMS/call codes.
        if result:
            await query.message.reply_text(
                "📡 Activation status received.\n\n"
                "The activation is still within "
                "the 20-minute period.\n\n"
                "Use MY NUMBER to see your number."
            )
        else:
            await query.message.reply_text(
                "ℹ️ No status information is currently available."
            )

    except Exception as e:
        await query.message.reply_text(
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


app = Application.builder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("info", info))
app.add_handler(CallbackQueryHandler(button_click))

print("Bot is running...")
app.run_polling()
