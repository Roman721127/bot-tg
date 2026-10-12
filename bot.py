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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

ai_client = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

# Словники для збереження контексту та груп
user_history = {}
user_groups = {} 

def fetch_group_id_from_site(group_name: str):
    """
    Робить запит до прихованого API сайту ПНУ для пошуку внутрішнього ID групи.
    """
    try:
        # ВАЖЛИВО: Кодуємо назву у windows-1251 для старого сайту ПНУ
        encoded_group = group_name.encode('windows-1251')
        query = urllib.parse.quote(encoded_group)
        
        # Прибрали lev=142, щоб шукало по всіх факультетах і рівнях
        search_url = f"https://asu-srv.pnu.edu.ua/cgi-bin/timetable.cgi?n=701&faculty=0&query={query}"
        
        response = requests.get(search_url, timeout=5, verify=False)
        response.encoding = 'windows-1251'
        
        data = response.json()
        
        if "suggestions" in data and "data" in data:
            suggestions = data["suggestions"]
            ids = data["data"]
            
            for i, suggestion in enumerate(suggestions):
                if suggestion.lower().replace(" ", "") == group_name.lower().replace(" ", ""):
                    return ids[i]
            
            if ids:
                return ids[0]
                
    except Exception as e:
        print(f"Помилка динамічного пошуку групи: {e}")
        
    return None

def fetch_schedule_from_site(group_name: str) -> str:
    system_group_id = fetch_group_id_from_site(group_name)
    
    if not system_group_id:
        group_ids = {
            "іпз-42": "-5633",
            "іпз-41": "-5632", 
        }
        search_key = group_name.lower().replace(" ", "")
        system_group_id = group_ids.get(search_key)
        
        if not system_group_id:
            return f"❌ Не вдалося знайти групу '{group_name}' в базі ПНУ. Перевірте правильність назви (наприклад: КН-12)."
    
    today = datetime.datetime.now()
    end_date = today + datetime.timedelta(days=14)
    sdate_str = today.strftime("%d.%m.%Y")
    edate_str = end_date.strftime("%d.%m.%Y")
    
    url = f"https://asu-srv.pnu.edu.ua/cgi-bin/timetable.cgi?n=700&group={system_group_id}&sdate={sdate_str}&edate={edate_str}"
    
    time_schedule = {
        "1": "09:00 - 10:20",
        "2": "10:35 - 11:55",
        "3": "12:20 - 13:40",
        "4": "13:50 - 15:10",
        "5": "15:20 - 16:40",
        "6": "16:50 - 18:10",
        "7": "18:15 - 19:35",
        "8": "19:40 - 21:00"
    }
    
    try:
        response = requests.get(url, timeout=10, verify=False)
        response.encoding = 'windows-1251'
        
        soup = BeautifulSoup(response.text, 'html.parser')
        days_blocks = soup.find_all('div', class_='col-12 col-sm-6')
        
        if not days_blocks:
            return f"Не вдалося знайти розклад для {group_name}. Можливо, у цей період пар немає."
            
        schedule_text = f"Актуальний розклад для {group_name} ({sdate_str} - {edate_str}):\n"
        
        for day_block in days_blocks:
            date_header = day_block.find('h4')
            if not date_header:
                continue
            
            day_title = date_header.get_text(separator=" ", strip=True)
            schedule_text += f"\n--- {day_title} ---\n"
            
            rows = day_block.find_all('tr')
            for row in rows:
                cells = row.find_all('td')
                if len(cells) >= 3:
                    para_num = cells[0].get_text(strip=True)
                    content_cell = cells[2]
                    
                    if not content_cell.get_text(strip=True):
                        continue
                    
                    links = []
                    for a_tag in content_cell.find_all('a'):
                        href = a_tag.get('href')
                        if href:
                            links.append(href)
                    
                    para_time = time_schedule.get(para_num, "Час невідомий")
                    content_text = content_cell.get_text(separator=" ", strip=True)
                    
                    link_info = f" | Посилання: {', '.join(links)}" if links else ""
                    schedule_text += f"Пара {para_num} [{para_time}]: {content_text}{link_info}\n"
                    
        return schedule_text[:3500] 
        
    except Exception as e:
        return f"Помилка доступу до бази ПНУ: {e}"

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Я віртуальний помічник ПНУ ім. В. Стефаника.\n\nЩоб я міг показувати ваш розклад, напишіть мені свою групу, наприклад:\n**Група ІПЗ-42**\n\n*(Я підтримую будь-які групи та факультети!)*", parse_mode="Markdown")

