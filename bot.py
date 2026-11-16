import os
import json
import asyncio
import datetime
import requests
import urllib3
import re
import sqlite3
import tempfile
from bs4 import BeautifulSoup
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from openai import AsyncOpenAI

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
4. Новий сайт розкладу: https://asu-sto.pnu.edu.ua/2026-2027-1/search-groups.html
5. Університет знаходиться у місті Івано-Франківськ, вулиця Шевченка, 57.
6. Гуманітарний корпус знаходиться у місті Івано-Франківськ, вулиця Шевченка, 57.
7. Приймальна комісія знаходиться у місті Івано-Франківськ, вулиця Шевченка, 57. Гуманітарний корпус (ауд. 101)
8. Педагогічний факультет — вул. С. Бандери, 1.
9. Факультет туризму — вул. Галицька, 201д.

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schedule_cache (
            group_name TEXT PRIMARY KEY,
            schedule_data TEXT,
            updated_at TEXT
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

def load_groups_db() -> dict:
    try:
        with open("groups.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def fix_cyrillic_letters(text: str) -> str:
    eng_chars = "ABCEHKMOPTXIXabcehkmoptxix"
    ukr_chars = "АВСЕНКМОРТХІХавсенкмортхіх"
    return text.translate(str.maketrans(eng_chars, ukr_chars))

def fetch_schedule_from_site(group_name: str, days_count: int = 14) -> str:
    clean_group = fix_cyrillic_letters(group_name.strip().upper())
    groups_db = load_groups_db()
    saved_id = groups_db.get(clean_group)
    
    candidate_ids = []
    if saved_id:
        candidate_ids.append(saved_id)
    
    for faculty_id in range(1000, 1020):
        for group_id_num in range(5000, 5800):
            generated_path = f"{faculty_id}/0-{group_id_num}"
            if generated_path not in candidate_ids:
                candidate_ids.append(generated_path)
                
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    valid_schedule = None
    found_path = None
    
    for candidate in candidate_ids:
        url = f"https://asu-sto.pnu.edu.ua/2026-2027-1/static/groups/{candidate}/"
        try:
            response = requests.get(url, headers=headers, timeout=3, verify=False)
            if response.status_code == 200:
                response.encoding = 'utf-8'
                soup = BeautifulSoup(response.text, 'html.parser')
                days_blocks = soup.find_all('div', class_='col-12 col-sm-6')
                if days_blocks:
                    schedule_text = f"📌 Актуальний розклад для {clean_group}:\n"
                    for day_block in days_blocks:
                        date_header = day_block.find('h4')
                        if not date_header: continue
                        schedule_text += f"\n--- {date_header.get_text(separator=' ', strip=True)} ---\n"
                        rows = day_block.find_all('tr')
                        for row in rows:
                            cells = row.find_all('td')
                            if len(cells) >= 3:
                                para_num = cells[0].get_text(strip=True)
                                content_cell = cells[2]
                                if not content_cell.get_text(strip=True): continue
                                links = [a.get('href') for a in content_cell.find_all('a') if a.get('href')]
                                content_text = content_cell.get_text(separator=" ", strip=True)
                                link_info = f" | Посилання: {', '.join(links)}" if links else ""
                                schedule_text += f"Пара {para_num}: {content_text}{link_info}\n"
                    
                    valid_schedule = schedule_text[:3500]
                    found_path = candidate
                    break
        except Exception:
            continue
            
    if valid_schedule and found_path:
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO schedule_cache (group_name, schedule_data, updated_at) VALUES (?, ?, ?)",
                       (clean_group, valid_schedule, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
        cursor.close()
        conn.close()
        return valid_schedule
        
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT schedule_data, updated_at FROM schedule_cache WHERE group_name = ?", (clean_group,))
    cached = cursor.fetchone()
    conn.close()
    
    if cached:
        return f"⚠️ Не вдалося знайти нову сторінку на сайті, але ось збережений розклад із бази (оновлено {cached[1]}):\n\n{cached[0]}"
        
    return f"❌ Не вдалося знайти розклад для групи {clean_group} ані на сайті, ані в базі."

def filter_schedule_for_user(schedule_text: str, excluded_electives: list) -> str:
    if not excluded_electives:
        return schedule_text
    lines = schedule_text.split('\n')
    filtered_lines = []
    for line in lines:
        skip = False
        for elective in excluded_electives:
            if elective.lower() in line.lower():
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
        f"Напишіть через кому назви вибіркових предметів, які ви **не обирали** і які не потрібно вам показувати у розкладі.\n"
        f"(Або надішліть слово `очистити`, щоб скинути фільтр)",
        parse_mode="Markdown"
    )

@dp.message(F.text == "📅 На сьогодні")
async def btn_today_schedule(message: types.Message):
    user_id = message.from_user.id
    group, excluded = get_user_data(user_id)
    if not group:
        await message.answer("Будь ласка, спершу вкажіть свою групу.", reply_markup=main_keyboard())
        return
    
    full_schedule = fetch_schedule_from_site(group, days_count=2)
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
    
    full_schedule = fetch_schedule_from_site(group, days_count=7)
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
    await message.answer("Введіть бажану дату у форматі ДД.ММ.РРРР (наприклад, `25.11.2026`):", parse_mode="Markdown")

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
        clean_group = fix_cyrillic_letters(user_text.upper())
        groups_db = load_groups_db()
        if clean_group in groups_db:
            save_user_group(user_id, clean_group)
            user_states.pop(user_id, None)
            await message.answer(f"✅ Групу **{clean_group}** успішно збережено в базі даних!", reply_markup=main_keyboard(), parse_mode="Markdown")
            return
        else:
            await message.answer(f"❌ Групу '{user_text}' не знайдено в довіднику ПНУ. Спробуйте ще раз (наприклад, `ІПЗ-42`).", parse_mode="Markdown")
            return

    if user_state == "waiting_for_electives":
        user_states.pop(user_id, None)
        if user_text.lower() == "очистити":
            save_user_electives(user_id, [])
            await message.answer("✅ Список вибіркових очищено. Тепер показуватимуться всі предмети.", reply_markup=main_keyboard())
        else:
            electives = [item.strip() for item in user_text.split(",") if item.strip()]
            save_user_electives(user_id, electives)
            await message.answer(f"✅ Збережено вибіркові для приховування: {', '.join(electives)}", reply_markup=main_keyboard())
        return

    if user_state == "waiting_for_date":
        user_states.pop(user_id, None)
        group, excluded = get_user_data(user_id)
        if not group:
            await message.answer("Спершу вкажіть свою групу.", reply_markup=main_keyboard())
            return
        
        match_date = re.search(r'\d{2}\.\d{2}\.\d{4}', user_text)
        if match_date:
            full_schedule = fetch_schedule_from_site(group, days_count=20)
            filtered = filter_schedule_for_user(full_schedule, excluded)
            target_date = match_date.group(0)
            if target_date in filtered:
                parts = filtered.split(target_date)
                day_schedule = parts[1].split('---')[0] if len(parts) > 1 else "Інформації на цей день не знайдено."
                await message.answer(f"📅 **Розклад на {target_date}:**\n{day_schedule}", reply_markup=main_keyboard(), parse_mode="Markdown")
            else:
                await message.answer(f"❌ На дату {target_date} розкладу не знайдено або пар немає.", reply_markup=main_keyboard())
        else:
            await message.answer("❌ Невірний формат дати. Використовуйте ДД.ММ.РРРР (наприклад, 25.11.2026).", reply_markup=main_keyboard())
        return

    match = re.search(r'([а-яїієґa-z]{2,}-\d{1,2}[а-яїієґa-z]*)', user_text.lower())
    if match and not get_user_data(user_id)[0]:
        group_name = fix_cyrillic_letters(match.group(1).upper())
        if group_name in load_groups_db():
            save_user_group(user_id, group_name)
            await message.answer(f"✅ Групу **{group_name}** збережено у базі даних!", reply_markup=main_keyboard(), parse_mode="Markdown")
            return

    group, excluded = get_user_data(user_id)
    wait_msg = await message.answer("Шукаю інформацію...")

    if "розклад" in user_text.lower() or "пари" in user_text.lower() or "пара" in user_text.lower():
        if group:
            real_schedule = fetch_schedule_from_site(group)
            real_schedule = filter_schedule_for_user(real_schedule, excluded)
            if "❌" in real_schedule and "⚠️" not in real_schedule:
                await wait_msg.edit_text(real_schedule)
                return
            context = f"Ось розклад для групи {group}:\n{real_schedule}"
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

async def background_schedule_updater():
    while True:
        await asyncio.sleep(10)
        try:
            groups_db = load_groups_db()
            for group_name in groups_db.keys():
                fetch_schedule_from_site(group_name, days_count=14)
                await asyncio.sleep(2)
        except Exception:
            pass
        await asyncio.sleep(7 * 24 * 60 * 60)

async def handle_ping(request): 
    return web.Response(text="Бот працює!")

async def main():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", 8080)))
    await site.start()
    
    asyncio.create_task(background_schedule_updater())
    await dp.start_polling(bot)

if __name__ == "__main__": 
    asyncio.run(main())