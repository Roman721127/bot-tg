import os
import json
import asyncio
import datetime
import requests
import urllib3
import re
from bs4 import BeautifulSoup
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
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

user_history = {}
user_groups = {}

PNU_KNOWLEDGE_BASE = """
Ти — офіційний віртуальний куратор Прикарпатського національного університету імені Василя Стефаника (ПНУ).
Твоє завдання: надавати правдиву інформацію про університет.
Головні факти про ПНУ:
1. Офіційна назва: Прикарпатський національний університет імені Василя Стефаника (ПНУ).
2. Ректор університету: Цепенда Ігор Євгенович.
3. Офіційний сайт: https://pnu.edu.ua/
4. Сайт розкладу: https://asu-srv.pnu.edu.ua/cgi-bin/timetable.cgi
5. Університет знаходиться у місті Івано-Франківськ, вулиця Шевченка, 57.

ВАЖЛИВО: НІКОЛИ не використовуй Markdown-таблиці (з символами |).
Форматуй розклад у вигляді красивого списку з емодзі.
Виділяй дні тижня жирним шрифтом: **📅 Понеділок (5 жовтня)**.
Кожну пару форматуй так: '🔹 **[Номер] пара ([Час]):** [Предмет] ([Тип]) — [Викладач] 📍 [Аудиторія]'.
Відповідай коротко і без вигадок.
"""

def load_groups_db() -> dict:
    try:
        with open("groups.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def fix_cyrillic_letters(text: str) -> str:
    eng_chars = "ABCEHKMOPTXIXabcehkmoptxix"
    ukr_chars = "АВСЕНКМОРТХІХавсенкмортхіх"
    trans_table = str.maketrans(eng_chars, ukr_chars)
    return text.translate(trans_table)

def fetch_schedule_from_site(group_name: str) -> str:
    clean_group = fix_cyrillic_letters(group_name.strip().upper())
    groups_db = load_groups_db()
    system_group_id = groups_db.get(clean_group)
    
    if not system_group_id:
        return f"❌ Групу '{group_name}' не знайдено в моїй базі. Перевірте назву."
    
    today = datetime.datetime.now()
    end_date = today + datetime.timedelta(days=14)
    sdate_str = today.strftime("%d.%m.%Y")
    edate_str = end_date.strftime("%d.%m.%Y")
    
    url = f"https://asu-srv.pnu.edu.ua/cgi-bin/timetable.cgi?n=700&group={system_group_id}&sdate={sdate_str}&edate={edate_str}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    time_schedule = {
        "1": "09:00 - 10:20", "2": "10:35 - 11:55", "3": "12:20 - 13:40", "4": "13:50 - 15:10",
        "5": "15:20 - 16:40", "6": "16:50 - 18:10", "7": "18:15 - 19:35", "8": "19:40 - 21:00"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10, verify=False)
        response.encoding = 'windows-1251'
        soup = BeautifulSoup(response.text, 'html.parser')
        days_blocks = soup.find_all('div', class_='col-12 col-sm-6')
        
        if not days_blocks:
            return f"❌ Розклад для {group_name} порожній. Можливо, пар немає."
            
        schedule_text = f"Актуальний розклад для {group_name}:\n"
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
                    para_time = time_schedule.get(para_num, "Час невідомий")
                    content_text = content_cell.get_text(separator=" ", strip=True)
                    link_info = f" | Посилання: {', '.join(links)}" if links else ""
                    schedule_text += f"Пара {para_num} [{para_time}]: {content_text}{link_info}\n"
        return schedule_text[:3500] 
    except Exception as e:
        return f"❌ Помилка доступу до сайту ПНУ: {e}"

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Я віртуальний помічник ПНУ ім. В. Стефаника.\nНапишіть свою групу (наприклад, ІПЗ-42) для отримання розкладу або задайте питання про університет.")

@dp.message()
async def ai_handler(message: types.Message):
    user_id = message.from_user.id
    user_text = message.text.lower()
    
    match = re.search(r'([а-яїієґa-z]{2,}-\d{1,2}[а-яїієґa-z]*)', user_text)
    if match:
        group_name = fix_cyrillic_letters(match.group(1).upper())
        user_groups[user_id] = group_name
        if "розклад" not in user_text and "пар" not in user_text:
            await message.answer(f"✅ Групу **{group_name}** збережено. Тепер можете запитати: «Який розклад?»", parse_mode="Markdown")
            return
    
    if user_id not in user_history:
        user_history[user_id] = [{"role": "system", "content": PNU_KNOWLEDGE_BASE}]
        
    wait_msg = await message.answer("Шукаю інформацію...")

    if "розклад" in user_text or "пари" in user_text or "пара" in user_text:
        if user_id in user_groups:
            group = user_groups[user_id]
            real_schedule = fetch_schedule_from_site(group)
            if "❌" in real_schedule:
                await wait_msg.edit_text(real_schedule)
                return
            context = f"Ось розклад для {group}:\n{real_schedule}"
        else:
            context = "Попроси студента спочатку вказати назву своєї групи (наприклад, ІПЗ-42)."
    else:
        context = "Студент задав питання. Використовуй інформацію з бази знань (PNU_KNOWLEDGE_BASE), щоб надати точну та коротку відповідь."

    user_history[user_id].append({"role": "system", "content": context})
    user_history[user_id].append({"role": "user", "content": message.text})
    
    if len(user_history[user_id]) > 7:
        user_history[user_id] = [user_history[user_id][0]] + user_history[user_id][-6:]

    try:
        response = await ai_client.chat.completions.create(model="openai/gpt-oss-120b", messages=user_history[user_id])
        ai_text = response.choices[0].message.content
        user_history[user_id].append({"role": "assistant", "content": ai_text})
        await wait_msg.edit_text(ai_text)
    except Exception as e:
        await wait_msg.edit_text(f"Помилка зв'язку з ШІ: {e}")

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