import os
import json
import asyncio
import datetime
import sqlite3
import tempfile
import re
import traceback
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from openai import AsyncOpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

ai_client = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

user_states = {}

PNU_KNOWLEDGE_BASE = """
Ти — офіційний віртуальний куратор Прикарпатського національного університету імені Василя Стефаника (ПНУ).
Твоє завдання: надавати правдиву інформацію про університет.
Головні факти про ПНУ:
1. Офіційна назва: Прикарпатський національний університет імені Василя Стефаника (ПНУ).
2. Ректор університету: Валентина Михайлівна Якубів.
3. Офіційний сайт: https://pnu.edu.ua/
4. Університет знаходиться у місті Івано-Франківськ, вулиця Шевченка, 57.
5. Гуманітарний корпус знаходиться у місті Івано-Франківськ, вулиця Шевченка, 57.
6. Приймальна комісія знаходиться у місті Івано-Франківськ, вулиця Шевченка, 57. Гуманітарний корпус (ауд. 101)
7. Педагогічний факультет — вул. С. Бандери, 1.
8. Факультет туризму — вул. Галицька, 201д.

ВАЖЛИВО: НІКОЛИ не використовуй Markdown-таблиці (з символами |).
Форматуй розклад у вигляді красивого списку з емодзі.
Виділяй дні тижня жирним шрифтом: **📅 Понеділок (5 жовтня)**.
Кожну пару форматуй так: '🔹 **[Номер] пара ([Час]):** [Предмет] ([Тип]) — [Викладач] 📍 [Аудиторія]'.
Відповідай коротко і без вигадок.
"""

def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            group_name TEXT,
            excluded_electives TEXT DEFAULT '[]'
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_user_data(user_id: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT group_name, excluded_electives FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0], json.loads(row[1])
    return None, []

def save_user_group(user_id: int, group_name: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, group_name) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET group_name = excluded.group_name
    """, (user_id, group_name))
    conn.commit()
    conn.close()

def save_user_electives(user_id: int, electives: list):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, excluded_electives) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET excluded_electives = excluded.excluded_electives
    """, (user_id, json.dumps(electives)))
    conn.commit()
    conn.close()

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 На сьогодні"), KeyboardButton(text="📆 На цей тиждень")],
            [KeyboardButton(text="🔍 На конкретний день"), KeyboardButton(text="⚙️ Вибір вибіркових")],
            [KeyboardButton(text="💬 Написати ШІ"), KeyboardButton(text="🏫 Обрати/Змінити групу")]
        ],
        resize_keyboard=True
    )

