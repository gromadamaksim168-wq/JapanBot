import asyncio
import logging
import sqlite3
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# ==================== НАЛАШТУВАННЯ ====================
TELEGRAM_BOT_TOKEN = "8790991563:AAF_jhaPcw_9ykOilsOMNdVcnZQcCFR7x_U" # Заміни на повний свій токен
CHAT_ID = "1020837165"

# Фінансові параметри
ZENMARKET_FEE_JPY = 500       # Комісія ZenMarket (єн)
DOMESTIC_SHIPPING_JPY = 1200 # Орієнтовна доставка по Японії (єн)
JPY_TO_UAH_RATE = 0.27       # Курс єни до гривні
USD_TO_UAH_RATE = 41.0       # Курс долара до гривні
INT_SHIPPING_UAH = 450       # Доставка в Україну (1.2 кг)

MIN_PROFIT_USD = 50          # МІНІМАЛЬНИЙ ЧИСТИЙ ПРИБУТОК ($)

# Перевірка щохвилини для швидкого перехоплення фіксованих цін
CHECK_INTERVAL_SECONDS = 60  

# Середні орієнтовні ціни OLX (в гривнях) для розрахунку маржі
OLX_BENCHMARK_PRICES = {
    # 20-а серія
    "RTX 2060": 6000, "RTX 2060 Super": 7800,
    "RTX 2070": 7000, "RTX 2070 Super": 8800,
    "RTX 2080": 9200, "RTX 2080 Super": 10500, "RTX 2080 Ti": 12000,
    # 30-а серія
    "RTX 3060": 10500, "RTX 3060 Ti": 11800,
    "RTX 3070": 12900, "RTX 3070 Ti": 14500,
    "RTX 3080": 16800, "RTX 3080 Ti": 20500,
    "RTX 3090": 25000, "RTX 3090 Ti": 29000,
    # 40-а серія
    "RTX 4060": 12500, "RTX 4060 Ti": 19000,
    "RTX 4070": 21000, "RTX 4070 Super": 25000, "RTX 4070 Ti": 27000,
    "RTX 4080": 37000, "RTX 4080 Super": 42000,
    "RTX 4090": 68000,
    # 50-а серія
    "RTX 5060": 15000, "RTX 5070": 26000, "RTX 5080": 48000, "RTX 5090": 85000
}

TARGET_KEYWORDS = [
    "ジャンク", "ファン異音", "ファン不動", "汚れあり", "動作確認済", "映像出力不可"
]
# ======================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

def init_db():
    conn = sqlite3.connect("seen_lots.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS lots (auction_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def is_lot_seen(auction_id: str) -> bool:
    conn = sqlite3.connect("seen_lots.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM lots WHERE auction_id = ?", (auction_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def mark_lot_as_seen(auction_id: str):
    conn = sqlite3.connect("seen_lots.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO lots (auction_id) VALUES (?)", (auction_id,))
    conn.commit()
    conn.close()

def calculate_cost_uah(price_jpy: int) -> int:
    total_jpy = price_jpy + ZENMARKET_FEE_JPY + DOMESTIC_SHIPPING_JPY
    return int((total_jpy * JPY_TO_UAH_RATE) + INT_SHIPPING_UAH)

def is_less_than_one_hour(time_str: str) -> bool:
    """Перевірка для аукціонних лотів: чи залишилося <= 1 година"""
    time_str = time_str.strip()
    if "日" in time_str:
        return False
    if "分" in time_str and "時間" not in time_str:
        return True
    if "1時間" in time_str:
        return True
    return False

def fetch_yahoo_auctions(model_name: str):
    url = f"https://auctions.yahoo.co.jp/search/search?p={model_name}&va={model_name}&is_postage_mode=1&dest_availability=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return []
        
        soup = BeautifulSoup(response.text, "html.parser")
        items = soup.find_all("li", class_="Product")
        
        found_lots = []
        for item in items:
            title_tag = item.find("a", class_="Product__titleLink")
            price_tag = item.find("span", class_="Product__priceValue")
            time_tag = item.find("span", class_="Product__time")
            bid_or_buy_tag = item.find("span", class_="Product__bid")
            
            if not title_tag or not price_tag:
                continue
                
            title = title_tag.text.strip()
            link = title_tag["href"]
            auction_id = link.split("/")[-1]
            time_left_str = time_tag.text.strip() if time_tag else ""
            
            # Перевірка на "Купити зараз" (即決)
            is_buy_now = False
            if bid_or_buy_tag and "即決" in bid_or_buy_tag.text:
                is_buy_now = True
            elif "即決" in title or "即決" in item.text:
                is_buy_now = True

            if not is_buy_now and not is_less_than_one_hour(time_left_str):
                continue
                
            price_str = price_tag.text.replace("円", "").replace(",", "").strip()
            try:
                price_jpy = int(price_str)
            except ValueError:
                continue
                
            if any(kw in title for kw in TARGET_KEYWORDS) or model_name in title:
                found_lots.append({
                    "id": auction_id,
                    "title": title,
                    "price_jpy": price_jpy,
                    "time_left": time_left_str if not is_buy_now else "Фіксована ціна (Buy It Now)",
                    "is_buy_now": is_buy_now,
                    "link": link
                })
        return found_lots
    except Exception as e:
        logging.error(f"Помилка парсингу {model_name}: {e}")
        return []

async def monitor_auctions():
    while True:
        for model_name, olx_price in OLX_BENCHMARK_PRICES.items():
            lots = fetch_yahoo_auctions(model_name)
            for lot in lots:
                if not is_lot_seen(lot["id"]):
                    mark_lot_as_seen(lot["id"])
                    
                    est_cost_uah = calculate_cost_uah(lot["price_jpy"])
                    est_profit_uah = olx_price - est_cost_uah
                    est_profit_usd = est_profit_uah / USD_TO_UAH_RATE
                    
                    if est_profit_usd >= MIN_PROFIT_USD:
                        if lot["is_buy_now"]:
                            header = "⚡️ **ФІКСОВАНА ЦІНА (BUY IT NOW) — КУПУЙ ОДРАЗУ!**"
                        else:
                            header = "⏰ **АУКЦІОН СКОРО ЗАКІНЧУЄТЬСЯ (<1 год)!**"
                        
                        message_text = (
                            f"{header}\n"
                            f"🔥 **Модель:** {model_name}\n\n"
                            f"📌 **Заголовок:** {lot['title']}\n"
                            f"⏳ **Статус/Час:** {lot['time_left']}\n"
                            f"💴 **Ціна:** {lot['price_jpy']:,} JPY\n"
                            f"📦 **Собівартість в UA:** ~{est_cost_uah:,} грн\n"
                            f"🏷 **Ціна на OLX:** ~{olx_price:,} грн\n"
                            f"💰 **Очікуваний прибуток:** ~${est_profit_usd:.1f} ({est_profit_uah:,} грн)\n\n"
                            f"🔗 [Переглянути на Yahoo]({lot['link']})\n"
                            f"🛒 [Купити через ZenMarket](https://zenmarket.jp/uk/auction.aspx?itemCode={lot['id']})"
                        )
                        
                        try:
                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=message_text,
                                parse_mode="Markdown",
                                disable_web_page_preview=False
                            )
                            await asyncio.sleep(1)
                        except Exception as e:
                            logging.error(f"Помилка надсилання: {e}")
                            
            await asyncio.sleep(2)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("🤖 Бот запущено! Відстежую аукціони (<1 год) та фіксовані ціна (Buy It Now) із маржею від $50.")

async def main():
    init_db()
    asyncio.create_task(monitor_auctions())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
