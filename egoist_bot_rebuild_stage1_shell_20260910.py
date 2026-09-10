
import asyncio, os, sqlite3
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from geopy.geocoders import Nominatim
from egoist_astro_engine_20260910 import chart
from egoist_interpretation_nodes_v3_20260910 import free_me, preview, deep, child_report, karmic_nodes_report, karmic_nodes_preview, technical

router=Router()
geo=Nominatim(user_agent="astroego_complete")
PROFILES={}

# Purchases must survive bot restarts. On Railway, mount a persistent volume at /data.
DB_PATH = os.getenv("EGOIST_DB_PATH") or ("/data/egoist.sqlite3" if os.path.isdir("/data") else "egoist.sqlite3")

def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS purchases (
        user_id INTEGER NOT NULL,
        section TEXT NOT NULL,
        purchased_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        telegram_charge_id TEXT,
        provider_charge_id TEXT,
        PRIMARY KEY (user_id, section)
    )""")
    return conn

def has_unlock(user_id, section):
    with _db() as conn:
        row = conn.execute("SELECT 1 FROM purchases WHERE user_id=? AND section=?", (int(user_id), section)).fetchone()
    return bool(row)

def save_unlock(user_id, section, payment=None):
    telegram_charge_id = getattr(payment, "telegram_payment_charge_id", None) if payment else None
    provider_charge_id = getattr(payment, "provider_payment_charge_id", None) if payment else None
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO purchases(user_id, section, telegram_charge_id, provider_charge_id) VALUES(?,?,?,?)",
            (int(user_id), section, telegram_charge_id, provider_charge_id),
        )

# Initialize DB at startup/import time so configuration errors fail visibly.
with _db():
    pass
PRICES={
    "who":600,
    "love":600,
    "career_money":800,
    "nodes":600,
    "growth":600,
    "child":900,
}

LABEL={
    "who":"✦ Кто я",
    "love":"❤ Всё о любви",
    "career_money":"✦ Таланты, карьера и деньги",
    "nodes":"☊ Кармические узлы",
    "growth":"✦ Точки роста",
    "child":"🌱 Потенциал ребёнка",
}

# Старые покупки не удаляются из БД.
# На следующем этапе отдельно решим, как переносить старые unlock-ключи
# love/talent/career/money/change/nodes в новую архитектуру.


class Birth(StatesGroup): date=State(); time=State(); city=State()
class Child(StatesGroup): date=State(); time=State(); city=State()

def menu():
    rows=[
      [InlineKeyboardButton(text="✦ Кто я",callback_data="sec:who")],
      [InlineKeyboardButton(text="❤ Всё о любви",callback_data="sec:love")],
      [InlineKeyboardButton(text="✦ Таланты, карьера и деньги",callback_data="sec:career_money")],
      [InlineKeyboardButton(text="☊ Кармические узлы",callback_data="sec:nodes")],
      [InlineKeyboardButton(text="✦ Точки роста",callback_data="sec:growth")],
      [InlineKeyboardButton(text="🌱 Потенциал ребёнка",callback_data="sec:child")],
      [InlineKeyboardButton(text="✨ Личный разбор с автором EGOIST",callback_data="author:menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def author_topics_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text="Разбор вашего «Я»",callback_data="author:me")],
      [InlineKeyboardButton(text="Кармические задачи",callback_data="author:karma")],
      [InlineKeyboardButton(text="Проф. реализация и финансы",callback_data="author:career")],
      [InlineKeyboardButton(text="Отношения",callback_data="author:love")],
      [InlineKeyboardButton(text="Как получать от жизни удовольствие",callback_data="author:venus")],
      [InlineKeyboardButton(text="Соляр на год",callback_data="author:solar")],
      [InlineKeyboardButton(text="Детский гороскоп",callback_data="author:child")],
      [InlineKeyboardButton(text="Другое",callback_data="author:other")],
      [InlineKeyboardButton(text="Полный разбор карты по всем блокам",callback_data="author:full")],
      [InlineKeyboardButton(text="← Назад",callback_data="menu")]
    ])

AUTHOR_TOPICS={
    "me": "Разбор вашего «Я»: ваши сильные и слабые стороны, с чем вы пришли в эту жизнь и главное - зачем",
    "karma": "Кармические задачи на это воплощение",
    "career": "Профессиональная реализация и финансы: где ваши деньги и через что они приходят",
    "love": "Отношения: кого вы притягиваете или отталкиваете и почему так происходит",
    "venus": "Как понять себя и начать получать от жизни удовольствие: чего вы на самом деле хотите и что мешает вам это получать",
    "solar": "Соляр на год",
    "child": "Детский гороскоп для выбора профессии, сильных направлений и ориентиров в будущем",
    "other": "Другой личный запрос",
    "full": "Полный разбор карты по всем блокам",
}

def author_contact_kb():
    username=os.getenv("AUTHOR_TELEGRAM","").strip().lstrip("@")
    rows=[]
    if username:
        rows.append([InlineKeyboardButton(text="Написать автору EGOIST",url=f"https://t.me/{username}")])
    rows.append([InlineKeyboardButton(text="← К направлениям",callback_data="author:menu")])
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


# ---------------------------------------------------------------------
# STAGE 1: новая архитектура разделов.
# Пока подключаем существующую рабочую интерпретацию как временный мост.
# На следующих этапах эти функции будут заменены новыми 5 блоками.
# ---------------------------------------------------------------------

def new_preview(c, sec):
    if sec == "who":
        return free_me(c)
    if sec == "love":
        return preview(c, "love")
    if sec == "career_money":
        # Временный мост. Финальная версия будет единым новым синтезом.
        return preview(c, "talent") + "\n\n" + preview(c, "career")
    if sec == "nodes":
        return karmic_nodes_preview(c)
    if sec == "growth":
        return preview(c, "change")
    raise KeyError(sec)


def new_deep(c, sec):
    if sec == "who":
        # Временный мост: бесплатное ядро остаётся рабочим.
        # Платная часть "Как раскрыть себя сильнее" будет подключена на этапе интерпретаций.
        return free_me(c)
    if sec == "love":
        return deep(c, "love")
    if sec == "career_money":
        return deep(c, "career") + "\n\n" + deep(c, "money")
    if sec == "nodes":
        return karmic_nodes_report(c, child=False)
    if sec == "growth":
        return deep(c, "change")
    raise KeyError(sec)


@router.message(CommandStart())
async def start(m:Message,state:FSMContext):
    await state.clear()
    await m.answer("ЭГОИСТ\nЗдесь всё о тебе.\n\nНачнём с главного — кто ты?\n\nВведи дату рождения в формате ДД.ММ.ГГГГ:")
    await state.set_state(Birth.date)


@router.message(Command("menu"))
async def command_menu(m:Message,state:FSMContext):
    await state.clear()
    await m.answer("Куда посмотрим дальше?", reply_markup=menu())

@router.message(Command("author"))
async def command_author(m:Message):
    text=(
        "✨ Личный разбор с астрологом и автором EGOIST\n\n"
        "Если вам хочется пойти глубже и получить персональный разбор своей карты, "
        "вы можете обратиться напрямую к астрологу и автору EGOIST.\n\n"
        "Выберите тему, которая вам сейчас наиболее важна:"
    )
    await m.answer(text, reply_markup=author_topics_kb())

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
        await state.clear(); await send_long(m.answer, new_preview(c, "who"), reply_markup=menu())
    except Exception as e: await m.answer(f"Не получилось построить карту: {e}")

@router.callback_query(F.data=="menu")
async def back(q:CallbackQuery):
    await q.message.answer("Куда посмотрим дальше?",reply_markup=menu()); await q.answer()

@router.callback_query(F.data.startswith("sec:"))
async def section(q:CallbackQuery,state:FSMContext):
    sec=q.data.split(":",1)[1]
    p=PROFILES.get(q.from_user.id)

    if not p:
        await q.message.answer("Сначала нажми /start и введи данные рождения.")
        await q.answer()
        return

    if sec=="child":
        await q.message.answer("🌱 Карта потенциала ребёнка\n\nВведи дату рождения ребёнка ДД.ММ.ГГГГ:")
        await state.set_state(Child.date)
        await q.answer()
        return

    if sec not in PRICES:
        await q.answer("Раздел не найден.", show_alert=True)
        return

    if has_unlock(q.from_user.id, sec):
        await send_long(q.message.answer, new_deep(p["chart"],sec), reply_markup=menu())
    else:
        await send_long(q.message.answer, new_preview(p["chart"],sec), reply_markup=unlock_kb(sec))

    await q.answer()


@router.callback_query(F.data=="author:menu")
async def author_menu(q:CallbackQuery):
    text=(
        "✨ Личный разбор с астрологом и автором EGOIST\n\n"
        "Если вам хочется пойти глубже и получить персональный разбор своей карты, "
        "вы можете обратиться напрямую к астрологу и автору EGOIST.\n\n"
        "Выберите тему, которая вам сейчас наиболее важна:"
    )
    await q.message.answer(text,reply_markup=author_topics_kb())
    await q.answer()

@router.callback_query(F.data.startswith("author:"))
async def author_topic(q:CallbackQuery):
    topic=q.data.split(":",1)[1]
    if topic=="menu":
        await q.answer()
        return
    label=AUTHOR_TOPICS.get(topic)
    if not label:
        await q.answer()
        return
    username=os.getenv("AUTHOR_TELEGRAM","").strip().lstrip("@")
    text=f"Вы выбрали:\n\n{label}\n\n"
    if username:
        text+="Нажмите кнопку ниже, чтобы перейти в личный диалог с автором EGOIST."
    else:
        text+="Контакт автора пока не подключён."
    await q.message.answer(text,reply_markup=author_contact_kb())
    await q.answer()

@router.callback_query(F.data.startswith("buy:"))
async def buy(q:CallbackQuery,bot:Bot):
    sec=q.data.split(":")[1]
    if has_unlock(q.from_user.id, sec):
        await q.answer("Этот раздел уже куплен — повторно платить не нужно ✨", show_alert=True)
        return
    await bot.send_invoice(chat_id=q.from_user.id,title=LABEL[sec],description="Персональный разбор по твоей карте",payload=f"unlock:{sec}",currency="XTR",prices=[LabeledPrice(label=LABEL[sec],amount=PRICES[sec])])
    await q.answer()

@router.pre_checkout_query()
async def precheckout(q:PreCheckoutQuery):
    try:
        parts = (q.invoice_payload or "").split(":", 1)
        sec = parts[1] if len(parts) == 2 and parts[0] == "unlock" else None
        if sec not in PRICES:
            await q.answer(ok=False, error_message="Не удалось определить раздел покупки.")
            return
        if has_unlock(q.from_user.id, sec):
            await q.answer(ok=False, error_message="Этот раздел уже куплен. Повторная оплата не требуется.")
            return
        await q.answer(ok=True)
    except Exception:
        await q.answer(ok=False, error_message="Не удалось проверить покупку. Попробуйте ещё раз.")

@router.message(F.successful_payment)
async def paid(m:Message,state:FSMContext):
    payload=(m.successful_payment.invoice_payload or "")
    parts=payload.split(":",1)
    sec=parts[1] if len(parts)==2 and parts[0]=="unlock" else None

    if sec not in PRICES:
        await m.answer("Оплата прошла, но раздел не удалось определить. Напиши автору EGOIST.")
        return

    save_unlock(m.from_user.id, sec, m.successful_payment)
    p=PROFILES.get(m.from_user.id)
    await m.answer("Готово. Разбор открыт ✨")

    if sec=="child":
        cc=p.get("child_chart") if p else None
        if cc:
            await send_long(m.answer, child_report(cc,full=True), reply_markup=menu())
        else:
            await m.answer("Введи дату рождения ребёнка ДД.ММ.ГГГГ:")
            await state.set_state(Child.date)
        return

    if not p:
        await m.answer("Теперь нажми /start и введи данные рождения, чтобы открыть разбор.")
        return

    await send_long(m.answer, new_deep(p["chart"],sec), reply_markup=menu())


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
        unlocked=has_unlock(m.from_user.id, "child")
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
    raw_token = os.getenv("BOT_TOKEN", "")
    # Railway/mobile copy-paste can leave spaces, line breaks, or wrapping quotes.
    # Telegram bot tokens never contain whitespace, so normalize safely before aiogram validation.
    token = "".join(raw_token.split()).strip("\"'")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан.")
    if ":" not in token:
        raise RuntimeError(
            f"BOT_TOKEN имеет неверный формат после очистки: длина={len(token)}, двоеточий={token.count(':')}"
        )
    bot=Bot(token); dp=Dispatcher(storage=MemoryStorage()); dp.include_router(router)
    await dp.start_polling(bot)

if __name__=="__main__": asyncio.run(main())
