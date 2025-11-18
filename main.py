import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler, ConversationHandler
import sqlite3
from geopy.distance import geodesic
from datetime import datetime, timedelta
import threading
import requests
import time

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# States
CONTACT, CAR_NUMBER, LOCATION, ORDER_ACCEPTED, AT_CUSTOMER, END_TRIP, REASON, NEW_END_LOCATION, TOPUP_WAITING = range(9)

# Admin chat IDs (o'zingizning chat ID'ingizni qo'ying)
ADMINS = [123456789]  # Misol: 123456789

# Database
def init_db():
    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS drivers
                 (id INTEGER PRIMARY KEY, chat_id INTEGER, phone TEXT, car_number TEXT, balance REAL, location_lat REAL, location_lon REAL, status TEXT, orders_today INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders
                 (id INTEGER PRIMARY KEY, user_id INTEGER, driver_id INTEGER, start_lat REAL, start_lon REAL, end_lat REAL, end_lon REAL, distance REAL, status TEXT, date TEXT, created_at TEXT, fare REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_bonus
                 (user_id INTEGER PRIMARY KEY, bonus REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS topup_requests
                 (id INTEGER PRIMARY KEY, driver_id INTEGER, amount REAL, screenshot TEXT, status TEXT)''')
    conn.commit()
    conn.close()

def calculate_fare(distance_km):
    if distance_km <= 0:
        return 10000

    if 1.5 < distance_km <= 2.5:
        total_distance = distance_km + 0.5
    elif 2.5 < distance_km < 6:
        total_distance = distance_km + 1
    elif distance_km >= 6:
        total_distance = distance_km + 1.5
    else:
        total_distance = distance_km

    fare = total_distance * 1800
    return max(fare, 10000)

def offer_order_to_driver_with_details(driver_chat_id, start_lat, start_lon, end_lat, end_lon, order_id):
    start_url = f"https://yandex.ru/maps/?pt={start_lon},{start_lat}&z=15&l=map"
    if end_lat and end_lon:
        end_url = f"https://yandex.ru/maps/?pt={end_lon},{end_lat}&z=15&l=map"
        real_distance = geodesic((start_lat, start_lon), (end_lat, end_lon)).kilometers
        fare = calculate_fare(real_distance)

        message = f"✅ Yangi buyurtma!\nBorish manzili: [Yandex Maps]({start_url})\nYetib boriladigan joy: [Manzil]({end_url})\nMasofa: {real_distance:.2f} km\nNarx: {fare:.0f} so'm\nQabul qilasizmi?"
    else:
        real_distance = 0
        message = f"✅ Yangi buyurtma!\nBorish manzili: [Yandex Maps]({start_url})\nMijoz manzilni keyinroq aytadi.\nQabul qilasizmi?"

    keyboard = [
        [InlineKeyboardButton("✅ Qabul qilaman", callback_data=f"accept_{order_id}"),
         InlineKeyboardButton("❌ Rad etaman", callback_data=f"reject_{order_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    context.bot.send_message(chat_id=driver_chat_id, text=message, parse_mode="Markdown", reply_markup=reply_markup)

    # 10 soniya ichida qabul qilmasa, boshqa haydovchiga yuborish
    timer = threading.Timer(10.0, try_next_driver, args=(order_id, driver_chat_id))
    timer.start()

def try_next_driver(order_id, last_driver_id):
    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()
    c.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
    row = c.fetchone()
    if row and row[0] == 'pending':
        # Eski haydovchini qayta ishlashga tayyor qilish
        c.execute("UPDATE drivers SET status = 'available' WHERE chat_id = ?", (last_driver_id,))
        # Endi boshqa haydovchilarga yuborish
        c.execute("SELECT user_id, start_lat, start_lon, end_lat, end_lon FROM orders WHERE id = ?", (order_id,))
        order_data = c.fetchone()
        if order_data:
            user_id, start_lat, start_lon, end_lat, end_lon = order_data
            # Eng yaqin haydovchini qidirish
            c.execute("SELECT chat_id FROM drivers WHERE status = 'available' AND chat_id != ? ORDER BY ABS(location_lat - ?) + ABS(location_lon - ?) LIMIT 1",
                      (last_driver_id, start_lat, start_lon))
            next_driver = c.fetchone()
            if next_driver:
                next_driver_id = next_driver[0]
                offer_order_to_driver_with_details(next_driver_id, start_lat, start_lon, end_lat, end_lon, order_id)
            else:
                logger.info(f"Buyurtma {order_id} uchun boshqa haydovchi topilmadi.")
    conn.close()

def button_callback(update, context):
    query = update.callback_query
    query.answer()

    data = query.data
    if data.startswith("accept_"):
        order_id = int(data.split("_")[1])
        driver_chat_id = query.from_user.id

        conn = sqlite3.connect('taxi.db')
        c = conn.cursor()
        c.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        row = c.fetchone()
        if row and row[0] == 'pending':
            c.execute("UPDATE orders SET status='accepted', driver_id=? WHERE id=?", (driver_chat_id, order_id))
            c.execute("UPDATE drivers SET status='busy' WHERE chat_id=?", (driver_chat_id,))
            c.execute("UPDATE drivers SET orders_today = orders_today + 1 WHERE chat_id = ?", (driver_chat_id,))
            conn.commit()

            c.execute("SELECT start_lat, start_lon FROM orders WHERE id=?", (order_id,))
            loc = c.fetchone()
            if loc:
                context.bot.send_location(chat_id=driver_chat_id, latitude=loc[0], longitude=loc[1])

            context.bot.edit_message_text(chat_id=query.message.chat_id,
                                          message_id=query.message.message_id,
                                          text="✅ Buyurtma qabul qilindi! Mijoz yoniga borib, 'Mijoz yonidaman' tugmasini bosing.")

            keyboard = [['✅ Mijoz yonidaman']]
            context.bot.send_message(chat_id=driver_chat_id, text="Mijoz yoniga borib, quyidagi tugmani bosing:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        else:
            query.edit_message_text(text="❌ Bu buyurtma allaqachon bekor qilingan yoki boshqasi tomonidan qabul qilingan.")

    elif data.startswith("reject_"):
        query.edit_message_text(text="❌ Siz buyurtmani rad etdingiz.")
        # Boshqa haydovchiga yuborish
        order_id = int(query.data.split("_")[1])
        try_next_driver(order_id, query.from_user.id)

def at_customer(update, context):
    driver_chat_id = update.effective_message.chat_id

    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM orders WHERE driver_id = ? AND status = 'accepted'", (driver_chat_id,))
    order = c.fetchone()
    if order:
        user_id = order[0]
        context.bot.send_message(
            chat_id=user_id,
            text="🚕 Taxi sizni kutmoqda. Iltimos, chiqing.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Bekor qilish", callback_data=f"cancel_after_driver_arrives_{order[0]}")]
            ])
        )

    update.message.reply_text(
        "Mijoz yonidasiz. Endi manzilni yuboring:",
        reply_markup=ReplyKeyboardRemove()
    )
    return END_TRIP

def cancel_after_driver_arrives(update, context):
    query = update.callback_query
    query.answer()
    order_id = int(query.data.split("_")[-1])
    user_id = query.from_user.id

    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()

    # Bonus hisobini olish
    c.execute("SELECT bonus FROM user_bonus WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    current_bonus = row[0] if row else 0

    # -1000 so'm bonusni yechish
    new_bonus = current_bonus - 1000
    c.execute("UPDATE user_bonus SET bonus = ? WHERE user_id = ?", (new_bonus, user_id))

    # Buyurtma bekor qilindi
    c.execute("UPDATE orders SET status = 'cancelled_by_user_after_driver_arrived' WHERE id = ? AND user_id = ?", (order_id, user_id))

    # Haydovchini topish
    c.execute("SELECT driver_id FROM orders WHERE id = ?", (order_id,))
    driver = c.fetchone()
    if driver:
        driver_id = driver[0]
        # Haydovchi hisobiga 1000 so'm qo'shish
        c.execute("UPDATE drivers SET balance = balance + 1000 WHERE chat_id = ?", (driver_id,))

    conn.commit()
    conn.close()

    query.edit_message_text("❌ Buyurtma bekor qilindingiz. Bonus hisobingizdan 1000 so'm yechildi.")

def get_end_location(update, context):
    location = update.message.location
    if not location:
        update.message.reply_text("Iltimos, joylashuvni yuboring.")
        return END_TRIP

    driver_chat_id = update.effective_message.chat_id

    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()
    c.execute("SELECT start_lat, start_lon, user_id, distance FROM orders WHERE driver_id = ? AND status = 'accepted'", (driver_chat_id,))
    order = c.fetchone()

    if order:
        start_lat, start_lon, user_id, distance = order
        end_lat = location.latitude
        end_lon = location.longitude

        real_distance = geodesic((start_lat, start_lon), (end_lat, end_lon)).kilometers
        fare = calculate_fare(real_distance)

        # Buyurtma narxini saqlash
        c.execute("UPDATE orders SET end_lat=?, end_lon=?, distance=?, status='completed', date=?, fare=? WHERE driver_id=? AND status='accepted'",
                  (end_lat, end_lon, real_distance, str(datetime.now().date()), fare, driver_chat_id))

        service_fee = 1000
        total = fare - service_fee

        # Get user bonus
        c.execute("SELECT bonus FROM user_bonus WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        user_bonus = row[0] if row else 0

        # Add bonus to driver earnings
        if user_bonus > 0:
            total += user_bonus
            c.execute("UPDATE user_bonus SET bonus = 0 WHERE user_id = ?", (user_id,))

        # Update driver balance (service fee ni ayirish)
        c.execute("UPDATE drivers SET balance = balance + ? WHERE chat_id=?", (total, driver_chat_id))
        c.execute("UPDATE drivers SET status='available' WHERE chat_id=?", (driver_chat_id,))
        conn.commit()

        update.message.reply_text(f"✅ Yo'nalish tugadi!\nHisobingizga: {total:.0f} so'm o'tkazildi (xizmat haqi 1000 so'm ayirildi).")

    conn.close()
    show_driver_menu(update, context)
    return ConversationHandler.END

def show_driver_menu(update, context):
    keyboard = [
        ['🟢 Ishni boshlash', '🔴 Linyadan chiqish'],
        ['💰 Hisobim', '📊 Hisobot'],
        ['📊 Bugungi statistika', '💳 Hisobni to\'ldirish']
    ]
    update.message.reply_text("Asosiy menyu:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

def set_online(update, context):
    chat_id = update.effective_message.chat_id
    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()
    c.execute("UPDATE drivers SET status = 'available' WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    update.message.reply_text("🟢 Siz ishga kirishingiz! Buyurtmalar kelsa xabar beriladi.")

def set_offline(update, context):
    chat_id = update.effective_message.chat_id
    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()
    c.execute("UPDATE drivers SET status = 'offline' WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    update.message.reply_text("🔴 Siz linyadan chiqdingiz. Buyurtmalar kelmaydi.")

def show_balance(update, context):
    chat_id = update.effective_message.chat_id
    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()
    c.execute("SELECT balance FROM drivers WHERE chat_id = ?", (chat_id,))
    balance = c.fetchone()
    conn.close()
    if balance:
        update.message.reply_text(f"Hisobingiz: {balance[0]:.0f} so'm")
    else:
        update.message.reply_text("Hisob topilmadi.")

def show_stats(update, context):
    chat_id = update.effective_message.chat_id
    today = str(datetime.now().date())
    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()
    c.execute("SELECT orders_today FROM drivers WHERE chat_id = ?", (chat_id,))
    stats = c.fetchone()
    conn.close()
    if stats:
        update.message.reply_text(f"📊 Bugun bajargan buyurtmalaringiz: {stats[0]} ta")
    else:
        update.message.reply_text("Statistika topilmadi.")

def show_report(update, context):
    chat_id = update.effective_message.chat_id
    today = str(datetime.now().date())
    week_start = (datetime.now() - timedelta(days=7)).date()
    month_start = (datetime.now() - timedelta(days=30)).date()

    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()

    # Bugungi foyda
    c.execute("SELECT SUM(fare) FROM orders WHERE driver_id = ? AND date = ?", (chat_id, str(today)))
    today_earnings = c.fetchone()[0]
    today_earnings = today_earnings or 0

    # Haftalik foyda
    c.execute("SELECT SUM(fare) FROM orders WHERE driver_id = ? AND date >= ?", (chat_id, str(week_start)))
    week_earnings = c.fetchone()[0]
    week_earnings = week_earnings or 0

    # Oylik foyda
    c.execute("SELECT SUM(fare) FROM orders WHERE driver_id = ? AND date >= ?", (chat_id, str(month_start)))
    month_earnings = c.fetchone()[0]
    month_earnings = month_earnings or 0

    conn.close()

    report = f"""
📊 Hisobot:
🔹 Bugungi foyda: {today_earnings:.0f} so'm
🔹 Haftalik foyda: {week_earnings:.0f} so'm
🔹 Oylik foyda: {month_earnings:.0f} so'm
"""
    update.message.reply_text(report)

def start_topup(update, context):
    update.message.reply_text("💳 Hisobingizni to'ldirish uchun minimal 10000 so'm kerak.\nIltimos, to'lov qiling va skrinshot yuboring.")
    return TOPUP_WAITING

def receive_screenshot(update, context):
    chat_id = update.effective_message.chat_id
    photo = update.message.photo
    if not photo:
        update.message.reply_text("Iltimos, skrinshot yuboring.")
        return TOPUP_WAITING

    # Skrinshotni saqlash (fayl ID sifatida)
    file_id = photo[-1].file_id

    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()
    c.execute("INSERT INTO topup_requests (driver_id, screenshot, status) VALUES (?, ?, 'pending')", (chat_id, file_id))
    conn.commit()
    conn.close()

    update.message.reply_text("✅ So'rovingiz yuborildi. Admin tez orada ko'rib chiqadi.")
    # Adminlarga xabar yuborish
    for admin_id in ADMINS:
        context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=f"Yangi to'ldirish so'rovi: {chat_id}")

    show_driver_menu(update, context)
    return ConversationHandler.END

def admin_panel(update, context):
    user_id = update.effective_message.from_user.id
    if user_id not in ADMINS:
        update.message.reply_text("❌ Siz admin emassiz.")
        return

    update.message.reply_text("Admin panelga xush kelibsiz!")

def check_driver_balance(update, context):
    user_id = update.effective_message.from_user.id
    if user_id not in ADMINS:
        update.message.reply_text("❌ Siz admin emassiz.")
        return

    try:
        driver_id = int(context.args[0])
        conn = sqlite3.connect('taxi.db')
        c = conn.cursor()
        c.execute("SELECT balance FROM drivers WHERE chat_id = ?", (driver_id,))
        balance = c.fetchone()
        conn.close()
        if balance:
            update.message.reply_text(f"Driver {driver_id} hisobi: {balance[0]:.0f} so'm")
        else:
            update.message.reply_text("Driver topilmadi.")
    except (IndexError, ValueError):
        update.message.reply_text("Foydalanish: /balance <driver_id>")

def add_balance(update, context):
    user_id = update.effective_message.from_user.id
    if user_id not in ADMINS:
        update.message.reply_text("❌ Siz admin emassiz.")
        return

    try:
        driver_id = int(context.args[0])
        amount = float(context.args[1])
        conn = sqlite3.connect('taxi.db')
        c = conn.cursor()
        c.execute("UPDATE drivers SET balance = balance + ? WHERE chat_id = ?", (amount, driver_id))
        conn.commit()
        conn.close()
        update.message.reply_text(f"✅ {amount:.0f} so'm qo'shildi. Yangi hisob: {get_driver_balance(driver_id):.0f} so'm")
        context.bot.send_message(chat_id=driver_id, text=f"✅ Hisobingizga {amount:.0f} so'm qo'shildi.")
    except (IndexError, ValueError):
        update.message.reply_text("Foydalanish: /add <driver_id> <amount>")

def remove_balance(update, context):
    user_id = update.effective_message.from_user.id
    if user_id not in ADMINS:
        update.message.reply_text("❌ Siz admin emassiz.")
        return

    try:
        driver_id = int(context.args[0])
        amount = float(context.args[1])
        conn = sqlite3.connect('taxi.db')
        c = conn.cursor()
        c.execute("UPDATE drivers SET balance = balance - ? WHERE chat_id = ?", (amount, driver_id))
        conn.commit()
        conn.close()
        update.message.reply_text(f"✅ {amount:.0f} so'm olib tashlandi. Yangi hisob: {get_driver_balance(driver_id):.0f} so'm")
        context.bot.send_message(chat_id=driver_id, text=f"❌ Hisobingizdan {amount:.0f} so'm olib tashlandi.")
    except (IndexError, ValueError):
        update.message.reply_text("Foydalanish: /remove <driver_id> <amount>")

def get_driver_balance(driver_id):
    conn = sqlite3.connect('taxi.db')
    c = conn.cursor()
    c.execute("SELECT balance FROM drivers WHERE chat_id = ?", (driver_id,))
    balance = c.fetchone()
    conn.close()
    return balance[0] if balance else 0

def keep_alive():
    while True:
        try:
            # O'z serveringizni URL bilan almashtiring
            response = requests.get("https://asataxi.fly.dev")
            print(f"Serverga so'rov yuborildi: {response.status_code}")
        except Exception as e:
            print(f"Xatolik: {e}")
        time.sleep(30)  # 30 soniyada bir so'rov

def start_keep_alive():
    thread = threading.Thread(target=keep_alive)
    thread.daemon = True
    thread.start()

def main():
    start_keep_alive()  # Uyquga ketmaslik funksiyasini chaqirish
    init_db()
    updater = Updater("8434009950:AAEHGZeAO_ToBylEwGG93pMZF2-09gmnTtM", use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CONTACT: [MessageHandler(Filters.contact, get_contact)],
            CAR_NUMBER: [MessageHandler(Filters.text, get_car_number)],
            END_TRIP: [MessageHandler(Filters.location, get_end_location)],
            TOPUP_WAITING: [MessageHandler(Filters.photo, receive_screenshot)]
        },
        fallbacks=[]
    )

    dp.add_handler(conv_handler)
    dp.add_handler(CallbackQueryHandler(button_callback))
    dp.add_handler(CallbackQueryHandler(cancel_after_driver_arrives, pattern=r"cancel_after_driver_arrives_\d+"))
    dp.add_handler(MessageHandler(Filters.regex('🟢 Ishni boshlash'), set_online))
    dp.add_handler(MessageHandler(Filters.regex('🔴 Linyadan chiqish'), set_offline))
    dp.add_handler(MessageHandler(Filters.regex('💰 Hisobim'), show_balance))
    dp.add_handler(MessageHandler(Filters.regex('📊 Bugungi statistika'), show_stats))
    dp.add_handler(MessageHandler(Filters.regex('📊 Hisobot'), show_report))
    dp.add_handler(MessageHandler(Filters.regex('💳 Hisobni to\'ldirish'), start_topup))

    # Admin commands
    dp.add_handler(CommandHandler("admin", admin_panel))
    dp.add_handler(CommandHandler("balance", check_driver_balance))
    dp.add_handler(CommandHandler("add", add_balance))
    dp.add_handler(CommandHandler("remove", remove_balance))

    updater.start_webhook(listen="0.0.0.0", port=8080, url_path="8434009950:AAEHGZeAO_ToBylEwGG93pMZF2-09gmnTtM")
    updater.bot.set_webhook(url="https://asataxi.fly.dev/8434009950:AAEHGZeAO_ToBylEwGG93pMZF2-09gmnTtM")

if __name__ == '__main__':
    main()
