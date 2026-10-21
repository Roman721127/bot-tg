import os
import urllib.parse
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
from duckduckgo_search import DDGS

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

def fix_cyrillic_letters(text: str) -> str:
    eng_chars = "ABCEHKMOPTXIXabcehkmoptxix"
    ukr_chars = "АВСЕНКМОРТХІХавсенкмортхіх"
    trans_table = str.maketrans(eng_chars, ukr_chars)
    return text.translate(trans_table)

def search_web(query: str) -> str:
    try:
        search_query = f"{query} Прикарпатський національний університет імені Василя Стефаника"
        with DDGS() as ddgs:
            results = list(ddgs.text(search_query, region='ua-uk', max_results=3))
            if not results:
                return "В інтернеті не знайдено інформації за цим запитом."
            context = "Знайдено в інтернеті:\n\n"
            for r in results:
                context += f"Джерело: {r['title']}\nТекст: {r['body']}\n\n"
            return context
    except Exception as e:
        return f"Помилка вебпошуку: {e}"

def fetch_group_id_from_site(group_name: str):
    clean_group = fix_cyrillic_letters(group_name.strip().upper())
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://asu-srv.pnu.edu.ua/cgi-bin/timetable.cgi"
    }
    url = "https://asu-srv.pnu.edu.ua/cgi-bin/timetable.cgi"
    search_variants = [
        {"n": "701", "faculty": "0", "query": clean_group.encode('windows-1251')},
        {"n": "701", "lev": "142", "faculty": "0", "query": clean_group.encode('windows-1251')},
        {"n": "701", "query": clean_group.encode('windows-1251')}
    ]
    for params in search_variants:
        try:
            response = requests.get(url, params=params, headers=headers, timeout=5, verify=False)
            response.encoding = 'windows-1251'
            data = response.json()
            if "suggestions" in data and "data" in data:
                for i, suggestion in enumerate(data["suggestions"]):
                    if suggestion.lower().replace(" ", "") == clean_group.lower().replace(" ", ""):
                        return data["data"][i]
                for i, suggestion in enumerate(data["suggestions"]):
                    if clean_group.lower().replace(" ", "") in suggestion.lower().replace(" ", ""):
                        return data["data"][i]
                if data["data"]:
                    return data["data"][0]
        except Exception:
            continue
    return None

def fetch_schedule_from_site(group_name: str) -> str:
    clean_group = fix_cyrillic_letters(group_name.strip().upper())
    system_group_id = fetch_group_id_from_site(clean_group)
    
    if not system_group_id:
        return f"❌ Не вдалося знайти групу '{group_name}' в базі ПНУ."
    
    today = datetime.datetime.now()
    end_date = today + datetime.timedelta(days=14)
    sdate_str = today.strftime("%d.%m.%Y")
    edate_str = end_date.strftime("%d.%m.%Y")
    
    url = "https://asu-srv.pnu.edu.ua/cgi-bin/timetable.cgi"
    params = {"n": "700", "group": system_group_id, "sdate": sdate_str, "edate": edate_str}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    time_schedule = {
        "1": "09:00 - 10:20", "2": "10:35 - 11:55", "3": "12:20 - 13:40", "4": "13:50 - 15:10",
        "5": "15:20 - 16:40", "6": "16:50 - 18:10", "7": "18:15 - 19:35", "8": "19:40 - 21:00"
    }
    
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10, verify=False)
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
        return f"❌ Помилка доступу до бази ПНУ: {e}"

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Я розумний помічник ПНУ ім. В. Стефаника. Напишіть свою групу для розкладу або задайте будь-яке питання про університет, і я пошукаю це в інтернеті!")

@dp.message()
async def ai_handler(message: types.Message):
    user_id = message.from_user.id
    user_text = message.text.lower()
    
    match = re.search(r'([а-яїієґa-z]{2,}-\d{1,2}[а-яїієґa-z]*)', user_text)
    if match:
        group_name = fix_cyrillic_letters(match.group(1).upper())
        user_groups[user_id] = group_name
        if "розклад" not in user_text and "пар" not in user_text:
            await message.answer(f"✅ Групу **{group_name}** збережено. Запитай: «Який розклад на завтра?»", parse_mode="Markdown")
            return
    
    if user_id not in user_history:
        user_history[user_id] = [{"role": "system", "content": "Ти помічник Прикарпатського національного університету (ПНУ). Відповідай правдиво українською."}]
        
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
            context = "Попроси студента спочатку вказати групу."
    else:
        web_data = search_web(message.text)
        context = f"Студент запитав загальне питання. Ти ПРОГУГЛИВ це і знайшов таке:\n{web_data}\n\nДай точну відповідь на основі цих статей. Якщо інформації там немає, скажи, що не зміг знайти в інтернеті."

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