@dp.message()
async def ai_handler(message: types.Message):
    user_id = message.from_user.id
    user_text = message.text.lower()
    
    # РОЗУМНИЙ ПОШУК ГРУПИ В ТЕКСТІ (Регулярний вираз)
    match = re.search(r'([а-яїієґa-z]{2,}-\d{1,2})', user_text)
    if match:
        group_name = match.group(1).upper()
        user_groups[user_id] = group_name
        
        # Якщо студент просто написав групу без слова "розклад", підтверджуємо і зупиняємось
        if "розклад" not in user_text and "пар" not in user_text:
            await message.answer(f"✅ Супер! Я запам'ятав вашу групу: **{group_name}**.\nТепер можете запитати: «Який розклад на завтра?»", parse_mode="Markdown")
            return
    
    if user_id not in user_history:
        user_history[user_id] = [
            {
                "role": "system", 
                "content": (
                    "Ти дружній віртуальний куратор ПНУ ім. В. Стефаника. "
                    "Твоя головна умова: завжди відповідай ВИКЛЮЧНО українською мовою. "
                    "ВАЖЛИВО: НІКОЛИ не використовуй Markdown-таблиці (з символами |). "
                    "Форматуй розклад у вигляді красивого списку з емодзі. "
                    "Виділяй дні тижня жирним шрифтом: **📅 Понеділок (5 жовтня)**. "
                    "Кожну пару форматуй так: '🔹 **[Номер] пара ([Час]):** [Предмет] ([Тип]) — [Викладач] 📍 [Аудиторія]'. "
                    "Якщо у вхідних даних є слово 'Посилання:', обов'язково роби його клікабельним форматом Markdown: '🔗 [Підключитися до пари](URL)'. "
                    "Роби порожні рядки (відступи) між різними днями тижня."
                )
            }
        ]
        
    wait_msg = await message.answer("Шукаю інформацію...")

    if "розклад" in user_text or "пари" in user_text or "пара" in user_text:
        if user_id in user_groups:
            group = user_groups[user_id]
            real_schedule = fetch_schedule_from_site(group)
            
            # Якщо група не знайдена, виводимо помилку ОДРАЗУ, щоб ШІ не заплутався
            if "❌" in real_schedule:
                await wait_msg.edit_text(real_schedule)
                return
            
            user_history[user_id].append({
                "role": "system", 
                "content": f"Ось актуальні дані про розклад для групи {group}:\n{real_schedule}\nВикористай їх для точної відповіді студенту."
            })
        else:
            user_history[user_id].append({
                "role": "system", 
                "content": "Студент хоче дізнатися розклад, але система ще не знає його групи. Ввічливо попроси його спочатку написати свою групу у форматі 'КН-12'."
            })

    user_history[user_id].append({"role": "user", "content": message.text})
    
    if len(user_history[user_id]) > 11:
        user_history[user_id] = [user_history[user_id][0]] + user_history[user_id][-10:]

    try:
        response = await ai_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=user_history[user_id]
        )
        
        ai_text = response.choices[0].message.content
        user_history[user_id].append({"role": "assistant", "content": ai_text})
        await wait_msg.edit_text(ai_text)
        
    except Exception as e:
        await wait_msg.edit_text(f"Виникла помилка під час зв'язку з ШІ: {e}")

async def handle_ping(request):
    return web.Response(text="Бот працює!")

async def main():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())