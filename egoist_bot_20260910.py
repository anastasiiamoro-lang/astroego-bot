
import asyncio, os
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from geopy.geocoders import Nominatim
from egoist_astro_engine_20260910 import chart, solar_return
from egoist_interpretation_20260910 import free_me, preview, deep, current_report, solar_report, child_report, karmic_nodes_report, technical

router=Router()
geo=Nominatim(user_agent="astroego_complete")
PROFILES={}
UNLOCKS={}
PRICES={"love":250,"want":200,"talent":250,"career":350,"money":300,"change":300,"solar":600,"now":200,"child":450}
LABEL={"love":"❤️ Как я люблю","want":"🔥 Чего я хочу","talent":"✨ В чём мой талант","career":"💼 В чём моё дело","money":"💰 Как я зарабатываю","change":"🖤 Точки роста","solar":"☀️ Каким будет мой год","now":"🕰 Что со мной сейчас","child":"🌱 Потенциал ребёнка"}

class Birth(StatesGroup): date=State(); time=State(); city=State()
class Solar(StatesGroup): city=State()
class Child(StatesGroup): date=State(); time=State(); city=State()

def menu():
    rows=[
      [InlineKeyboardButton(text="Я",callback_data="me")],
      [InlineKeyboardButton(text="❤️ Люблю",callback_data="sec:love"),InlineKeyboardButton(text="🔥 Хочу",callback_data="sec:want")],
      [InlineKeyboardButton(text="✨ Могу",callback_data="sec:talent"),InlineKeyboardButton(text="💼 Делаю",callback_data="sec:career")],
      [InlineKeyboardButton(text="💰 Имею",callback_data="sec:money"),InlineKeyboardButton(text="🖤 Точки роста",callback_data="sec:change")],
      [InlineKeyboardButton(text="☀️ Мой год",callback_data="sec:solar"),InlineKeyboardButton(text="🕰 Сейчас",callback_data="sec:now")],
      [InlineKeyboardButton(text="☊ Кармические узлы",callback_data="nodes")],
      [InlineKeyboardButton(text="🌱 Ребёнок",callback_data="sec:child")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def unlock_kb(sec):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"Открыть полный разбор · ⭐ {PRICES[sec]}",callback_data=f"buy:{sec}")],[InlineKeyboardButton(text="← Назад",callback_data="menu")]])


def split_telegram_text(text, limit=3900):
    """Split long Telegram text safely, preferring paragraph boundaries."""
    text = (text or "").strip()
    if not text:
        return [""]
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


async def send_long(answer_func, text, reply_markup=None):
    """Send a report in several Telegram messages; keyboard goes under the last part."""
    parts = split_telegram_text(text)
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        await answer_func(part, reply_markup=markup)

async def locate(city):
    loc=await asyncio.to_thread(geo.geocode,city,language="ru",exactly_one=True)
    if not loc: raise ValueError("Не нашла город. Напиши город и страну.")
    return float(loc.latitude),float(loc.longitude)

@router.message(CommandStart())
async def start(m:Message,state:FSMContext):
    await state.clear()
    await m.answer("ЭГОИСТ\nЗдесь всё о тебе.\n\nНачнём с главного — кто ты?\n\nВведи дату рождения в формате ДД.ММ.ГГГГ:")
    await state.set_state(Birth.date)

@router.message(Birth.date)
async def bdate(m:Message,state:FSMContext):
    await state.update_data(date=m.text.strip()); await m.answer("Точное время рождения, например 14:35:"); await state.set_state(Birth.time)

@router.message(Birth.time)
async def btime(m:Message,state:FSMContext):
    await state.update_data(time=m.text.strip()); await m.answer("Город рождения и страна, например: Париж, Франция"); await state.set_state(Birth.city)

@router.message(Birth.city)
async def bcity(m:Message,state:FSMContext):
    d=await state.get_data()
    try:
        lat,lon=await locate(m.text.strip()); c=chart(d["date"],d["time"],lat,lon)
        PROFILES[m.from_user.id]={"chart":c,"birth_city":m.text.strip()}
        await state.clear(); await send_long(m.answer, free_me(c), reply_markup=menu())
    except Exception as e: await m.answer(f"Не получилось построить карту: {e}")

@router.callback_query(F.data=="menu")
async def back(q:CallbackQuery):
    await q.message.answer("Куда посмотрим дальше?",reply_markup=menu()); await q.answer()

@router.callback_query(F.data=="me")
async def me(q:CallbackQuery):
    p=PROFILES.get(q.from_user.id)
    if not p: await q.message.answer("Сначала нажми /start и введи данные рождения.")
    else: await send_long(q.message.answer, free_me(p["chart"]), reply_markup=menu())
    await q.answer()

@router.callback_query(F.data=="nodes")
async def nodes(q:CallbackQuery):
    p=PROFILES.get(q.from_user.id)
    if not p:
        await q.message.answer("Сначала нажми /start и введи данные рождения.")
    else:
        await send_long(q.message.answer, karmic_nodes_report(p["chart"], child=False), reply_markup=menu())
    await q.answer()

