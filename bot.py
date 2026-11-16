import os
import json
import asyncio
import datetime
import sqlite3
import tempfile
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from openai import AsyncOpenAI

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
1. Офіційна назва: Карпатський національний університет імені Василя Стефаника (ПНУ).
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
            [KeyboardButton(text="🏫 Обрати/Змінити групу")]
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

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    group, _ = get_user_data(user_id)
    text = "Привіт! Я віртуальний помічник ПНУ ім. В. Стефаника.\n"
    if group:
        text += f"Ваша збережена група: **{group}**."
    else:
        text += "Будь ласка, оберіть або введіть назву своєї групи (наприклад, ІПЗ-42)."
    await message.answer(text, reply_markup=main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "🏫 Обрати/Змінити групу")
async def btn_change_group(message: types.Message):
    user_states[message.from_user.id] = "waiting_for_group"
    await message.answer("Введіть назву вашої академічної групи (наприклад, `ІПЗ-42`):", parse_mode="Markdown")

@dp.message(F.text == "⚙️ Вибір вибіркових")
async def btn_electives(message: types.Message):
    user_id = message.from_user.id
    group, excluded = get_user_data(user_id)
    if not group:
        await message.answer("Спершу оберіть свою групу через кнопку «🏫 Обрати/Змінити групу».", reply_markup=main_keyboard())
        return
    user_states[user_id] = "waiting_for_electives"
    current_excluded = ", ".join(excluded) if excluded else "немає"
    await message.answer(
        f"⚙️ **Управління вибірковими дисциплінами**\n\n"
        f"Зараз приховані предмети: _{current_excluded}_\n\n"
        f"Напишіть через кому нові назви вибіркових предметів, які потрібно додати до списку приховування.\n"
        f"(Або надішліть слово `очистити`, щоб скинути весь фільтр)",
        parse_mode="Markdown"
    )

@dp.message(F.text == "📅 На сьогодні")
async def btn_today_schedule(message: types.Message):
    user_id = message.from_user.id
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
    await message.answer("Введіть бажану дату у форматі ДД.ММ.РРРР (наприклад, `17.11.2026`):", parse_mode="Markdown")

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
            await message.answer(f"❌ Групу '{user_text}' не знайдено в базі розкладу. Спробуйте ще раз (наприклад, `ІПЗ-42`).", parse_mode="Markdown")
            return

    if user_state == "waiting_for_electives":
        user_states.pop(user_id, None)
        _, current_excluded = get_user_data(user_id)
        if user_text.lower() == "очистити":
            save_user_electives(user_id, [])
            await message.answer("✅ Список вибіркових очищено. Тепер показуватимуться всі предмети.", reply_markup=main_keyboard())
        else:
            new_electives = [item.strip() for item in user_text.split(",") if item.strip()]
            updated_electives = list(set(current_excluded + new_electives))
            save_user_electives(user_id, updated_electives)
            await message.answer(f"✅ Оновлено список вибіркових для приховування: {', '.join(updated_electives)}", reply_markup=main_keyboard())
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

    match = re.search(r'([а-яїієґa-z]{2,}-\d{1,2}[а-яїієґa-z]*)', user_text.lower())
    if match and not get_user_data(user_id)[0]:
        group_name = match.group(1).upper()
        if group_name in load_schedule_file():
            save_user_group(user_id, group_name)
            await message.answer(f"✅ Групу **{group_name}** збережено у базі даних!", reply_markup=main_keyboard(), parse_mode="Markdown")
            return

    group, excluded = get_user_data(user_id)
    wait_msg = await message.answer("Шукаю інформацію...")

    if "розклад" in user_text.lower() or "пари" in user_text.lower() or "пара" in user_text.lower():
        if group:
            real_schedule = fetch_schedule_from_local(group, days_count=7)
            real_schedule = filter_schedule_for_user(real_schedule, excluded)
            await wait_msg.edit_text(real_schedule, reply_markup=main_keyboard())
            return
        else:
            context = "Попроси студента спочатку обрати свою групу за допомогою кнопки «🏫 Обрати/Змінити групу»."
    else:
        context = f"Студент питає: '{user_text}'. Використовуй інформацію з бази знань (PNU_KNOWLEDGE_BASE), щоб надати точну відповідь."

    try:
        response = await ai_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": PNU_KNOWLEDGE_BASE},
                {"role": "user", "content": context}
            ]
        )
        ai_text = response.choices[0].message.content
        await wait_msg.edit_text(ai_text, reply_markup=main_keyboard())
    except Exception as e:
        await wait_msg.edit_text(f"Помилка зв'язку з ШІ: {e}", reply_markup=main_keyboard())

async def handle_ping(request): 
    return web.Response(text="Бот працює!")

async def main():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", 8080)))
    await site.start()
    
    await dp.start_polling(bot)

if __name__ == "__main__": 
    asyncio.run(main())