def load_schedule_file() -> dict:
    try:
        with open("schedule.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def fetch_schedule_from_local(group_name: str, days_count: int = 14) -> str:
    clean_group = group_name.strip().upper()
    data = load_schedule_file()
    group_days = data.get(clean_group)
    
    if not group_days:
        return f"❌ Групу '{group_name}' не знайдено в локальній базі розкладу."
    
    today = datetime.datetime.now()
    end_date = today + datetime.timedelta(days=days_count)
    
    schedule_text = f"📌 Актуальний розклад для {clean_group}:\n"
    found_any = False
    
    for day_item in group_days:
        date_str = day_item.get("date")
        day_name = day_item.get("day")
        try:
            d_obj = datetime.datetime.strptime(date_str, "%d.%m.%Y")
            if today.date() <= d_obj.date() <= end_date.date():
                found_any = True
                schedule_text += f"\n--- 📅 **{day_name} ({date_str})** ---\n"
                for p in day_item.get("pairs", []):
                    link_info = f" 🔗 {p['link']}" if p.get('link') else ""
                    loc_info = f" 📍 {p['location']}" if p.get('location') else ""
                    schedule_text += f"🔹 **Пара {p['num']} ({p['time']}):** {p['subject']} — {p['teacher']}{loc_info}{link_info}\n"
        except Exception:
            continue
            
    if not found_any:
        return f"❌ На найближчі дні для групи {clean_group} пар не знайдено."
        
    return schedule_text[:3500]

def filter_schedule_for_user(schedule_text: str, excluded_electives: list) -> str:
    if not excluded_electives:
        return schedule_text
    lines = schedule_text.split('\n')
    filtered_lines = []
    for line in lines:
        skip = False
        for elective in excluded_electives:
            if elective.strip().lower() in line.lower():
                skip = True
                break
        if not skip:
            filtered_lines.append(line)
    return '\n'.join(filtered_lines)

def get_group_subjects(group_name: str) -> list:
    data = load_schedule_file()
    group_days = data.get(group_name.strip().upper(), [])
    subjects = set()
    for day in group_days:
        for p in day.get("pairs", []):
            subjects.add(p["subject"])
    return sorted(list(subjects))

def generate_electives_keyboard(group_name: str, selected: list):
    subjects = get_group_subjects(group_name)
    keyboard_rows = []
    for subj in subjects:
        if subj in selected:
            keyboard_rows.append([KeyboardButton(text=f"❌ Приховати: {subj}")])
        else:
            keyboard_rows.append([KeyboardButton(text=f"✅ Показувати: {subj}")])
    keyboard_rows.append([KeyboardButton(text="✅ Готово")])
    return ReplyKeyboardMarkup(keyboard=keyboard_rows, resize_keyboard=True)

# Функція щоденної вечірньої розсилки розкладу на завтра о 21:00
async def send_evening_schedule_broadcast():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, group_name, excluded_electives FROM users WHERE group_name IS NOT NULL")
    users = cursor.fetchall()
    conn.close()

    if not users:
        return

    tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
    target_date = tomorrow.strftime("%d.%m.%Y")
    data = load_schedule_file()

    for user_id, group_name, excluded_json in users:
        try:
            excluded = json.loads(excluded_json) if excluded_json else []
            group_days = data.get(group_name.strip().upper(), [])
            day_found = None
            for d in group_days:
                if d.get("date") == target_date:
                    day_found = d
                    break
            
            if day_found:
                res = f"🌙 **Вечірнє нагадування: розклад на завтра ({target_date}, {day_found.get('day')}):**\n"
                for p in day_found.get("pairs", []):
                    link_info = f" 🔗 {p['link']}" if p.get('link') else ""
                    loc_info = f" 📍 {p['location']}" if p.get('location') else ""
                    res += f"🔹 **Пара {p['num']} ({p['time']}):** {p['subject']} — {p['teacher']}{loc_info}{link_info}\n"
                
                filtered = filter_schedule_for_user(res, excluded)
                await bot.send_message(user_id, filtered, parse_mode="Markdown")
        except Exception as e:
            print(f"Помилка розсилки для користувача {user_id}: {e}")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    user_states.pop(user_id, None)
    group, _ = get_user_data(user_id)
    text = "Привіт! Я віртуальний помічник ПНУ ім. В. Стефаника.\n"
    if group:
        text += f"Ваша збережена група: **{group}**."
    else:
        text += "Будь ласка, оберіть або введіть назву своєї групи (наприклад, ІПЗ-42)."
    await message.answer(text, reply_markup=main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "🏫 Обрати/Змінити групу")
async def btn_change_group(message: types.Message):
    user_id = message.from_user.id
    user_states[user_id] = "waiting_for_group"
    await message.answer("Введіть назву вашої академічної групи (наприклад, `ІПЗ-42`):", reply_markup=main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "💬 Написати ШІ")
async def btn_chat_ai(message: types.Message):
    user_id = message.from_user.id
    user_states[user_id] = "chat_with_ai"
    await message.answer("💬 Режим розмови з ШІ активовано! Задавайте будь-які запитання про університет.", reply_markup=main_keyboard())

@dp.message(F.text == "⚙️ Вибір вибіркових")
async def btn_electives(message: types.Message):
    user_id = message.from_user.id
    group, excluded = get_user_data(user_id)
    if not group:
        await message.answer("Спершу оберіть свою групу через кнопку «🏫 Обрати/Змінити групу».", reply_markup=main_keyboard())
        return
    user_states[user_id] = {"state": "selecting_electives", "selected": list(excluded)}
    kb = generate_electives_keyboard(group, excluded)
    await message.answer("⚙️ Натискайте на предмети нижче, щоб змінити їх статус (приховати/показувати). Після завершення натисніть «✅ Готово»:", reply_markup=kb)

@dp.message(F.text == "📅 На сьогодні")
async def btn_today_schedule(message: types.Message):
    user_id = message.from_user.id
    user_states.pop(user_id, None)
    group, excluded = get_user_data(user_id)
    if not group:
        await message.answer("Будь ласка, спершу вкажіть свою групу.", reply_markup=main_keyboard())
        return
    
    full_schedule = fetch_schedule_from_local(group, days_count=1)
    filtered = filter_schedule_for_user(full_schedule, excluded)
    today_str = datetime.datetime.now().strftime("%d.%m.%Y")
    await message.answer(f"📅 **Розклад на сьогодні ({today_str}):**\n\n{filtered[:3000]}", reply_markup=main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "📆 На цей тиждень")
async def btn_week_schedule(message: types.Message):
    user_id = message.from_user.id
    user_states.pop(user_id, None)
    group, excluded = get_user_data(user_id)
    if not group:
        await message.answer("Будь ласка, спершу вкажіть свою групу.", reply_markup=main_keyboard())
        return
    
    full_schedule = fetch_schedule_from_local(group, days_count=7)
    filtered = filter_schedule_for_user(full_schedule, excluded)
    await message.answer(filtered, reply_markup=main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "🔍 На конкретний день")
async def btn_custom_day(message: types.Message):
    user_id = message.from_user.id
    group, _ = get_user_data(user_id)
    if not group:
        await message.answer("Спершу оберіть свою групу.", reply_markup=main_keyboard())
        return
    user_states[user_id] = "waiting_for_date"
    await message.answer("Введіть бажану дату у форматі ДД.ММ.РРРР (наприклад, `17.11.2026`):", reply_markup=main_keyboard(), parse_mode="Markdown")

@dp.message(F.voice)
async def voice_handler(message: types.Message):
    wait_msg = await message.answer("🎧 Слухаю та розпізнаю ваше голосове повідомлення...")
    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_audio:
        temp_audio_path = temp_audio.name
        
    await bot.download(file, destination=temp_audio_path)
    
    try:
        with open(temp_audio_path, "rb") as audio_file:
            transcript = await ai_client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=audio_file
            )
        user_text = transcript.text
        await wait_msg.edit_text(f"🗣 Ви сказали: _{user_text}_", parse_mode="Markdown")
        message.text = user_text
        await ai_handler(message)
    except Exception as e:
        await wait_msg.edit_text(f"❌ Не вдалося розпізнати голосове повідомлення: {e}")
    finally:
        if os.path.exists(temp_audio_path):
            os.remove(temp_audio_path)