@router.callback_query(F.data.startswith("sec:"))
async def section(q:CallbackQuery,state:FSMContext):
    sec=q.data.split(":")[1]; p=PROFILES.get(q.from_user.id)
    if not p: await q.message.answer("Сначала нажми /start и введи данные рождения."); await q.answer(); return
    if sec=="child":
        await q.message.answer("🌱 Карта потенциала ребёнка\n\nВведи дату рождения ребёнка ДД.ММ.ГГГГ:")
        await state.set_state(Child.date); await q.answer(); return
    if sec=="solar":
        if sec in UNLOCKS.get(q.from_user.id,set()):
            await q.message.answer("В каком городе ты будешь в день рождения? Напиши город и страну.")
            await state.set_state(Solar.city)
        else:
            await q.message.answer("☀️ Мой год\n\nСоляр строится на точный момент возвращения Солнца и на город, где ты проводишь день рождения. Он читается только вместе с натальной картой.\n\nВ полном разборе: главная тема года, отношения, работа и деньги, дом и семья, энергия, поездки, возможности и напряжённые зоны.",reply_markup=unlock_kb(sec))
        await q.answer(); return
    if sec=="now":
        try:
            now=datetime.now(); tc=chart(now.strftime("%d.%m.%Y"),now.strftime("%H:%M"),p["chart"]["lat"],p["chart"]["lon"])
            unlocked=sec in UNLOCKS.get(q.from_user.id,set())
            await send_long(q.message.answer, current_report(p["chart"],tc,full=unlocked), reply_markup=menu() if unlocked else unlock_kb(sec))
        except Exception as e:
            await q.message.answer(f"Не получилось рассчитать текущие транзиты: {e}", reply_markup=menu())
        await q.answer(); return
    if sec in UNLOCKS.get(q.from_user.id,set()):
        await send_long(q.message.answer, deep(p["chart"],sec), reply_markup=menu())
    else:
        await send_long(q.message.answer, preview(p["chart"],sec), reply_markup=unlock_kb(sec))
    await q.answer()

@router.callback_query(F.data.startswith("buy:"))
async def buy(q:CallbackQuery,bot:Bot):
    sec=q.data.split(":")[1]
    await bot.send_invoice(chat_id=q.from_user.id,title=LABEL[sec],description="Персональный разбор по твоей карте",payload=f"unlock:{sec}",currency="XTR",prices=[LabeledPrice(label=LABEL[sec],amount=PRICES[sec])])
    await q.answer()

@router.pre_checkout_query()
async def precheckout(q:PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def paid(m:Message,state:FSMContext):
    sec=m.successful_payment.invoice_payload.split(":")[1]
    UNLOCKS.setdefault(m.from_user.id,set()).add(sec)
    p=PROFILES.get(m.from_user.id)
    await m.answer("Готово. Разбор открыт ✨")
    if sec=="solar":
        await m.answer("В каком городе ты будешь в день рождения? Напиши город и страну."); await state.set_state(Solar.city)
    elif sec=="child":
        cc=p.get("child_chart") if p else None
        if cc:
            await send_long(m.answer, child_report(cc,full=True), reply_markup=menu())
        else:
            await m.answer("Введи дату рождения ребёнка ДД.ММ.ГГГГ:"); await state.set_state(Child.date)
    elif sec=="now":
        try:
            now=datetime.now(); tc=chart(now.strftime("%d.%m.%Y"),now.strftime("%H:%M"),p["chart"]["lat"],p["chart"]["lon"])
            await send_long(m.answer, current_report(p["chart"],tc,full=True), reply_markup=menu())
        except Exception as e:
            await m.answer(f"Не получилось рассчитать текущие транзиты: {e}", reply_markup=menu())
    else: await send_long(m.answer, deep(p["chart"],sec), reply_markup=menu())

@router.message(Solar.city)
async def solar_city(m:Message,state:FSMContext):
    p=PROFILES[m.from_user.id]
    try:
        lat,lon=await locate(m.text.strip())
        birth=datetime.strptime(p["chart"]["date_str"],"%d.%m.%Y")
        today=datetime.now()
        year=today.year if (today.month,today.day)<=(birth.month,birth.day) else today.year+1
        sr=solar_return(p["chart"],year,lat,lon)
        await state.clear(); await send_long(m.answer, solar_report(p["chart"],sr), reply_markup=menu())
    except Exception as e: await m.answer(f"Не получилось рассчитать соляр: {e}")

@router.message(Child.date)
async def cdate(m:Message,state:FSMContext):
    await state.update_data(cdate=m.text.strip()); await m.answer("Точное время рождения ребёнка:"); await state.set_state(Child.time)
@router.message(Child.time)
async def ctime(m:Message,state:FSMContext):
    await state.update_data(ctime=m.text.strip()); await m.answer("Город рождения ребёнка и страна:"); await state.set_state(Child.city)
@router.message(Child.city)
async def ccity(m:Message,state:FSMContext):
    d=await state.get_data()
    try:
        lat,lon=await locate(m.text.strip()); c=chart(d["cdate"],d["ctime"],lat,lon)
        PROFILES.setdefault(m.from_user.id,{})["child_chart"]=c
        unlocked="child" in UNLOCKS.get(m.from_user.id,set())
        await state.clear()
        await send_long(m.answer, child_report(c,full=unlocked), reply_markup=menu() if unlocked else unlock_kb("child"))
    except Exception as e: await m.answer(f"Не получилось построить карту: {e}")

@router.message(Command("tech"))
async def tech(m:Message):
    admin=os.getenv("ADMIN_ID")
    if not admin or str(m.from_user.id)!=str(admin): return
    p=PROFILES.get(m.from_user.id)
    if p: await send_long(m.answer, technical(p["chart"]))

async def main():
    token=os.getenv("BOT_TOKEN")
    if not token: raise RuntimeError("BOT_TOKEN не задан.")
    bot=Bot(token); dp=Dispatcher(storage=MemoryStorage()); dp.include_router(router)
    await dp.start_polling(bot)

if __name__=="__main__": asyncio.run(main())
