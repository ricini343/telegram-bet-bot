import logging
import os
import json
import re
import base64
import io
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, filters, ContextTypes
from telegram.constants import ChatAction
from PIL import Image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AWAITING_PERCENTAGE = 1
pending_bets = {}

def analyze_screenshot(image_base64):
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": "claude-3-opus-20240229",
        "max_tokens": 1024,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": image_base64}
            }, {
                "type": "text",
                "text": "Analyze this betting screenshot. Extract ALL games/bets. Return ONLY valid JSON: {\"sport\": \"Football/Basketball/Tennis\", \"games\": [{\"match\": \"Team A vs Team B\", \"bet\": \"description\"}]}"
            }]
        }]
    }
    response = requests.post(url, json=payload, headers=headers)
    return response.json()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🎯 Bet Parlay Analyzer Bot\n\nSend me a screenshot of your bet!")

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message.photo:
        await update.message.reply_text("Please send a screenshot of your bet.")
        return
    
    try:
        await update.message.chat.send_action(ChatAction.TYPING)
        photo_file = await update.message.photo[-1].get_file()
        photo_data = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_data))
        
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        img_base64 = base64.b64encode(buffered.getvalue()).decode()
        
        logger.info("Analyzing with Claude...")
        response = analyze_screenshot(img_base64)
        
        if "error" in response:
            error_msg = response['error'].get('message', 'Unknown error')
            logger.error(f"API Error: {error_msg}")
            await update.message.reply_text(f"❌ API Error: {error_msg}")
            return
        
        response_text = response["content"][0]["text"].strip()
        try:
            bet_data = json.loads(response_text)
        except:
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            bet_data = json.loads(json_match.group()) if json_match else {"sport": "Unknown", "games": []}
        
        num_legs = len(bet_data.get("games", []))
        if num_legs == 1:
            parlay_type = "Single Bet"
        elif num_legs == 2:
            parlay_type = "2 Leg Parlay"
        elif num_legs == 3:
            parlay_type = "3 Leg Parlay"
        else:
            parlay_type = f"{num_legs} Leg Parlay"
        
        user_id = update.effective_user.id
        pending_bets[user_id] = {
            "photo_file_id": update.message.photo[-1].file_id,
            "sport": bet_data.get("sport", "Sports"),
            "games": bet_data.get("games", []),
            "parlay_type": parlay_type
        }
        
        games_text = "\n".join([f"⚽ {game['match']} — {game['bet']}" for game in bet_data.get("games", [])])
        preview = f"🏆 {bet_data.get('sport', 'Sports')} — {parlay_type}\n{games_text}\n\nWhat % of your bank?"
        await update.message.reply_text(preview)
        return AWAITING_PERCENTAGE
        
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        percentage_text = update.message.text.strip()
        percentage = None
        if "%" in percentage_text:
            num = re.search(r'(\d+(?:\.\d+)?)', percentage_text)
            if num:
                percentage = float(num.group(1))
        else:
            percentage = float(percentage_text)
        
        if not (0 < percentage <= 100):
            await update.message.reply_text("Please enter a valid percentage between 1 and 100.")
            return AWAITING_PERCENTAGE
        
        user_id = update.effective_user.id
        if user_id not in pending_bets:
            await update.message.reply_text("Bet data expired. Send a new screenshot.")
            return None
        
        bet_info = pending_bets[user_id]
        games_text = "\n".join([f"⚽ {game['match']} — {game['bet']}" for game in bet_info["games"]])
        final_message = f"🏆 {bet_info['sport']} — {bet_info['parlay_type']}\n{games_text}\n💰 {percentage}% of your bank"
        
        vip_channel_id = os.getenv("VIP_CHANNEL_ID")
        if not vip_channel_id:
            await update.message.reply_text("❌ VIP channel not configured.")
            return None
        
        await context.bot.send_photo(chat_id=vip_channel_id, photo=bet_info["photo_file_id"], caption=final_message, parse_mode="HTML")
        await update.message.reply_text("✅ Bet posted to VIP channel!")
        del pending_bets[user_id]
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("Please enter a valid percentage (e.g., 5 or 5.5)")
        return AWAITING_PERCENTAGE
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id in pending_bets:
        del pending_bets[user_id]
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")
    
    app = Application.builder().token(token).build()
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, handle_screenshot)],
        states={AWAITING_PERCENTAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_percentage), CommandHandler("cancel", cancel)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    logger.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