@dp.message()
async def ai_handler(message: types.Message):
    user_id = message.from_user.id
    user_text = message.text.strip()
    user_state = user_states.get(user_id)
    
    if user_state == "waiting_for_group":
        clean_group = user_text.upper()
        data = load_schedule_file()
        if clean_group in data:
            save_user_group(user_id, clean_group)
            user_states.pop(user_id, None)
            await message.answer(f"✅ Групу **{clean_group}** успішно збережено в базі даних!", reply_markup=main_keyboard(), parse_mode="Markdown")
            return
        else:
            await message.answer(f"❌ Групу '{user_text}' не знайдено в базі розкладу. Спробуйте ще раз (наприклад, `ІПЗ-42`).", reply_markup=main_keyboard())
            return

    if isinstance(user_state, dict) and user_state.get("state") == "selecting_electives":
        group, _ = get_user_data(user_id)
        selected = user_state["selected"]
        
        if user_text == "✅ Готово":
            save_user_electives(user_id, selected)
            user_states.pop(user_id, None)
            ex_text = ", ".join(selected) if selected else "немає"
            await message.answer(f"✅ Збережено приховані вибіркові предмети: _{ex_text}_", reply_markup=main_keyboard(), parse_mode="Markdown")
            return
        
        if user_text.startswith("❌ Приховати: "):
            subject_name = user_text.replace("❌ Приховати: ", "").strip()
            if subject_name in selected:
                selected.remove(subject_name)
        elif user_text.startswith("✅ Показувати: "):
            subject_name = user_text.replace("✅ Показувати: ", "").strip()
            if subject_name not in selected:
                selected.append(subject_name)
                
        kb = generate_electives_keyboard(group, selected)
        await message.answer("Оновлено вибір. Натисніть інші предмети або «✅ Готово»:", reply_markup=kb)
        return

    if user_state == "waiting_for_date":
        user_states.pop(user_id, None)
        group, excluded = get_user_data(user_id)
        if not group:
            await message.answer("Спершу вкажіть свою групу.", reply_markup=main_keyboard())
            return
        
        match_date = re.search(r'\d{2}\.\d{2}\.\d{4}', user_text)
        if match_date:
            target_date = match_date.group(0)
            data = load_schedule_file()
            group_days = data.get(group, [])
            day_found = None
            for d in group_days:
                if d.get("date") == target_date:
                    day_found = d
                    break
            if day_found:
                res = f"📅 **Розклад на {target_date} ({day_found.get('day')}):**\n"
                for p in day_found.get("pairs", []):
                    link_info = f" 🔗 {p['link']}" if p.get('link') else ""
                    loc_info = f" 📍 {p['location']}" if p.get('location') else ""
                    res += f"🔹 **Пара {p['num']} ({p['time']}):** {p['subject']} — {p['teacher']}{loc_info}{link_info}\n"
                filtered = filter_schedule_for_user(res, excluded)
                await message.answer(filtered, reply_markup=main_keyboard(), parse_mode="Markdown")
            else:
                await message.answer(f"❌ На дату {target_date} розкладу не знайдено або пар немає.", reply_markup=main_keyboard())
        else:
            await message.answer("❌ Невірний формат дати. Використовуйте ДД.ММ.РРРР (наприклад, 17.11.2026).", reply_markup=main_keyboard())
        return

    # Якщо активовано режим спілкування з ШІ
    if user_state == "chat_with_ai":
        wait_msg = await message.answer("Думаю над відповіддю...")
        try:
            response = await ai_client.chat.completions.create(
                model="llama-3.1-8b-instant",  # Актуальна та стабільна модель Groq
                messages=[
                    {"role": "system", "content": PNU_KNOWLEDGE_BASE},
                    {"role": "user", "content": user_text}
                ]
            )
            ai_text = response.choices[0].message.content
            await wait_msg.edit_text(ai_text)
        except Exception as e:
            traceback.print_exc()
            await wait_msg.edit_text(f"Помилка зв'язку з ШІ: {e}")
        return

    # За замовчуванням, якщо жоден стан не вибрано
    await message.answer("Використовуйте кнопки нижче або натисніть «💬 Написати ШІ», щоб поспілкуватися з асистентом.", reply_markup=main_keyboard())

async def handle_ping(request): 
    return web.Response(text="Бот працює!")

async def main():
    # Налаштування фонового планувальника для розсилки о 21:00 щодня
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    scheduler.add_job(send_evening_schedule_broadcast, "cron", hour=21, minute=0)
    scheduler.start()

    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", 8080)))
    await site.start()
    
    await dp.start_polling(bot)

if __name__ == "__main__": 
    asyncio.run(main())