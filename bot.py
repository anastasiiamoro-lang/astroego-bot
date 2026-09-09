
import asyncio
import os
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from geopy.geocoders import Nominatim

from astro_engine import build_chart, format_degree, point_from_longitude, SIGNS

router = Router()
geocoder = Nominatim(user_agent="astro_bot_mvp")

class BirthForm(StatesGroup):
    date = State()
    time = State()
    city = State()

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✨ Пробный астрологический бот\n\n"
        "Я рассчитаю твою натальную Венеру: знак, точный градус, реальный дом Placidus, "
        "дома под её управлением и полнознаковые аспекты с оценкой точности.\n\n"
        "Введи дату рождения в формате ДД.ММ.ГГГГ:"
    )
    await state.set_state(BirthForm.date)

@router.message(BirthForm.date)
async def get_date(message: Message, state: FSMContext):
    await state.update_data(date=message.text.strip())
    await message.answer("Теперь точное время рождения, например 22:24:")
    await state.set_state(BirthForm.time)

@router.message(BirthForm.time)
async def get_time(message: Message, state: FSMContext):
    await state.update_data(time=message.text.strip())
    await message.answer("Город рождения, например: Усть-Каменогорск, Казахстан")
    await state.set_state(BirthForm.city)

@router.message(BirthForm.city)
async def get_city(message: Message, state: FSMContext):
    city = message.text.strip()
    data = await state.get_data()
    try:
        loc = geocoder.geocode(city, language="ru")
        if not loc:
            await message.answer("Не нашла этот город. Попробуй написать город + страну.")
            return
        chart = build_chart(
            data["date"], data["time"],
            float(loc.latitude), float(loc.longitude)
        )
        v = chart["venus"]
        asc = chart["asc"]
        mc = chart["mc"]

        cusps_txt = []
        for i, c in enumerate(chart["cusps"], 1):
            p = point_from_longitude("", c)
            cusps_txt.append(f"{i}: {p.sign} {format_degree(p.degree)}")

        intercepted = (
            ", ".join(f"{s} в {h} доме" for s, h in chart["intercepted"])
            if chart["intercepted"] else "нет"
        )

        text = (
            f"📍 {loc.address}\n"
            f"Часовой пояс: {chart['timezone']}\n\n"
            f"ASC: {asc.sign} {format_degree(asc.degree)}\n"
            f"MC: {mc.sign} {format_degree(mc.degree)}\n\n"
            f"♀ ВЕНЕРА\n"
            f"{chart['interpretation']}\n\n"
            f"Включённые знаки: {intercepted}\n\n"
            f"Технически рассчитанные куспиды Placidus:\n" +
            "\n".join(cusps_txt)
        )
        await message.answer(text)
        await state.clear()
    except Exception as e:
        await message.answer(
            "Не получилось рассчитать карту. Проверь формат даты, времени и города.\n\n"
            f"Техническая ошибка: {e}"
        )

async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Добавьте BOT_TOKEN в переменную окружения.")
    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
