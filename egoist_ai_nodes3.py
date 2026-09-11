
import asyncio, os, sqlite3, re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
import swisseph as swe
from timezonefinder import TimezoneFinder
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from geopy.geocoders import Nominatim
from openai import AsyncOpenAI

# =====================================================================
# ВСТРОЕННЫЙ АСТРОЛОГИЧЕСКИЙ ДВИЖОК
# Swiss Ephemeris + Placidus + исторический часовой пояс + узлы
# =====================================================================


SIGNS = [
    "Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
    "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"
]

PLANETS = {
    "Солнце": swe.SUN,
    "Луна": swe.MOON,
    "Меркурий": swe.MERCURY,
    "Венера": swe.VENUS,
    "Марс": swe.MARS,
    "Юпитер": swe.JUPITER,
    "Сатурн": swe.SATURN,
    "Уран": swe.URANUS,
    "Нептун": swe.NEPTUNE,
    "Плутон": swe.PLUTO,
}

OUTER = {"Уран", "Нептун", "Плутон"}

WHOLE_SIGN_ASPECTS = {
    0: "соединение",
    2: "секстиль",
    3: "квадрат",
    4: "тригон",
    6: "оппозиция",
    8: "тригон",
    9: "квадрат",
    10: "секстиль",
}

EXACT_ANGLES = {
    "соединение": 0,
    "секстиль": 60,
    "квадрат": 90,
    "тригон": 120,
    "оппозиция": 180,
}

# Первый управитель - основной в текущем MVP.
# Для Скорпиона, Водолея и Рыб сохраняем традиционного соуправителя вторым.
ENGINE_RULERS = {
    0: ["Марс"],
    1: ["Венера"],
    2: ["Меркурий"],
    3: ["Луна"],
    4: ["Солнце"],
    5: ["Меркурий"],
    6: ["Венера"],
    7: ["Плутон", "Марс"],
    8: ["Юпитер"],
    9: ["Сатурн"],
    10: ["Уран", "Сатурн"],
    11: ["Нептун", "Юпитер"],
}


@dataclass
class Point:
    name: str
    longitude: float
    sign_index: int
    sign: str
    degree: float


def normalize(x):
    return x % 360.0


def point_from_longitude(name, lon):
    lon = normalize(lon)
    sign_index = int(lon // 30)
    return Point(
        name=name,
        longitude=lon,
        sign_index=sign_index,
        sign=SIGNS[sign_index],
        degree=lon % 30,
    )


def format_degree(deg):
    d = int(deg)
    m_float = (deg - d) * 60
    m = int(m_float)
    s = int(round((m_float - m) * 60))
    if s == 60:
        s = 0
        m += 1
    if m == 60:
        m = 0
        d += 1
    return f"{d}°{m:02d}′"


def utc_from_local(date_str, time_str, lat, lon):
    tf = TimezoneFinder()
    tz_name = tf.timezone_at(lat=lat, lng=lon)
    if not tz_name:
        raise ValueError("не удалось определить часовой пояс места рождения")

    try:
        local_dt = datetime.strptime(
            f"{date_str} {time_str}", "%d.%m.%Y %H:%M"
        )
    except ValueError:
        raise ValueError("формат должен быть ДД.ММ.ГГГГ и ЧЧ:ММ")

    local_dt = local_dt.replace(tzinfo=ZoneInfo(tz_name))
    return local_dt.astimezone(ZoneInfo("UTC")), tz_name


def julian_day(utc_dt):
    hour = utc_dt.hour + utc_dt.minute / 60 + utc_dt.second / 3600
    return swe.julday(utc_dt.year, utc_dt.month, utc_dt.day, hour)


def calculate_planets(jd_ut):
    result = {}
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED
    for name, planet_id in PLANETS.items():
        xx, _ = swe.calc_ut(jd_ut, planet_id, flags)
        p = point_from_longitude(name, xx[0])
        p.speed = xx[3]
        result[name] = p
    return result


def calculate_placidus_houses(jd_ut, lat, lon):
    cusps, ascmc = swe.houses(jd_ut, lat, lon, b"P")
    cusp_lons = [normalize(float(c)) for c in cusps[:12]]
    asc = point_from_longitude("ASC", ascmc[0])
    mc = point_from_longitude("MC", ascmc[1])
    return cusp_lons, asc, mc


def in_arc(x, start, end):
    x, start, end = normalize(x), normalize(start), normalize(end)
    if start <= end:
        return start <= x < end
    return x >= start or x < end


def house_of_longitude(lon, cusps):
    for i in range(12):
        if in_arc(lon, cusps[i], cusps[(i + 1) % 12]):
            return i + 1
    raise RuntimeError("не удалось определить дом")


def cusp_sign(cusp_lon):
    return int(normalize(cusp_lon) // 30)


def intercepted_signs(cusps):
    used = {cusp_sign(c) for c in cusps}
    intercepted = []
    for sign_index in range(12):
        if sign_index not in used:
            midpoint = sign_index * 30 + 15
            house = house_of_longitude(midpoint, cusps)
            intercepted.append((SIGNS[sign_index], house))
    return intercepted


def angular_distance(a, b):
    d = abs(normalize(a) - normalize(b))
    return min(d, 360 - d)


def exact_orb(a_lon, b_lon, aspect_name):
    return abs(angular_distance(a_lon, b_lon) - EXACT_ANGLES[aspect_name])


def whole_sign_aspect(a, b):
    diff = (b.sign_index - a.sign_index) % 12
    return WHOLE_SIGN_ASPECTS.get(diff)


def strength_label(other_name, orb):
    if orb <= 1:
        return "доминирующий"
    if other_name in OUTER and orb <= 2:
        return "очень сильный"
    if orb <= 3:
        return "сильный"
    if orb <= 6:
        return "заметный"
    return "фоновый"


def strength_rank(label):
    return {
        "доминирующий": 0,
        "очень сильный": 1,
        "сильный": 2,
        "заметный": 3,
        "фоновый": 4,
    }[label]


def aspects_for(point_name, planets):
    point = planets[point_name]
    result = []
    for other_name, other in planets.items():
        if other_name == point_name:
            continue
        aspect = whole_sign_aspect(point, other)
        if not aspect:
            continue
        orb = exact_orb(point.longitude, other.longitude, aspect)
        result.append({
            "planet": other_name,
            "aspect": aspect,
            "orb": orb,
            "strength": strength_label(other_name, orb),
        })
    result.sort(key=lambda a: (strength_rank(a["strength"]), a["orb"]))
    return result


def houses_ruled_by(planet_name, cusps):
    ruled = []
    for house, cusp in enumerate(cusps, start=1):
        sign_index = cusp_sign(cusp)
        if planet_name in ENGINE_RULERS[sign_index]:
            ruled.append((house, SIGNS[sign_index]))
    return ruled


def main_ruler_for_sign(sign_index):
    return ENGINE_RULERS[sign_index][0]


def collect_key_personality_aspects(planets, focus_names):
    seen = set()
    rows = []

    for name in focus_names:
        for a in aspects_for(name, planets):
            pair = tuple(sorted((name, a["planet"])))
            if pair in seen:
                continue
            seen.add(pair)
            rows.append({
                "a": name,
                "b": a["planet"],
                "aspect": a["aspect"],
                "orb": a["orb"],
                "strength": a["strength"],
            })

    rows.sort(key=lambda x: (strength_rank(x["strength"]), x["orb"]))
    return rows[:3]


SUN_SIGN_TEXT = {
    "Овен": "ядро личности действует прямо, инициативно и через личный импульс",
    "Телец": "ядро личности стремится к устойчивости, качеству и материальной опоре",
    "Близнецы": "ядро личности раскрывается через информацию, контакт и интеллектуальную подвижность",
    "Рак": "ядро личности связано с эмоциональной памятью, защитой и принадлежностью",
    "Лев": "ядро личности требует самовыражения, признания и права занимать своё место",
    "Дева": "ядро личности реализуется через компетентность, пользу, анализ и улучшение",
    "Весы": "ядро личности развивается через отношения, баланс, вкус и умение видеть обе стороны",
    "Скорпион": "ядро личности стремится к глубине, контролю над собой и глубоких перемен",
    "Стрелец": "ядро личности раскрывается через смысл, рост, свободу и расширение горизонтов",
    "Козерог": "ядро личности строится вокруг зрелости, результата, структуры и личной ответственности",
    "Водолей": "ядро личности нуждается в независимом мышлении, свободе и собственном способе жить",
    "Рыбы": "ядро личности тонко воспринимает атмосферу, смыслы, чувства и невидимые связи",
}

MOON_SIGN_TEXT = {
    "Овен": "эмоции быстрые и прямые; для восстановления нужны движение и возможность немедленно реагировать",
    "Телец": "эмоциональная устойчивость приходит через телесный комфорт, предсказуемость и надёжность",
    "Близнецы": "чувства легче проживаются через разговор, понимание и смену впечатлений",
    "Рак": "эмоциональная безопасность особенно зависит от близких, дома и ощущения защищённости",
    "Лев": "эмоциям важно тепло, признание, щедрость и право проявляться ярко",
    "Дева": "эмоциональная система успокаивается, когда есть порядок, ясность и возможность что-то исправить",
    "Весы": "для душевного равновесия важны гармония, красота, контакт и отсутствие грубой конфронтации",
    "Скорпион": "эмоции глубокие и интенсивные; поверхностное проживание чувств почти не работает",
    "Стрелец": "эмоционально легче, когда есть пространство, движение, смысл и перспектива",
    "Козерог": "чувства контролируются; безопасность создаётся через собранность, ответственность и опору на себя",
    "Водолей": "эмоциям нужно пространство и интеллектуальная дистанция, прежде чем человек поймёт, что именно чувствует",
    "Рыбы": "эмоциональная восприимчивость высокая; важно регулярно отделять свои чувства от чужой атмосферы",
}

ASC_TEXT = {
    "Овен": "внешне человек воспринимается инициативным, быстрым и прямым",
    "Телец": "внешний стиль спокойный, устойчивый, чувственный и основательный",
    "Близнецы": "первое впечатление создают подвижность, контактность и любопытство",
    "Рак": "внешняя подача мягкая, осторожная и эмоционально восприимчивая",
    "Лев": "внешне заметны яркость, достоинство и потребность выразить себя",
    "Дева": "подача собранная, наблюдательная, точная и практичная",
    "Весы": "внешний стиль дипломатичный, эстетичный и ориентированный на контакт",
    "Скорпион": "первое впечатление сильное, закрытое, интенсивное и магнетичное",
    "Стрелец": "внешне ощущаются открытость, движение, прямота и интерес к большему",
    "Козерог": "подача серьёзная, собранная и ориентированная на контроль ситуации",
    "Водолей": "внешне подчёркнуты независимость, необычность и собственный ритм",
    "Рыбы": "первое впечатление мягкое, тонкое, пластичное и восприимчивое",
}

HOUSE_TEXT = {
    1: "через личность, образ, тело и способ заявлять о себе",
    2: "через деньги, самоценность, ресурсы и материальную опору",
    3: "через обучение, речь, мышление, документы и ближайшее окружение",
    4: "через дом, семью, корни, приватную жизнь и ощущение защищённости",
    5: "через творчество, романтику, удовольствие, детей и личное самовыражение",
    6: "через работу, навыки, рутину, здоровье и ежедневную эффективность",
    7: "через партнёрство, отношения, клиентов и взаимодействие один на один",
    8: "через близость, общие ресурсы, кризисы и глубокие изменения",
    9: "через образование, путешествия, мировоззрение, иностранную среду и смысл",
    10: "через карьеру, статус, цель, репутацию и общественную реализацию",
    11: "через друзей, сообщества, аудиторию, планы и коллективные проекты",
    12: "через личную жизнь и переживания, уединение, скрытые процессы и психологическую глубину",
}


def short_aspects_text(name, aspects, limit=2):
    if not aspects:
        return ""
    parts = []
    for a in aspects[:limit]:
        parts.append(
            f"{a['strength']} {a['aspect']} с {a['planet']}"
        )
    return "; ".join(parts)


def ruled_houses_text(rows):
    if not rows:
        return "не управляет отдельными куспидами в этой карте"
    return ", ".join(f"{h} домом" for h, _ in rows)


def user_profile_text(chart):
    sun = chart["planets"]["Солнце"]
    moon = chart["planets"]["Луна"]
    asc = chart["asc"]

    sun_house = chart["houses"]["Солнце"]
    moon_house = chart["houses"]["Луна"]

    sun_ruler_name = chart["sun_ruler_name"]
    sun_ruler = chart["planets"][sun_ruler_name]
    sun_ruler_house = chart["houses"][sun_ruler_name]

    asc_ruler_name = chart["asc_ruler_name"]
    asc_ruler = chart["planets"][asc_ruler_name]
    asc_ruler_house = chart["houses"][asc_ruler_name]

    sun_aspects = chart["aspects"]["Солнце"]
    moon_aspects = chart["aspects"]["Луна"]
    sun_ruler_aspects = chart["aspects"][sun_ruler_name]
    asc_ruler_aspects = chart["aspects"][asc_ruler_name]

    lines = [
        "КТО Я?",
        "",
        f"☉ Солнце - {sun.sign} {format_degree(sun.degree)}, {sun_house} дом.",
        f"Твоё основное «я»: {SUN_SIGN_TEXT[sun.sign]}. "
        f"В карте это особенно проявляется {HOUSE_TEXT[sun_house]}.",
    ]

    if sun_aspects:
        lines.append(
            "Что сильнее всего окрашивает Солнце: "
            + short_aspects_text("Солнце", sun_aspects)
            + "."
        )

    lines += [
        "",
        f"Управитель Солнца - {sun_ruler_name}.",
        f"Он стоит в {sun_ruler.sign}, {sun_ruler_house} доме. "
        f"Поэтому твоя солнечная энергия получает дополнительный канал реализации "
        f"{HOUSE_TEXT[sun_ruler_house]}.",
        f"{sun_ruler_name} управляет в карте: {ruled_houses_text(chart['ruled_houses'][sun_ruler_name])}.",
    ]

    if sun_ruler_aspects:
        lines.append(
            "Его главные связи: "
            + short_aspects_text(sun_ruler_name, sun_ruler_aspects)
            + "."
        )

    lines += [
        "",
        f"☽ Луна - {moon.sign} {format_degree(moon.degree)}, {moon_house} дом.",
        f"Эмоционально: {MOON_SIGN_TEXT[moon.sign]}. "
        f"Главная зона, где это особенно заметно, - {HOUSE_TEXT[moon_house]}.",
    ]

    if moon_aspects:
        lines.append(
            "Что сильнее всего влияет на Луну: "
            + short_aspects_text("Луна", moon_aspects)
            + "."
        )

    lines += [
        "",
        f"ASC - {asc.sign} {format_degree(asc.degree)}.",
        f"То, как ты входишь в мир: {ASC_TEXT[asc.sign]}.",
        f"Управитель ASC - {asc_ruler_name}: {asc_ruler.sign}, {asc_ruler_house} дом.",
        f"Именно через него стиль Асцендента реализуется {HOUSE_TEXT[asc_ruler_house]}.",
        f"{asc_ruler_name} управляет в карте: {ruled_houses_text(chart['ruled_houses'][asc_ruler_name])}.",
    ]

    if asc_ruler_aspects:
        lines.append(
            "Его главные связи: "
            + short_aspects_text(asc_ruler_name, asc_ruler_aspects)
            + "."
        )

    lines += ["", "ТВОИ 3 КЛЮЧЕВЫХ АСПЕКТА"]
    for row in chart["key_aspects"]:
        lines.append(
            f"• {row['a']} - {row['aspect']} - {row['b']} ({row['strength']})."
        )

    lines += [
        "",
        "Собираем тебя:",
        (
            f"В твоей карте одновременно работают три слоя: "
            f"Солнце в {sun.sign} задаёт центральный вектор личности, "
            f"Луна в {moon.sign} показывает эмоциональную природу, "
            f"а ASC в {asc.sign} - способ проявляться и взаимодействовать с миром. "
            f"Диспозитор Солнца {sun_ruler_name} в {sun_ruler_house} доме и "
            f"управитель ASC {asc_ruler_name} в {asc_ruler_house} доме показывают, "
            f"куда эта личность фактически направляет энергию. "
            f"Ключевые аспекты выше - те напряжения и ресурсы, которые сильнее всего "
            f"модифицируют базовый характер."
        ),
        "",
        "Это бесплатная база раздела «Я». Дальше ЭГОИСТ будет собирать её в более глубокий персональный портрет.",
    ]

    return "\n".join(lines)


def build_chart(date_str, time_str, lat, lon):
    utc_dt, tz_name = utc_from_local(date_str, time_str, lat, lon)
    jd = julian_day(utc_dt)

    planets = calculate_planets(jd)
    cusps, asc, mc = calculate_placidus_houses(jd, lat, lon)

    houses = {
        name: house_of_longitude(point.longitude, cusps)
        for name, point in planets.items()
    }

    aspects = {
        name: aspects_for(name, planets)
        for name in planets
    }

    sun = planets["Солнце"]
    sun_ruler_name = main_ruler_for_sign(sun.sign_index)
    asc_ruler_name = main_ruler_for_sign(asc.sign_index)

    ruled_houses = {
        name: houses_ruled_by(name, cusps)
        for name in planets
    }

    focus = ["Солнце", "Луна", sun_ruler_name, asc_ruler_name]
    key_aspects = collect_key_personality_aspects(planets, focus)

    return {
        "utc": utc_dt,
        "timezone": tz_name,
        "planets": planets,
        "cusps": cusps,
        "asc": asc,
        "mc": mc,
        "houses": houses,
        "aspects": aspects,
        "intercepted": intercepted_signs(cusps),
        "sun_ruler_name": sun_ruler_name,
        "asc_ruler_name": asc_ruler_name,
        "ruled_houses": ruled_houses,
        "key_aspects": key_aspects,
    }


# --- Compatibility API used by bot.py / interpretation.py ---
def fmtdeg(deg):
    return format_degree(deg)


def _calculate_nodes(jd_ut):
    lon = swe.calc_ut(jd_ut, swe.TRUE_NODE)[0][0] % 360.0
    north = point_from_longitude("Северный узел", lon)
    south = point_from_longitude("Южный узел", (lon + 180.0) % 360.0)
    return north, south


def strongest(c, names, limit=4):
    """Return strongest unique aspects touching any of the requested points."""
    planets = c["planets"]
    wanted = set(names)
    rows = []
    seen = set()
    for a_name, a in planets.items():
        for b_name, b in planets.items():
            if a_name >= b_name:
                continue
            if a_name not in wanted and b_name not in wanted:
                continue
            asp = whole_sign_aspect(a, b)
            if not asp:
                continue
            orb = exact_orb(a.longitude, b.longitude, asp)
            row = {
                "a": a_name, "b": b_name, "aspect": asp, "orb": orb,
                "strength": strength_label(b_name if a_name in wanted else a_name, orb),
            }
            key = (a_name, b_name, asp)
            if key not in seen:
                seen.add(key)
                rows.append(row)
    rows.sort(key=lambda x: (strength_rank(x["strength"]), x["orb"]))
    return rows[:limit]


def chart(date_str, time_str, lat, lon):
    c = build_chart(date_str, time_str, lat, lon)
    jd = julian_day(c["utc"])
    north, south = _calculate_nodes(jd)
    c["planets"]["Северный узел"] = north
    c["planets"]["Южный узел"] = south
    c["houses"]["Северный узел"] = house_of_longitude(north.longitude, c["cusps"])
    c["houses"]["Южный узел"] = house_of_longitude(south.longitude, c["cusps"])
    c["aspects"]["Северный узел"] = aspects_for("Северный узел", c["planets"])
    c["aspects"]["Южный узел"] = aspects_for("Южный узел", c["planets"])
    c["sun_dispositor"] = c.get("sun_ruler_name")
    c["asc_ruler"] = c.get("asc_ruler_name")
    c["date_str"] = date_str
    c["time_str"] = time_str
    c["lat"] = lat
    c["lon"] = lon
    return c


def _sun_longitude_at(dt_utc):
    return swe.calc_ut(julian_day(dt_utc), swe.SUN)[0][0] % 360.0


def _signed_angle(x):
    return (x + 180.0) % 360.0 - 180.0


def solar_return(natal, year, lat, lon):
    """Build a solar-return chart for the requested year and location."""
    from datetime import timedelta, timezone

    target = natal["planets"]["Солнце"].longitude
    natal_utc = natal.get("utc")
    if natal_utc is None:
        raise ValueError("В натальной карте отсутствует время рождения")

    # Search around the birthday in UTC, then refine by bisection.
    center = natal_utc.replace(year=int(year))
    start = center - timedelta(days=3)
    step = timedelta(hours=3)
    prev_t = start
    prev_f = _signed_angle(_sun_longitude_at(prev_t) - target)
    bracket = None
    for i in range(1, 49):
        cur_t = start + i * step
        cur_f = _signed_angle(_sun_longitude_at(cur_t) - target)
        if prev_f == 0 or cur_f == 0 or (prev_f < 0 <= cur_f) or (cur_f < 0 <= prev_f):
            # Reject discontinuity across +/-180.
            if abs(prev_f - cur_f) < 20:
                bracket = (prev_t, cur_t)
                break
        prev_t, prev_f = cur_t, cur_f
    if bracket is None:
        # Fallback: choose closest sample and refine around it.
        samples=[]
        for i in range(49):
            tt=start+i*step
            samples.append((abs(_signed_angle(_sun_longitude_at(tt)-target)),tt))
        _, best=min(samples,key=lambda x:x[0])
        bracket=(best-timedelta(hours=3),best+timedelta(hours=3))

    lo, hi = bracket
    for _ in range(40):
        mid = lo + (hi - lo) / 2
        flo = _signed_angle(_sun_longitude_at(lo) - target)
        fmid = _signed_angle(_sun_longitude_at(mid) - target)
        if abs(fmid) < 1e-8:
            lo = hi = mid
            break
        if flo * fmid <= 0:
            hi = mid
        else:
            lo = mid
    ret_utc = lo + (hi - lo) / 2

    jd = julian_day(ret_utc)
    planets = calculate_planets(jd)
    cusps, asc, mc = calculate_placidus_houses(jd, lat, lon)
    houses = {name: house_of_longitude(pt.longitude, cusps) for name, pt in planets.items()}
    north, south = _calculate_nodes(jd)
    planets["Северный узел"] = north
    planets["Южный узел"] = south
    houses["Северный узел"] = house_of_longitude(north.longitude, cusps)
    houses["Южный узел"] = house_of_longitude(south.longitude, cusps)
    return {
        "utc": ret_utc, "planets": planets, "cusps": cusps,
        "asc": asc, "mc": mc, "houses": houses,
        "lat": lat, "lon": lon,
    }


# =====================================================================
# TELEGRAM-БОТ EGOIST
# =====================================================================

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
      [InlineKeyboardButton(text="Разбор твоего «Я»",callback_data="author:me")],
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
    "me": "Разбор твоего «Я»: твои сильные и слабые стороны, с чем ты пришла в эту жизнь и главное - зачем",
    "karma": "Кармические задачи на это воплощение",
    "career": "Профессиональная реализация и финансы: где твои деньги и через что они приходят",
    "love": "Отношения: кого ты притягиваешь или отталкиваешь и почему так происходит",
    "venus": "Как понять себя и начать получать от жизни удовольствие: чего ты на самом деле хочешь и что мешает тебе это получать",
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

KNOWN_CITIES = {
    "уфа": (54.7388, 55.9721),
    "уфа россия": (54.7388, 55.9721),
    "москва": (55.7558, 37.6173),
    "москва россия": (55.7558, 37.6173),
    "санкт-петербург": (59.9343, 30.3351),
    "санкт петербург": (59.9343, 30.3351),
    "казань": (55.7961, 49.1064),
    "екатеринбург": (56.8389, 60.6057),
    "новосибирск": (55.0084, 82.9357),
    "калининград": (54.7104, 20.4522),
    "уст каменогорск": (49.9483, 82.6275),
    "усть каменогорск": (49.9483, 82.6275),
    "усть-каменогорск": (49.9483, 82.6275),
    "nice": (43.7102, 7.2620),
    "ницца": (43.7102, 7.2620),
    "париж": (48.8566, 2.3522),
    "paris": (48.8566, 2.3522),
}

def _normalize_city_name(city):
    s = city.strip().lower().replace(",", " ")
    s = re.sub(r"\s+", " ", s)
    return s

async def locate(city):
    key = _normalize_city_name(city)

    # Для частых городов не зависим от внешнего геокодера вообще.
    if key in KNOWN_CITIES:
        return KNOWN_CITIES[key]

    # Для остальных городов пробуем Nominatim несколько раз.
    query = city.strip()
    last_error = None
    for attempt in range(3):
        try:
            loc = await asyncio.to_thread(
                geo.geocode,
                query,
                language="ru",
                exactly_one=True,
                timeout=10,
                addressdetails=False,
            )
            if loc:
                return float(loc.latitude), float(loc.longitude)
            break
        except Exception as e:
            last_error = e
            if attempt < 2:
                await asyncio.sleep(1.2)

    if last_error:
        raise ValueError(
            "Сервис поиска города временно не ответил. "
            "Попробуй написать город вместе со страной, например: Уфа, Россия."
        )
    raise ValueError("Не нашла город. Напиши город и страну, например: Уфа, Россия.")



# ---------------------------------------------------------------------
# STAGE 2: НОВЫЕ 5 БЛОКОВ EGOIST
# Внутри - синтез карты. Наружу - человеческий текст.
# ---------------------------------------------------------------------

SIGNS = [
    "Овен","Телец","Близнецы","Рак","Лев","Дева",
    "Весы","Скорпион","Стрелец","Козерог","Водолей","Рыбы"
]

SIGN_GEN = {
    "Овен":"Овна","Телец":"Тельца","Близнецы":"Близнецов","Рак":"Рака",
    "Лев":"Льва","Дева":"Девы","Весы":"Весов","Скорпион":"Скорпиона",
    "Стрелец":"Стрельца","Козерог":"Козерога","Водолей":"Водолея","Рыбы":"Рыб"
}

SIGN_PREP = {
    "Овен":"Овне","Телец":"Тельце","Близнецы":"Близнецах","Рак":"Раке",
    "Лев":"Льве","Дева":"Деве","Весы":"Весах","Скорпион":"Скорпионе",
    "Стрелец":"Стрельце","Козерог":"Козероге","Водолей":"Водолее","Рыбы":"Рыбах"
}


PLANET_INSTR = {
    "Солнце":"Солнцем",
    "Луна":"Луной",
    "Меркурий":"Меркурием",
    "Венера":"Венерой",
    "Марс":"Марсом",
    "Юпитер":"Юпитером",
    "Сатурн":"Сатурном",
    "Уран":"Ураном",
    "Нептун":"Нептуном",
    "Плутон":"Плутоном",
    "Асцендент":"Асцендентом",
    "Северный узел":"Северным узлом",
    "Южный узел":"Южным узлом",
}

SIGN_DATIVE = {
    "Овен":"Овну","Телец":"Тельцу","Близнецы":"Близнецам","Рак":"Раку",
    "Лев":"Льву","Дева":"Деве","Весы":"Весам","Скорпион":"Скорпиону",
    "Стрелец":"Стрельцу","Козерог":"Козерогу","Водолей":"Водолею","Рыбы":"Рыбам",
}

SIGN_WITH = {
    "Овен":"Овном","Телец":"Тельцом","Близнецы":"Близнецами","Рак":"Раком",
    "Лев":"Львом","Дева":"Девой","Весы":"Весами","Скорпион":"Скорпионом",
    "Стрелец":"Стрельцом","Козерог":"Козерогом","Водолей":"Водолеем","Рыбы":"Рыбами",
}

def planet_instr(name):
    return PLANET_INSTR.get(name, name)

def sign_dative(sign):
    return SIGN_DATIVE.get(sign, sign)

def sign_with(sign):
    return SIGN_WITH.get(sign, sign)

def sign_phrase_po(sign):
    return "по " + sign_dative(sign)

def sign_phrase_with(sign):
    return "с " + sign_with(sign)

def planet_phrase_with(name):
    return "с " + planet_instr(name)


def house_instr(house):
    return f"{house} домом"

def houses_instr(houses):
    houses = [int(h) for h in houses]
    if not houses:
        return ""
    if len(houses) == 1:
        return house_instr(houses[0])
    if len(houses) == 2:
        return f"{houses[0]} и {houses[1]} домами"
    return ", ".join(str(h) for h in houses[:-1]) + f" и {houses[-1]} домами"


OPPOSITE = {
    "Овен":"Весы","Телец":"Скорпион","Близнецы":"Стрелец","Рак":"Козерог",
    "Лев":"Водолей","Дева":"Рыбы","Весы":"Овен","Скорпион":"Телец",
    "Стрелец":"Близнецы","Козерог":"Рак","Водолей":"Лев","Рыбы":"Дева"
}

RULERS = {
    "Овен":"Марс","Телец":"Венера","Близнецы":"Меркурий","Рак":"Луна",
    "Лев":"Солнце","Дева":"Меркурий","Весы":"Венера","Скорпион":"Плутон",
    "Стрелец":"Юпитер","Козерог":"Сатурн","Водолей":"Уран","Рыбы":"Нептун"
}

SIGN_SYMBOL = {
    "Овен":"инициатива, смелость, самостоятельность, действие, конкуренция и право идти первой",
    "Телец":"устойчивость, комфорт, телесность, деньги, качество, удовольствие и материальная опора",
    "Близнецы":"общение, языки, обучение, информация, письмо, связи и быстрый обмен",
    "Рак":"дом, семья, забота, родительство, питание, уют и эмоциональная поддержка",
    "Лев":"творчество, сцена, яркость, лидерство, дети, самовыражение и признание",
    "Дева":"работа, мастерство, анализ, здоровье, порядок, сервис и практическая польза",
    "Весы":"партнёрство, эстетика, консультации, переговоры, баланс, право и дипломатия",
    "Скорпион":"глубина, кризисы, психология, власть, сексуальность, риск и трансформация",
    "Стрелец":"обучение, высшее знание, путешествия, иностранцы, преподавание и мировоззрение",
    "Козерог":"труд, упорство, взрослость, ответственность, карьера, статус, структура и дисциплина",
    "Водолей":"свобода, друзья, сообщества, технологии, астрология, инновации и нестандартность",
    "Рыбы":"музыка, творчество, интуиция, эмпатия, психология, помощь, вода и тонкое восприятие"
}

HOUSE_SYMBOL = {
    1:"личность, самостоятельность, образ и способ проявляться",
    2:"деньги, имущество, самоценность и личные ресурсы",
    3:"обучение, речь, языки, письмо, информацию, поездки и связи",
    4:"дом, семью, родителей, корни, недвижимость и ощущение защищённости",
    5:"творчество, любовь, детей, сцену, хобби и удовольствие",
    6:"работу, здоровье, мастерство, ежедневные навыки и режим",
    7:"партнёрство, брак, клиентов, договоры и взаимодействие один на один",
    8:"кризисы, интимность, чужие ресурсы, инвестиции и трансформацию",
    9:"высшее образование, путешествия, иностранцев, философию и преподавание",
    10:"карьеру, статус, управление, репутацию и общественную реализацию",
    11:"друзей, сообщества, аудиторию, технологии и коллективные проекты",
    12:"уединение, закулисье, творчество, помощь и скрытые процессы"
}

SUN_CORE = {
    "Овен":"тебе важно действовать, начинать и ощущать, что ты сама влияешь на ход событий",
    "Телец":"тебе важно создавать устойчивость, качество и реальный материальный результат",
    "Близнецы":"тебя зажигают идеи, общение, обучение и возможность быстро соединять информацию",
    "Рак":"тебе важно чувствовать эмоциональную связь, создавать своё пространство и заботиться о близких",
    "Лев":"тебе важно творить, проявляться, быть замеченной и вкладывать себя в то, что любишь",
    "Дева":"тебя зажигает мастерство, полезность, точность и возможность улучшать то, к чему ты прикасаешься",
    "Весы":"тебе важны красота, партнёрство, справедливость и умение создавать гармонию между людьми",
    "Скорпион":"тебе нужна глубина, сильная вовлечённость и возможность докапываться до сути",
    "Стрелец":"тебя зажигают масштаб, знания, путешествия, развитие и ощущение большого смысла",
    "Козерог":"тебе важно строить долгий результат, расти профессионально и уважать собственные достижения",
    "Водолей":"тебе нужны свобода мысли, необычные идеи, своё окружение и возможность делать не как принято",
    "Рыбы":"тебя зажигают воображение, тонкое понимание людей, творчество, смысл и работа с тем, что нельзя измерить только логикой"
}

MOON_NEED = {
    "Овен":"движение, свобода реакции и возможность быстро выпускать напряжение",
    "Телец":"спокойствие, телесный комфорт, предсказуемость и приятная среда",
    "Близнецы":"разговор, информация, смена впечатлений и интеллектуальное переключение",
    "Рак":"дом, близкие, забота, тёплая атмосфера и ощущение эмоциональной безопасности",
    "Лев":"тепло, внимание, творчество, радость и возможность открыто выражать чувства",
    "Дева":"порядок, понятный режим, ощущение полезности и контроль над бытовыми деталями",
    "Весы":"мирная атмосфера, красота, партнёрство и возможность спокойно договориться",
    "Скорпион":"глубокая честность, сильная близость и ощущение, что ничего важного не замалчивается",
    "Стрелец":"пространство, движение, новые впечатления, юмор и перспектива",
    "Козерог":"структура, надёжность, собранность и ощущение, что ситуация под контролем",
    "Водолей":"свобода, дистанция, друзья, интеллектуальная среда и право побыть отдельно",
    "Рыбы":"тишина, музыка, творчество, сон, вода, уединение и мягкая эмоциональная среда"
}

VENUS_LOVE = {
    "Овен":"любишь ярко, быстро и прямо. Важны искра, инициатива, драйв и ощущение живого интереса",
    "Телец":"любишь через стабильность, прикосновения, качество, верность, комфорт и телесную близость",
    "Близнецы":"влюбляешься через разговор, юмор, интеллект, любопытство и постоянный обмен",
    "Рак":"любишь через заботу, домашность, эмоциональную включённость и ощущение семьи",
    "Лев":"любишь щедро и заметно. Важны романтика, восхищение, яркость и гордость друг за друга",
    "Дева":"показываешь любовь делами, вниманием к деталям, полезностью и заботой о качестве жизни",
    "Весы":"тебе важны взаимность, красота, такт, партнёрство, эстетика и умение слышать друг друга",
    "Скорпион":"любишь глубоко и интенсивно. Важны страсть, честность, сильная эмоциональная и сексуальная связь",
    "Стрелец":"тебе нужны свобода, приключение, развитие, путешествия и ощущение, что отношения расширяют жизнь",
    "Козерог":"тебе важны серьёзность, надёжность, качество, поступки и перспектива отношений",
    "Водолей":"тебе нужны дружба, свобода, интеллектуальная связь, необычность и отсутствие удушающего контроля",
    "Рыбы":"любишь тонко, романтично и эмпатично. Важны душевность, музыка, образность и ощущение особой связи"
}

ASC_PRESENT = {
    "Овен":"ты производишь впечатление прямого, быстрого и инициативного человека",
    "Телец":"ты производишь впечатление спокойного, основательного и чувственного человека",
    "Близнецы":"ты кажешься подвижной, контактной, любопытной и быстрой",
    "Рак":"ты воспринимаешься мягкой, осторожной и эмоционально чувствительной",
    "Лев":"ты заметна, выразительна и производишь впечатление человека с достоинством",
    "Дева":"ты кажешься собранной, наблюдательной, точной и практичной",
    "Весы":"ты производишь впечатление дипломатичной, эстетичной и ориентированной на контакт",
    "Скорпион":"ты воспринимаешься сильной, закрытой, интенсивной и магнетичной",
    "Стрелец":"ты кажешься открытой, прямой, живой и ориентированной на движение",
    "Козерог":"ты производишь впечатление серьёзной, собранной и контролирующей ситуацию",
    "Водолей":"ты кажешься независимой, необычной и живущей в собственном ритме",
    "Рыбы":"ты воспринимаешься мягкой, тонкой, пластичной и очень чувствительной к атмосфере"
}

DIGNITY = {
    "Солнце":{"fall":["Весы"],"detriment":["Водолей"]},
    "Луна":{"fall":["Скорпион"],"detriment":["Козерог"]},
    "Меркурий":{"fall":["Рыбы"],"detriment":["Стрелец"]},
    "Венера":{"fall":["Дева"],"detriment":["Овен","Скорпион"]},
    "Марс":{"fall":["Рак"],"detriment":["Телец","Весы"]},
    "Юпитер":{"fall":["Козерог"],"detriment":["Близнецы","Дева"]},
    "Сатурн":{"fall":["Овен"],"detriment":["Рак","Лев"]},
}

TENSE = {"квадрат","оппозиция"}

def _p(c, name):
    return c["planets"][name]

def _house(c, name):
    return int(c["houses"][name])

def _aspect_rows(c, name, tense_only=False, max_orb=6.0):
    rows=[]
    for a in c.get("aspects",{}).get(name,[]) or []:
        asp=a.get("aspect")
        orb=float(a.get("orb",99))
        if orb > max_orb:
            continue
        if tense_only and asp not in TENSE:
            continue
        rows.append(a)
    rows.sort(key=lambda x: float(x.get("orb",99)))
    return rows

def _aspect_to(c, a, b, max_orb=6.0):
    for row in _aspect_rows(c,a,False,max_orb):
        if row.get("planet")==b:
            return row
    return None

def _cusp_sign(c, house):
    lon=float(c["cusps"][house-1]) % 360
    return SIGNS[int(lon//30)]

def _dsc_sign(c):
    return OPPOSITE[c["asc"].sign]

def _dignity_issue(planet, sign):
    d=DIGNITY.get(planet,{})
    if sign in d.get("fall",[]):
        return "падение"
    if sign in d.get("detriment",[]):
        return "изгнание"
    return None

def _clean_join(items, limit=3):
    vals=[]
    for x in items:
        if x and x not in vals:
            vals.append(x)
    return ", ".join(vals[:limit])

def _aspect_modifier(name, row):
    if not row:
        return ""
    other=row.get("planet","")
    asp=row.get("aspect","")
    tense=asp in TENSE
    if other=="Плутон":
        return "добавляет глубину, сильную вовлечённость и тему контроля" if tense else "добавляет психологическую глубину и психологическую силу"
    if other=="Сатурн":
        return "добавляет самоконтроль, серьёзность и страх ошибиться" if tense else "даёт выдержку, надёжность и способность строить надолго"
    if other=="Уран":
        return "усиливает потребность в свободе и резкую реакцию на ограничения" if tense else "даёт оригинальность и независимость"
    if other=="Нептун":
        return "усиливает идеализацию, впечатлительность и размытые границы" if tense else "усиливает интуицию, творчество и тонкое восприятие"
    if other=="Марс":
        return "добавляет импульсивность, напор и противоречивость и напряжение" if tense else "добавляет смелость и инициативу"
    if other=="Юпитер":
        return "может раскачивать крайности и завышать ожидания" if tense else "расширяет возможности и уверенность"
    if other=="Венера":
        return "создаёт личный конфликт между желанием и гармонией" if tense else "смягчает проявление и добавляет вкус"
    if other=="Луна":
        return "делает тему эмоционально заряженной" if tense else "соединяет волю с эмоциональными потребностями"
    if other=="Солнце":
        return "делает тему частью самоощущения и личной воли"
    if other=="Меркурий":
        return "сильно связывает тему с мышлением и словами"
    return ""


# =====================================================================
# EGOIST STAGE 4 - СОГЛАСОВАННЫЕ НОРМАТИВЫ 5 БЛОКОВ
# Эти данные перенесены из отдельных файлов правил, которые были
# согласованы поэтапно. Это не сокращённая версия Stage 3.
# =====================================================================

WHO_SIGN_MEANINGS = {'Овен': {'sun_core': ['инициатива, прямота, смелость, самостоятельность',
                       'потребность действовать, начинать и видеть быстрый отклик',
                       'сила проявляется через решительность и способность первым идти в неизвестное'],
          'sun_ignite': ['движение, вызов, конкуренция, возможность действовать самостоятельно',
                         'задачи, где нужно быстро принимать решения и влиять на результат'],
          'asc': 'Может производить впечатление прямого, энергичного, самостоятельного человека, который быстро '
                 'включается в происходящее.',
          'moon_comfort': 'Эмоционально важны свобода реакции, возможность действовать и не чувствовать себя '
                          'беспомощным.',
          'moon_stress': 'В стрессе может резко реагировать, раздражаться или пытаться немедленно решить проблему '
                         'действием.',
          'shadow': 'Весы',
          'shadow_resource': 'Ресурс тени - способность учитывать другого человека, договариваться, выдерживать паузу '
                             'и выбирать не только силой импульса.',
          'shadow_extreme': 'Перекос возможен между привычкой всё решать самому и зависимостью от чужой реакции или '
                            'одобрения.'},
 'Телец': {'sun_core': ['устойчивость, практичность, терпение, способность создавать материальную опору',
                        'потребность в качестве, надёжности и ощутимом результате',
                        'сила проявляется через последовательность и умение сохранять ценное'],
           'sun_ignite': ['понятный результат, качество, красота, комфорт, материальная устойчивость',
                          'дело, которое можно постепенно развивать и видеть его реальную ценность'],
           'asc': 'Может производить впечатление спокойного, устойчивого и не склонного суетиться человека.',
           'moon_comfort': 'Эмоционально важны предсказуемость, телесный комфорт, надёжность и ощущение прочной почвы '
                           'под ногами.',
           'moon_stress': 'В стрессе может цепляться за привычное, сопротивляться переменам и искать успокоение через '
                          'комфорт.',
           'shadow': 'Скорпион',
           'shadow_resource': 'Ресурс тени - способность не только сохранять, но и вовремя отпускать, меняться, '
                              'проходить через кризисы и не бояться глубины.',
           'shadow_extreme': 'Перекос возможен между чрезмерным удержанием привычного и резкими глубокими кризисами, '
                             'которые заставляют всё менять.'},
 'Близнецы': {'sun_core': ['любознательность, гибкость, скорость мышления, способность связывать людей и информацию',
                           'потребность понимать, обсуждать, сравнивать и постоянно узнавать новое',
                           'сила проявляется через слово, контакт, обучение и интеллектуальную подвижность'],
              'sun_ignite': ['новая информация, разговоры, обучение, движение, разнообразие',
                             'среда, где можно задавать вопросы, обмениваться идеями и быстро переключаться'],
              'asc': 'Может производить впечатление лёгкого, любознательного, разговорчивого и быстро реагирующего '
                     'человека.',
              'moon_comfort': 'Эмоционально легче, когда происходящее можно назвать словами, обсудить и понять.',
              'moon_stress': 'В стрессе может начинать слишком много думать, переключаться между версиями и искать '
                             'информацию вместо проживания чувства.',
              'shadow': 'Стрелец',
              'shadow_resource': 'Ресурс тени - способность собирать множество фактов в единую картину, формировать '
                                 'позицию и видеть большой смысл за отдельными деталями.',
              'shadow_extreme': 'Перекос возможен между вечным поиском новых фактов и желанием однажды найти один '
                                'окончательный ответ.'},
 'Рак': {'sun_core': ['чувствительность, память, забота, сильная связь с близкими и личным пространством',
                      'потребность создавать безопасность, принадлежность и эмоционально значимые связи',
                      'сила проявляется через способность чувствовать людей и защищать то, что дорого'],
         'sun_ignite': ['близкие отношения, ощущение нужности, создание своего пространства, забота',
                        'дело, в котором есть эмоциональная вовлечённость и человеческий смысл'],
         'asc': 'Может производить впечатление мягкого, осторожного и чувствительного к атмосфере человека.',
         'moon_comfort': 'Эмоционально важны близость, доверие, дом, свои люди и возможность не защищаться постоянно.',
         'moon_stress': 'В стрессе может закрываться, сильнее цепляться за близких или болезненно реагировать на '
                        'отсутствие поддержки.',
         'shadow': 'Козерог',
         'shadow_resource': 'Ресурс тени - эмоциональная зрелость, границы, самостоятельность, умение действовать даже '
                            'тогда, когда эмоциональной поддержки недостаточно.',
         'shadow_extreme': 'Перекос возможен между чрезмерной зависимостью от чувства безопасности и попыткой '
                           'полностью запретить себе слабость.'},
 'Лев': {'sun_core': ['яркость, творческая воля, достоинство, потребность проявлять индивидуальность',
                      'желание быть автором своей жизни и видеть признание своего вклада',
                      'сила проявляется через смелое самовыражение, создание и лидерство'],
         'sun_ignite': ['творчество, личная ответственность, признание, возможность влиять и создавать',
                        'задачи, где можно проявить индивидуальность и гордиться результатом'],
         'asc': 'Может производить впечатление заметного, уверенного, тёплого или собранного человека.',
         'moon_comfort': 'Эмоционально важны тепло, искреннее внимание, признание и возможность чем-то гордиться.',
         'moon_stress': 'В стрессе может особенно болезненно реагировать на холодность, игнорирование или ощущение, '
                        'что его недооценили.',
         'shadow': 'Водолей',
         'shadow_resource': 'Ресурс тени - способность видеть себя частью большего, сотрудничать на равных, не '
                            'зависеть полностью от личного признания и делать что-то ради общей идеи.',
         'shadow_extreme': 'Перекос возможен между сильной потребностью быть замеченным и демонстративным отстранением '
                           'в духе «мне никто не нужен и чужое мнение не важно».'},
 'Дева': {'sun_core': ['наблюдательность, аналитичность, точность, практическая полезность',
                       'потребность разбираться, улучшать, систематизировать и становиться мастером',
                       'сила проявляется через компетентность, качество и способность видеть то, что другие '
                       'пропускают'],
          'sun_ignite': ['обучение, сложные задачи, совершенствование навыка, понятная польза',
                         'работа, где можно улучшать процесс, повышать качество и видеть конкретный результат'],
          'asc': 'Может производить впечатление собранного, наблюдательного, аккуратного и внимательного к деталям '
                 'человека.',
          'moon_comfort': 'Эмоционально легче, когда есть порядок, понятность, полезные действия и ощущение, что '
                          'ситуацию можно привести в систему.',
          'moon_stress': 'В стрессе может усиливаться тревожный анализ, поиск ошибок, контроль деталей и недовольство '
                         'собой.',
          'shadow': 'Рыбы',
          'shadow_resource': 'Ресурс тени - интуиция, воображение, доверие процессу, способность принимать '
                             'несовершенство и чувствовать то, что невозможно полностью разложить по полочкам.',
          'shadow_extreme': 'Перекос возможен между попыткой всё контролировать и периодическим желанием полностью '
                            'отпустить ситуацию или уйти от неё.'},
 'Весы': {'sun_core': ['чувство баланса, эстетика, дипломатичность, способность видеть несколько сторон',
                       'потребность во взаимодействии, взаимности и красивой, справедливой системе отношений',
                       'сила проявляется через переговоры, партнёрство, вкус и умение соединять людей'],
          'sun_ignite': ['сотрудничество, эстетика, общение, интеллектуальный обмен, создание гармоничных решений',
                         'среда, где важно учитывать интересы нескольких сторон и находить точку равновесия'],
          'asc': 'Может производить впечатление приятного, дипломатичного, эстетичного и умеющего чувствовать '
                 'социальную дистанцию человека.',
          'moon_comfort': 'Эмоционально важны мирная атмосфера, взаимность, уважительное общение и ощущение '
                          'равновесия.',
          'moon_stress': 'В стрессе может долго колебаться, избегать прямого конфликта или слишком сильно '
                         'ориентироваться на реакцию другого.',
          'shadow': 'Овен',
          'shadow_resource': 'Ресурс тени - решительность, право хотеть своего, способность сказать «нет» и '
                             'действовать до того, как все стороны будут полностью довольны.',
          'shadow_extreme': 'Перекос возможен между чрезмерной адаптацией к другим и внезапной резкостью после долгого '
                            'подавления собственных желаний.'},
 'Скорпион': {'sun_core': ['глубина, воля, наблюдательность, способность видеть скрытые мотивы',
                           'потребность в настоящей интенсивности, честности и психологической силе',
                           'сила проявляется через кризисоустойчивость, трансформацию и способность идти в сложные '
                           'темы'],
              'sun_ignite': ['сильные задачи, глубина, исследования, влияние, преодоление',
                             'ситуации, где нужно выдержать давление, докопаться до сути или изменить систему'],
              'asc': 'Может производить впечатление сильного, закрытого, наблюдательного и не склонного сразу '
                     'раскрывать себя человека.',
              'moon_comfort': 'Эмоционально важны доверие, глубина, честность и ощущение, что близость настоящая, а не '
                              'формальная.',
              'moon_stress': 'В стрессе может усиливаться подозрительность, контроль, напряжение или страх '
                             'потери влияния.',
              'shadow': 'Телец',
              'shadow_resource': 'Ресурс тени - простота, телесная устойчивость, спокойствие, способность наслаждаться '
                                 'жизнью без необходимости постоянно проходить через эмоциональную интенсивность.',
              'shadow_extreme': 'Перекос возможен между постоянной потребностью всё углублять и сильным желанием '
                                'однажды просто остановиться и удержать стабильность.'},
 'Стрелец': {'sun_core': ['масштаб мышления, оптимизм, стремление к развитию, свободе и смыслу',
                          'потребность расширять горизонты, учиться, путешествовать, преподавать или передавать идеи',
                          'сила проявляется через способность видеть перспективу и заражать других верой в возможность '
                          'роста'],
             'sun_ignite': ['обучение, путешествия, большие цели, международная среда, новые мировоззрения',
                            'дело, где есть перспектива, рост и ощущение, что мир становится шире'],
             'asc': 'Может производить впечатление открытого, прямого, энергичного и ориентированного на возможности '
                    'человека.',
             'moon_comfort': 'Эмоционально важны свобода, перспектива, движение и ощущение, что впереди есть что-то '
                             'новое.',
             'moon_stress': 'В стрессе может пытаться быстрее вырваться из тяжёлого состояния, обесценить проблему или '
                            'срочно сменить обстановку.',
             'shadow': 'Близнецы',
             'shadow_resource': 'Ресурс тени - внимание к фактам, деталям, конкретным вопросам и способность не только '
                                'видеть большую идею, но и проверять, из чего она реально состоит.',
             'shadow_extreme': 'Перекос возможен между уверенной большой картиной мира и распылением на множество '
                               'противоречивых фактов.'},
 'Козерог': {'sun_core': ['ответственность, амбиции, выдержка, ориентация на результат и долгий путь',
                          'потребность строить, достигать, становиться компетентнее и чувствовать направление жизни',
                          'сила проявляется через дисциплину, стойкость характера и способность выдерживать большие '
                          'задачи'],
             'sun_ignite': ['цель, рост, ответственность, сложные проекты, возможность становиться сильнее',
                            'ситуации, где можно выстроить систему и со временем получить серьёзный результат'],
             'asc': 'Может производить впечатление собранного, взрослого, сдержанного и ориентированного на результат '
                    'человека.',
             'moon_comfort': 'Эмоционально важны надёжность, понятные границы, уважение и ощущение, что на '
                             'происходящее можно опереться.',
             'moon_stress': 'В стрессе может сильнее контролировать себя, уходить в обязанности и запрещать себе '
                            'показывать слабость.',
             'shadow': 'Рак',
             'shadow_resource': 'Ресурс тени - способность чувствовать, принимать поддержку, заботиться о себе и '
                                'создавать эмоционально безопасное пространство.',
             'shadow_extreme': 'Перекос возможен между постоянным «надо справляться» и сильной скрытой потребностью, '
                               'чтобы о человеке тоже кто-то позаботился.'},
 'Водолей': {'sun_core': ['независимость мышления, оригинальность, свобода, ориентация на идеи и будущее',
                          'потребность жить не по шаблону и самостоятельно определять правила',
                          'сила проявляется через нестандартный взгляд, реформы, сообщества и новые решения'],
             'sun_ignite': ['новые идеи, технологии, свобода, единомышленники, интеллектуальная среда',
                            'задачи, где можно изменить привычную систему или придумать другой способ'],
             'asc': 'Может производить впечатление независимого, необычного, интеллектуального или немного '
                    'дистанцированного человека.',
             'moon_comfort': 'Эмоционально важны свобода, пространство, возможность оставаться собой и не чувствовать '
                             'давления.',
             'moon_stress': 'В стрессе может уходить в дистанцию, рационализировать чувства или резко отстраняться от '
                            'эмоционального давления.',
             'shadow': 'Лев',
             'shadow_resource': 'Ресурс тени - право быть заметной лично, проявлять сердечность и щедрость, творчески выражать себя, '
                                'гордиться своими сильными сторонами и не растворяться полностью в группе или общей идее.',
             'shadow_extreme': 'Перекос возможен между демонстративной независимостью от признания и скрытым сильным '
                               'желанием быть особенным и замеченным.'},
 'Рыбы': {'sun_core': ['чувствительность, интуиция, воображение, эмпатия, способность чувствовать невидимые связи',
                       'потребность в смысле, вдохновении и пространстве для своего личного мира',
                       'сила проявляется через творчество, сострадание, образное мышление и тонкое восприятие'],
          'sun_ignite': ['творчество, интуитивная работа, помощь, искусство, смысл, глубокое эмоциональное включение',
                         'среда, где можно чувствовать, представлять, соединять идеи и не жить исключительно по сухим '
                         'правилам'],
          'asc': 'Может производить впечатление мягкого, чувствительного, интуитивного или немного ускользающего '
                 'человека.',
          'moon_comfort': 'Эмоционально важны тишина, возможность побыть наедине с собой, вдохновение и пространство '
                          'без жёсткого давления.',
          'moon_stress': 'В стрессе может усиливаться желание уйти от перегруза, закрыться, раствориться в фантазиях '
                         'или слишком сильно впитывать чужое состояние.',
          'shadow': 'Дева',
          'shadow_resource': 'Ресурс тени - структура, конкретика, границы, режим и способность переводить ощущение в '
                             'понятное действие.',
          'shadow_extreme': 'Перекос возможен между жизнью по вдохновению и болезненной попыткой всё контролировать, '
                            'когда неопределённости становится слишком много.'}}
WHO_SUN_SHADOW = {'Овен': 'Весы',
 'Телец': 'Скорпион',
 'Близнецы': 'Стрелец',
 'Рак': 'Козерог',
 'Лев': 'Водолей',
 'Дева': 'Рыбы',
 'Весы': 'Овен',
 'Скорпион': 'Телец',
 'Стрелец': 'Близнецы',
 'Козерог': 'Рак',
 'Водолей': 'Лев',
 'Рыбы': 'Дева'}
WHO_SUN_HOUSES = {1: 'Солнце проявляется через личную инициативу, образ, самостоятельность и право быть заметным.',
 2: 'Солнце раскрывается через создание личной ценности, ресурсов, заработка, качества и материальной опоры.',
 3: 'Солнце проявляется через обучение, речь, письмо, связи, информацию, поездки и передачу знаний.',
 4: 'Солнце сильно связано с домом, семьёй, корнями, личным пространством и созданием собственной опоры.',
 5: 'Солнце раскрывается через творчество, самовыражение, удовольствие, сцену, проекты, детей и право создавать своё.',
 6: 'Солнце проявляется через мастерство, полезность, систематизацию, качество работы, здоровье, сервис и ежедневную '
    'практику.',
 7: 'Солнце раскрывается через партнёрства, клиентов, взаимодействие с людьми и умение строить отношения на равных.',
 8: 'Солнце проявляется через глубину, кризисы, трансформации, управление общими ресурсами, психологию и сложные '
    'процессы.',
 9: 'Солнце раскрывается через обучение высокого уровня, преподавание, путешествия, иностранную среду, мировоззрение и '
    'большие смыслы.',
 10: 'Солнце стремится проявиться через профессию, статус, достижения, управление, репутацию и видимый результат.',
 11: 'Солнце раскрывается через сообщества, друзей, аудиторию, коллективные проекты, технологии и идеи будущего.',
 12: 'Солнце проявляется глубже и тише: через закулисную работу, уединение, творчество, интуицию, помощь, исследование '
     'и закрытые процессы.'}
WHO_DISPOSITOR = {'Овен': 'Марс',
 'Телец': 'Венера',
 'Близнецы': 'Меркурий',
 'Рак': 'Луна',
 'Лев': 'Солнце',
 'Дева': 'Меркурий',
 'Весы': 'Венера',
 'Скорпион': 'Плутон',
 'Стрелец': 'Юпитер',
 'Козерог': 'Сатурн',
 'Водолей': 'Уран',
 'Рыбы': 'Нептун'}
WHO_PAID_TITLE = 'Как раскрыть себя сильнее'
WHO_PAID_POINTS = ['как проявлять себя через своё Солнце и что тебя по-настоящему зажигает;',
 'что тебя гасит и мешает проявляться в полную силу;',
 'как использовать ресурс своей тени и не уходить в её крайности;',
 'как справляться со стрессом, возвращаться к эмоциональному комфорту и находить свой ресурс.']

LOVE_VENUS_SIGNS = {'Овен': {'love': 'Любит живо, прямо и быстро. Нужны искра, интерес, движение и ощущение, что отношения не '
                  'превратились в скучную привычку.',
          'pleasure': 'Удовольствие через активность, новые впечатления, инициативу, азарт, спорт, спонтанные решения '
                      'и возможность выбирать самой.',
          'risk': 'Может быстро загораться и так же быстро терять интерес, если исчезают драйв и ощущение движения.'},
 'Телец': {'love': 'Любит чувственно, надёжно и последовательно. Важны верность, прикосновения, понятность отношений и '
                   'физическое ощущение комфорта рядом с партнёром.',
           'pleasure': 'Удовольствие через вкусную еду, прикосновения, качество вещей, красивое пространство, музыку, '
                       'запахи, природу, телесность и материальную устойчивость.',
           'risk': 'Может слишком долго держаться за привычное, даже если отношения или образ жизни уже перестали '
                   'радовать.'},
 'Близнецы': {'love': 'Влюбляется через интерес, разговор, юмор и интеллектуальный контакт. Важно, чтобы с партнёром '
                      'было о чём говорить и чтобы отношения оставались живыми.',
              'pleasure': 'Удовольствие через общение, информацию, обучение, книги, поездки, новые темы и '
                          'разнообразие.',
              'risk': 'Может быстро скучать, если в отношениях нет движения, обмена мыслями и свежих впечатлений.'},
 'Рак': {'love': 'Любит через заботу, эмоциональную включённость, домашность и ощущение «мы свои». Нужны безопасность, '
                 'нежность и чувство принадлежности.',
         'pleasure': 'Удовольствие через дом, близких, уют, еду, воспоминания, семейные традиции и эмоционально тёплую '
                     'атмосферу.',
         'risk': 'Может слишком сильно привязываться, ждать постоянного подтверждения близости или болезненно '
                 'реагировать на холодность.'},
 'Лев': {'love': 'Любит ярко, щедро и заметно. Нужны внимание, восхищение, тёплая романтика и ощущение, что любовь '
                 'действительно особенная.',
         'pleasure': 'Удовольствие через творчество, красивые события, праздники, стиль, признание, сцену и '
                     'возможность проявить индивидуальность.',
         'risk': 'Может болезненно реагировать на отсутствие внимания или путать любовь с необходимостью постоянно '
                 'чувствовать себя особенной.'},
 'Дева': {'love': 'Любит через заботу, конкретную помощь, внимание к деталям и полезность. Чувства часто проявляются '
                  'делом, а не красивыми словами.',
          'pleasure': 'Удовольствие через порядок, качество, полезные привычки, здоровье, мастерство, повседневную '
                      'организованность и ощущение, что жизнь становится лучше.',
          'risk': 'Может быть слишком требовательной к себе и партнёру, анализировать чувства и считать, что '
                  'удовольствие нужно сначала заслужить.'},
 'Весы': {'love': 'Любит через взаимность, красивое общение, уважение, гармонию и ощущение равного партнёрства.',
          'pleasure': 'Удовольствие через эстетику, искусство, стиль, красивую среду, хорошие манеры, приятные '
                      'разговоры и гармоничные отношения.',
          'risk': 'Может слишком ориентироваться на реакцию другого и избегать неудобных разговоров ради внешнего '
                  'мира.'},
 'Скорпион': {'love': 'Любит глубоко и интенсивно. Нужны сильная привязанность, эмоциональная честность, верность, '
                      'страсть и ощущение особой связи.',
              'pleasure': 'Удовольствие через эмоциональную глубину, сильные переживания, интимность, тайну, '
                          'исследование, психологию и всё, что даёт ощущение настоящей вовлечённости.',
              'risk': 'Может усиливаться ревность, контроль, категоричность, страх потери и желание полностью владеть '
                      'вниманием партнёра.'},
 'Стрелец': {'love': 'Любит через свободу, интерес, рост и совместное расширение горизонтов. Партнёр должен '
                     'вдохновлять, а не ограничивать.',
             'pleasure': 'Удовольствие через путешествия, обучение, новые культуры, иностранную среду, большие идеи, '
                         'приключения и ощущение, что жизнь становится шире.',
             'risk': 'Может терять интерес, если отношения становятся слишком тесными, контролирующими или лишёнными '
                     'развития.'},
 'Козерог': {'love': 'Любит сдержанно, серьёзно и через надёжность. Важны ответственность, уважение, верность, '
                     'качество и ощущение, что отношения имеют будущее.',
             'pleasure': 'Удовольствие через качество, статус, долговечность, лаконичную эстетику, понятные формы и '
                         'вещи, которые имеют реальную ценность.',
             'risk': 'Может сдерживать чувства, избегать лишней романтизации и слишком долго проверять партнёра на '
                     'надёжность.'},
 'Водолей': {'love': 'Любит через дружбу, свободу, интеллектуальное родство и ощущение, что рядом можно оставаться '
                     'собой.',
             'pleasure': 'Удовольствие через друзей, сообщества, необычные идеи, технологии, новое, нестандартную '
                         'эстетику и ощущение свободы.',
             'risk': 'Может слишком дистанцироваться от эмоций или ставить свободу выше глубокой эмоциональной '
                     'включённости.'},
 'Рыбы': {'love': 'Любит тонко, романтично, эмпатично и с сильной эмоциональной вовлечённостью. Нужны вдохновение, '
                  'душевная близость и ощущение особой связи.',
          'pleasure': 'Удовольствие через музыку, кино, искусство, творчество, воду, мечты, атмосферу, романтику, '
                      'сострадание и тонкие эмоциональные переживания.',
          'risk': 'Может идеализировать партнёра, растворяться в отношениях или долго верить в образ вместо реального '
                  'человека.'}}
LOVE_VENUS_HOUSES = {1: 'Удовольствие и привлекательность связаны с образом, внешностью, личным стилем и свободой нравиться.',
 2: 'Сильна связь Венеры с деньгами, самоценностью, качеством вещей, комфортом и личными ресурсами.',
 3: 'Удовольствие приходит через общение, обучение, переписку, поездки, знания и приятное интеллектуальное окружение.',
 4: 'Важны красивый дом, уют, эмоциональная безопасность, семейная атмосфера и личное пространство.',
 5: 'Венера ярко проявляется через романтику, творчество, удовольствие, свидания, сцену, хобби и самовыражение.',
 6: 'Удовольствие связано с качеством повседневной жизни, работой, мастерством, заботой о теле, режиме и полезностью.',
 7: 'Венера сильно раскрывается через партнёрство, отношения, клиентов, взаимность и желание быть в союзе.',
 8: 'Любовь и удовольствие переживаются глубоко; важны интимность, доверие, страсть, общие ресурсы и эмоциональная '
    'интенсивность.',
 9: 'Удовольствие через путешествия, обучение, иностранную среду, новые культуры, мировоззрение и расширение '
    'горизонтов.',
 10: 'Венера влияет на публичный образ, профессию, статус, репутацию, эстетику карьеры и социальную привлекательность.',
 11: 'Удовольствие через друзей, сообщества, аудиторию, единомышленников, коллективные проекты и свободу.',
 12: 'Венера проживается более тонко и скрыто; важны уединение, искусство, фантазия, духовность, закулисные чувства и '
     'богатый мир переживаний.'}
LOVE_VENUS_ASPECTS = {'Сатурн': {'supportive': 'Добавляет серьёзность, верность, устойчивость, осторожность в выборе и способность строить '
                          'отношения надолго.',
            'tense': 'Может делать чувства более сдержанными, осторожными, холодноватыми снаружи; усиливать страх '
                     'отказа, высокие требования и трудность свободно принимать любовь.'},
 'Плутон': {'supportive': 'Добавляет глубину, сильное притяжение, сексуальную интенсивность и способность проживать '
                          'отношения очень трансформирующе.',
            'tense': 'Может усиливать ревность, контроль, страх потери, крайности, зависимость от сильных эмоций и '
                     'борьбу за власть.'},
 'Нептун': {'supportive': 'Добавляет романтичность, эмпатию, творческий вкус, музыкальность, воображение и тонкое '
                          'восприятие красоты.',
            'tense': 'Может усиливать идеализацию, размытые границы, склонность видеть не реального человека, а '
                     'желаемый образ.'},
 'Уран': {'supportive': 'Добавляет оригинальность вкуса, свободу, интерес к необычным людям и способность сохранять '
                        'свежесть в отношениях.',
          'tense': 'Может давать резкие перемены в симпатиях, страх скуки, потребность в большой свободе и '
                   'непредсказуемость в близости.'},
 'Марс': {'supportive': 'Усиливает сексуальность, инициативу, физическое притяжение и способность ясно показывать '
                        'желание.',
          'tense': 'Может усиливать страсть вместе с конфликтностью, резкостью, борьбой за инициативу и эмоциональной '
                   'импульсивностью.'},
 'Юпитер': {'supportive': 'Добавляет щедрость, оптимизм, удовольствие от жизни, широкий вкус и желание делиться '
                          'хорошим.',
            'tense': 'Может усиливать избыточность, большие ожидания, склонность переоценивать отношения или тратить '
                     'больше, чем комфортно.'},
 'Луна': {'supportive': 'Помогает чувствам, заботе и потребности в близости работать согласованно.',
          'tense': 'Может давать конфликт между тем, кого человек хочет, и тем, рядом с кем ему эмоционально '
                   'спокойно.'},
 'Солнце': {'supportive': 'Помогает легче соединять самовыражение, любовь, привлекательность и удовольствие.',
            'tense': 'Может создавать напряжение между тем, каким человек хочет быть, и тем, чего он ищет в любви и '
                     'удовольствии.'},
 'Меркурий': {'supportive': 'Добавляет лёгкость выражения симпатии, хороший вкус в словах, способность говорить о '
                            'чувствах и ценностях.',
              'tense': 'Может давать переанализ чувств, сомнения, трудность прямо говорить о желаниях или склонность '
                       'рационализировать любовь.'}}
LOVE_DESC = {'Овен': {'attracts': 'активные, прямые, самостоятельные, инициативные люди',
          'dynamic': 'Рядом с тобой партнёр может становиться более решительным, прямым и ориентированным на '
                     'собственные желания.'},
 'Телец': {'attracts': 'надёжные, спокойные, чувственные, практичные люди',
           'dynamic': 'В длительных отношениях в партнёре могут усиливаться спокойствие, бытовая устойчивость, '
                      'привязанность к комфорту и стабильности.'},
 'Близнецы': {'attracts': 'разговорчивые, интеллектуальные, подвижные, любознательные люди',
              'dynamic': 'Партнёр рядом с тобой может сильнее уходить в общение, обмен идеями, поездки, обучение и '
                         'потребность в разнообразии.'},
 'Рак': {'attracts': 'заботливые, домашние, эмоциональные, семейные люди',
         'dynamic': 'В партнёре могут сильнее проявляться домашность, опека, мягкость, семейность и желание '
                    'заботиться.'},
 'Лев': {'attracts': 'яркие, заметные, творческие, гордые люди',
         'dynamic': 'Рядом с тобой партнёр может становиться более заметным, требовать больше внимания, признания и '
                    'пространства для самовыражения.'},
 'Дева': {'attracts': 'практичные, полезные, собранные, внимательные к деталям люди',
          'dynamic': 'В партнёре могут усиливаться практичность, желание помогать, контролировать детали, улучшать быт '
                     'и наводить порядок.'},
 'Весы': {'attracts': 'приятные, дипломатичные, эстетичные, ориентированные на партнёрство люди',
          'dynamic': 'Партнёр рядом с тобой может сильнее стремиться к компромиссу, красивой форме отношений и '
                     'взаимности.'},
 'Скорпион': {'attracts': 'глубокие, сильные, закрытые, эмоционально интенсивные люди',
              'dynamic': 'В длительной связи в партнёре могут усиливаться глубина, ревность, контроль, страсть и '
                         'потребность в полной эмоциональной вовлечённости.'},
 'Стрелец': {'attracts': 'свободолюбивые, активные, образованные, ориентированные на развитие люди',
             'dynamic': 'Партнёр рядом с тобой может сильнее стремиться к свободе, путешествиям, обучению и расширению '
                        'своих горизонтов.'},
 'Козерог': {'attracts': 'серьёзные, взрослые, надёжные, амбициозные люди',
             'dynamic': 'В партнёре могут усиливаться ответственность, сдержанность, карьерная направленность и '
                        'желание строить отношения надолго.'},
 'Водолей': {'attracts': 'независимые, необычные, интеллектуальные, свободолюбивые люди',
             'dynamic': 'Партнёр рядом с тобой может сильнее уходить в роль друга, единомышленника, ценить свободу, '
                        'дистанцию и отношения без лишнего давления.'},
 'Рыбы': {'attracts': 'чувствительные, творческие, эмпатичные, тонкие люди',
          'dynamic': 'В партнёре могут сильнее проявляться мягкость, романтичность, мечтательность, эмоциональная '
                     'восприимчивость и потребность в принятии.'}}
LOVE_VENUS_DIGNITY = {'Телец': 'обитель',
 'Весы': 'обитель',
 'Рыбы': 'экзальтация',
 'Овен': 'изгнание',
 'Скорпион': 'изгнание',
 'Дева': 'падение'}
LOVE_VENUS_DIGNITY_MEANING = {'обитель': 'Венерианские темы обычно проживаются естественнее: человеку легче чувствовать вкус, ценность, '
            'удовольствие, симпатию и принимать приятное.',
 'экзальтация': 'Венерианская чувствительность может быть особенно тонкой, идеализированной, творческой или '
                'эмоционально богатой.',
 'изгнание': 'Любовь и удовольствие могут проживаться более напряжённо, резко или интенсивно. Человеку бывает сложнее '
             'просто принимать приятное без борьбы, крайностей или напряжения.',
 'падение': 'Может быть повышенная требовательность к себе и чувствам, склонность анализировать удовольствие и '
            'сомневаться в собственной ценности вместо простого принятия.'}
LOVE_PAID_TITLE = 'Твой сценарий любви и гармонии'
LOVE_PAID_POINTS = ['какой партнёр действительно подходит тебе для долгих отношений;',
 'что помогает сохранять близость, доверие и интерес к партнёру со временем;',
 'где твои уязвимые места в любви и что с ними делать, чтобы не разрушать отношения;',
 'как расслабляться, получать больше удовольствия от жизни и возвращать себе ощущение наполненности и внутренней '
 'гармонии.']

CAREER_SUN_SIGNS = {'Овен': {'talents': ['инициатива', 'решительность', 'скорость действий', 'конкурентность', 'самостоятельность'],
          'directions': ['предпринимательство',
                         'спорт',
                         'управление',
                         'экстренные и динамичные сферы',
                         'проекты, где важна скорость']},
 'Телец': {'talents': ['устойчивость',
                       'практичность',
                       'чувство качества',
                       'терпение',
                       'умение создавать материальную опору'],
           'directions': ['финансы',
                          'недвижимость',
                          'еда',
                          'красота',
                          'дизайн',
                          'производство',
                          'земля и материальные активы']},
 'Близнецы': {'talents': ['коммуникация', 'обучение', 'быстрое мышление', 'связи', 'работа с информацией'],
              'directions': ['медиа', 'продажи', 'маркетинг', 'обучение', 'языки', 'журналистика', 'контент']},
 'Рак': {'talents': ['эмпатия', 'забота', 'память', 'чувство атмосферы', 'работа с людьми и пространством'],
         'directions': ['психология', 'недвижимость', 'гостеприимство', 'семейные проекты', 'еда', 'забота и сервис']},
 'Лев': {'talents': ['творчество', 'самопрезентация', 'лидерство', 'умение привлекать внимание', 'работа с детьми'],
         'directions': ['сцена',
                        'медиа',
                        'личный бренд',
                        'ивенты',
                        'детские проекты',
                        'творческий бизнес',
                        'управление']},
 'Дева': {'talents': ['аналитика', 'систематизация', 'поиск ошибок', 'мастерство', 'качество', 'точность'],
          'directions': ['аналитика',
                         'бухгалтерия',
                         'медицина',
                         'здоровье',
                         'контроль качества',
                         'обучение',
                         'сервис']},
 'Весы': {'talents': ['переговоры', 'партнёрство', 'эстетика', 'баланс интересов', 'дипломатия'],
          'directions': ['право', 'медиация', 'дизайн', 'красота', 'работа с клиентами', 'партнёрские проекты']},
 'Скорпион': {'talents': ['глубина', 'кризисоустойчивость', 'исследование', 'психологическое чутьё', 'работа с риском'],
              'directions': ['психология',
                             'хирургия',
                             'кризисные службы',
                             'силовые структуры',
                             'финансы',
                             'банки',
                             'расследования']},
 'Стрелец': {'talents': ['масштаб мышления',
                         'обучение',
                         'международность',
                         'видение перспективы',
                         'вдохновение других'],
             'directions': ['образование',
                            'международные проекты',
                            'туризм',
                            'право',
                            'издательское дело',
                            'консалтинг']},
 'Козерог': {'talents': ['управление',
                         'дисциплина',
                         'стратегия',
                         'ответственность',
                         'способность строить долгий результат'],
             'directions': ['менеджмент',
                            'корпоративная среда',
                            'госструктуры',
                            'строительство',
                            'финансы',
                            'управление проектами']},
 'Водолей': {'talents': ['нестандартное мышление',
                         'технологичность',
                         'реформаторство',
                         'работа с сообществами',
                         'инновации'],
             'directions': ['IT',
                            'технологии',
                            'стартапы',
                            'наука',
                            'сообщества',
                            'онлайн-проекты',
                            'инновационные продукты']},
 'Рыбы': {'talents': ['эмпатия', 'интуиция', 'воображение', 'творчество', 'тонкое восприятие людей и атмосферы'],
          'directions': ['психология',
                         'музыка',
                         'кино',
                         'искусство',
                         'помогающие профессии',
                         'закулисная работа',
                         'медицина']}}
CAREER_SUN_HOUSES = {1: 'талант проявляется через личную инициативу, образ, самостоятельность и прямое влияние',
 2: 'талант тесно связан с деньгами, ценностью, ресурсами, торговлей и материальным результатом',
 3: 'талант проявляется через речь, обучение, информацию, письмо, связи и поездки',
 4: 'талант может раскрываться через дом, семью, недвижимость, ощущение опоры и работу из личного пространства',
 5: 'талант проявляется через творчество, сцену, детей, проекты, развлечения и личный бренд',
 6: 'талант раскрывается через мастерство, полезность, систематизацию, качество, сервис и ежедневную практику',
 7: 'талант проявляется через партнёрства, клиентов, переговоры и работу один на один',
 8: 'талант связан с кризисами, трансформацией, риском, психологией, общими ресурсами и глубокой аналитикой',
 9: 'талант проявляется через высшее знание, обучение, преподавание, международную среду и расширение горизонтов',
 10: 'талант стремится стать карьерой, статусом, управлением и видимым общественным результатом',
 11: 'талант раскрывается через сообщества, аудиторию, технологии, друзей и коллективные проекты',
 12: 'талант может быть сильнее в закулисной, закрытой, исследовательской, творческой, помогающей или удалённой работе'}
CAREER_SECOND_HOUSE_PLANETS = {'Солнце': 'Деньги и личная ценность могут быть сильно связаны с самоощущением. Важно самому влиять на уровень дохода '
           'и чувствовать гордость за созданный ресурс.',
 'Луна': 'Финансовая стабильность сильно влияет на эмоциональное состояние. Доходы и траты могут быть чувствительны к '
         'настроению, семье и ощущению безопасности.',
 'Меркурий': 'Деньги могут быть связаны с торговлей, коммуникацией, обучением, информацией, переговорами, '
             'посредничеством.',
 'Венера': 'Деньги могут быть связаны с красотой, эстетикой, отношениями, клиентами, комфортом, качеством, искусством '
           'и ценностью.',
 'Марс': 'Человек активно добывает ресурсы, быстро включается при финансовой мотивации, может действовать конкурентно, '
         'напористо и предпринимательски.',
 'Юпитер': 'Тема денег склонна к расширению: большие амбиции, крупные возможности, обучение, международность, масштаб.',
 'Сатурн': 'Деньги требуют структуры, дисциплины и времени. Возможны задержки, страх нехватки, но и способность '
           'строить устойчивую систему.',
 'Уран': 'Доход может быть нестандартным, проектным, технологичным или нестабильным. Сильна тема свободы, инноваций и '
         'неожиданных финансовых поворотов.',
 'Нептун': 'Возможны размытые финансовые границы, недооценка своей работы или утечки денег. При поддержке карты доход '
           'может идти через творчество, воду, музыку, кино, помощь, медицину, духовные и закулисные сферы.',
 'Плутон': 'Сильная интенсивность в теме денег и ресурсов. Возможны крупные финансовые амбиции, работа с властью, '
           'кризисами, чужими ресурсами, банками, инвестициями, трансформацией.'}
CAREER_SIXTH_HOUSE_PLANETS = {'Солнце': 'реализация через мастерство, полезность, качество, профессиональную компетентность и ежедневную работу',
 'Луна': 'эмоциональная включённость в работу, заботу, сервис и повседневные процессы',
 'Меркурий': 'аналитика, документы, обучение, информация, расчёты, коммуникация в ежедневной работе',
 'Венера': 'приятная рабочая среда, эстетика, клиентский сервис, красота, забота, качество',
 'Марс': 'высокая рабочая активность, скорость, конкуренция, техническая или физически активная работа',
 'Юпитер': 'рост через навыки, обучение, сервис, масштабирование рабочих процессов',
 'Сатурн': 'дисциплина, нагрузка, профессиональная ответственность, системность, мастерство через долгую практику',
 'Уран': 'нестандартный график, технологии, свобода, проектная работа, инновационные процессы',
 'Нептун': 'помощь, медицина, творчество, закрытые учреждения, размытые рабочие границы',
 'Плутон': 'интенсивная работа, контроль, кризисные процессы, сложные системы, трансформация'}
CAREER_TENTH_HOUSE_PLANETS = {'Солнце': 'сильная потребность в признании, статусе, видимой реализации и личном влиянии',
 'Луна': 'карьера может быть эмоционально значимой и связанной с публикой, людьми, заботой, изменчивыми задачами',
 'Меркурий': 'карьера через информацию, обучение, коммуникацию, продажи, управление потоками данных',
 'Венера': 'карьера через эстетику, отношения, клиентов, публичный образ, красоту, искусство или дипломатичность',
 'Марс': 'сильная карьерная активность, конкуренция, управление, инициативность, иногда конфликтность',
 'Юпитер': 'стремление к масштабу, авторитету, росту, преподаванию, международной или статусной реализации',
 'Сатурн': 'амбиции, ответственность, управление, долгий карьерный путь, ориентация на статус и систему',
 'Уран': 'нестандартная карьера, технологии, свобода, резкие повороты, инновации',
 'Нептун': 'творчество, помощь, медицина, кино, музыка, благотворительность, закулисные или размытые карьерные '
           'траектории',
 'Плутон': 'власть, влияние, кризисное управление, крупные ресурсы, банки, политика, трансформационные сферы'}
CAREER_PAID_TITLE = 'Как увеличить свой доход'
CAREER_PAID_POINTS = ['где прячется твой финансовый ресурс;',
 'через что к тебе легче приходят деньги;',
 'что лучше всего монетизируется именно у тебя;',
 'куда уходят деньги и что мешает тебе зарабатывать больше.']

NODE_SIGN_SYMBOLISM = {'Овен': {'themes': ['инициатива', 'смелость', 'самостоятельность', 'борьба', 'спорт', 'конкуренция', 'скорость'],
          'south': 'Знакомый опыт самостоятельности, борьбы, быстрых решений и привычки действовать первым.',
          'north': 'Развитие через смелость, самостоятельные решения, инициативу и умение обозначать свои желания.',
          'teachers': 'Овны, люди с сильным Марсом, активные, прямые и самостоятельные люди.'},
 'Телец': {'themes': ['деньги', 'материальные ценности', 'еда', 'комфорт', 'тело', 'стабильность', 'качество'],
           'south': 'Знакомый опыт создания материальной опоры, комфорта, стабильности и удержания ресурсов.',
           'north': 'Развитие через устойчивость, финансовую опору, собственную ценность, телесность и качество.',
           'teachers': 'Тельцы, люди с сильной Венерой, практичные и устойчивые люди.'},
 'Близнецы': {'themes': ['общение', 'языки', 'обучение', 'информация', 'письмо', 'переговоры', 'поездки', 'медиа'],
              'south': 'Знакомый опыт общения, обучения, посредничества, языков и работы с информацией.',
              'north': 'Развитие через речь, письмо, языки, обучение, вопросы и передачу знаний.',
              'teachers': 'Близнецы, люди с сильным Меркурием, преподаватели, переводчики и коммуникаторы.'},
 'Рак': {'themes': ['дом',
                    'семья',
                    'быт',
                    'родительство',
                    'забота',
                    'питание',
                    'уют',
                    'эмоциональная поддержка',
                    'род'],
         'south': 'Знакомый опыт дома, семьи, заботы, родительства, быта, эмоциональной включённости, приготовления '
                  'пищи, создания уюта и безопасной атмосферы.',
         'north': 'Развитие через заботу, эмоциональную близость, дом, семью и способность принимать поддержку.',
         'teachers': 'Раки, люди с сильной Луной, заботливые, семейные и эмоционально включённые люди.'},
 'Лев': {'themes': ['сцена', 'творчество', 'дети', 'праздники', 'лидерство', 'самовыражение', 'внимание', 'признание'],
         'south': 'Знакомый опыт личного проявления, лидерства, творчества, внимания и признания.',
         'north': 'Развитие через смелое самовыражение, творчество, лидерство и право быть заметным.',
         'teachers': 'Львы, люди с сильным Солнцем, яркие, творческие и лидерские личности.'},
 'Дева': {'themes': ['работа',
                     'мастерство',
                     'анализ',
                     'здоровье',
                     'медицина',
                     'сервис',
                     'животные',
                     'режим',
                     'порядок'],
          'south': 'Знакомый опыт труда, анализа, служения, здоровья, быта, режима и профессионального мастерства.',
          'north': 'Развитие через дисциплину повседневности, навык, полезность, здоровье и практическую '
                   'компетентность.',
          'teachers': 'Девы, люди с сильным Меркурием, специалисты, мастера, аналитики и врачи.'},
 'Весы': {'themes': ['партнёрство',
                     'консультации',
                     'эстетика',
                     'красота',
                     'право',
                     'переговоры',
                     'баланс',
                     'дипломатия'],
          'south': 'Знакомый опыт партнёрства, компромиссов, консультаций, эстетики, права и дипломатии.',
          'north': 'Развитие через партнёрство, баланс интересов, переговоры, эстетику и работу один на один.',
          'teachers': 'Весы, люди с сильной Венерой, дипломаты, консультанты и юристы.'},
 'Скорпион': {'themes': ['кризисы',
                         'трансформация',
                         'психология',
                         'секс',
                         'власть',
                         'чужие ресурсы',
                         'банки',
                         'инвестиции',
                         'хирургия'],
              'south': 'Знакомый опыт кризисов, глубины, психологической интенсивности, власти, риска и чужих '
                       'ресурсов.',
              'north': 'Развитие через глубину, трансформацию, кризисоустойчивость, влияние, риск и общие ресурсы.',
              'teachers': 'Скорпионы, люди с сильным Плутоном или Марсом, кризисные и трансформирующие личности.'},
 'Стрелец': {'themes': ['высшее образование',
                        'путешествия',
                        'иностранцы',
                        'религия',
                        'философия',
                        'право',
                        'преподавание'],
             'south': 'Знакомый опыт больших знаний, путешествий, иностранных культур, преподавания и сильных '
                      'убеждений.',
             'north': 'Развитие через образование, путешествия, преподавание, международную среду и расширение '
                      'горизонтов.',
             'teachers': 'Стрельцы, люди с сильным Юпитером, преподаватели, иностранцы и путешественники.'},
 'Козерог': {'themes': ['работа',
                        'труд',
                        'упорство',
                        'взрослость',
                        'ответственность',
                        'структура',
                        'статус',
                        'карьера',
                        'дисциплина'],
             'south': 'Знакомый опыт ответственности, труда, дисциплины, управления, статуса и необходимости быть '
                      'взрослым.',
             'north': 'Развитие через упорство, работу, дисциплину, профессионализм, ответственность и долгий '
                      'результат.',
             'teachers': 'Козероги, люди с сильным Сатурном, старшие, руководители, строгие и профессиональные люди.'},
 'Водолей': {'themes': ['друзья', 'сообщества', 'астрология', 'технологии', 'IT', 'инновации', 'свобода', 'будущее'],
             'south': 'Знакомый опыт сообществ, друзей, технологий, нестандартных идей и свободы.',
             'north': 'Развитие через сообщества, технологии, астрологию, IT, инновации и свободу мышления.',
             'teachers': 'Водолеи, люди с сильным Ураном, новаторы, программисты, астрологи и свободомыслящие люди.'},
 'Рыбы': {'themes': ['музыка',
                     'творчество',
                     'кино',
                     'психология',
                     'эмпатия',
                     'помощь',
                     'духовность',
                     'вода',
                     'интуиция'],
          'south': 'Знакомый опыт творчества, эмпатии, интуиции, помощи, музыки, воды или жизни за кадром.',
          'north': 'Развитие через творчество, интуицию, сострадание, музыку, психологию, помощь и тонкие смыслы.',
          'teachers': 'Рыбы, люди с сильным Нептуном, творческие, эмпатичные и чувствительные люди.'}}
NODE_HOUSES_FULL = {1: 'личность, самостоятельность, образ, тело, право действовать от себя',
 2: 'деньги, имущество, личные ресурсы, ценность, материальная опора',
 3: 'обучение, речь, языки, письмо, информация, связи, поездки, документы',
 4: 'дом, семья, родители, род, недвижимость, личное пространство',
 5: 'творчество, дети, любовь, сцена, хобби, самовыражение, удовольствие',
 6: 'работа, здоровье, мастерство, служба, ежедневные навыки, режим',
 7: 'партнёрства, брак, клиенты, договоры, взаимодействие один на один',
 8: 'кризисы, интимность, чужие деньги, наследство, банки, инвестиции, трансформация',
 9: 'высшее образование, путешествия, иностранцы, религия, философия, преподавание',
 10: 'карьера, статус, управление, общественная роль, достижения',
 11: 'друзья, сообщества, аудитория, технологии, проекты, будущее',
 12: 'уединение, закулисье, творчество, духовность, закрытые учреждения, помощь, скрытые процессы'}
NODE_PLANETS_FULL = {'Солнце': {'south': 'Знакомый опыт личной силы, лидерства и самовыражения.',
            'north': 'Важно развивать уверенность, творчество, самостоятельное проявление и право занимать своё '
                     'место.'},
 'Луна': {'south': 'Знакомый эмоциональный опыт, семья, забота, материнство и привычные реакции.',
          'north': 'Развитие через эмоциональную зрелость, заботу, контакт с чувствами, дом и семью.'},
 'Меркурий': {'south': 'Знакомый опыт обучения, письма, языков, торговли, переговоров и информации.',
              'north': 'Важно развивать речь, письмо, языки, обучение, аналитику и передачу знаний.'},
 'Венера': {'south': 'Знакомый опыт любви, эстетики, партнёрства, красоты, удовольствия и материальных тем.',
            'north': 'Развитие через отношения, красоту, вкус, гармонию, любовь и собственную ценность.'},
 'Марс': {'south': 'Знакомый опыт борьбы, действия, конкуренции, риска и необходимости быстро реагировать.',
          'north': 'Важно развивать решительность, инициативу, смелость и способность защищать свои интересы.'},
 'Юпитер': {'south': 'Знакомый опыт образования, преподавания, путешествий, веры, закона и расширения горизонтов.',
            'north': 'Развитие через образование, преподавание, международность, право, путешествия и масштаб.'},
 'Сатурн': {'south': 'Знакомый опыт труда, ограничений, ответственности, статуса, дисциплины и управления.',
            'north': 'Важно развивать дисциплину, терпение, структуру, ответственность и профессионализм.'},
 'Уран': {'south': 'Знакомый опыт нестандартности, свободы, технологий, сообществ, астрологии и резких перемен.',
          'north': 'Развитие через нестандартное мышление, технологии, IT, астрологию, программирование и инновации.'},
 'Нептун': {'south': 'Знакомый опыт творчества, музыки, тонкого восприятия, духовности, помощи, воды или изоляции.',
            'north': 'Развитие через творчество, музыку, образное мышление, интуицию, эмпатию и тонкие смыслы.'},
 'Плутон': {'south': 'Знакомый опыт кризисов, власти, контроля, трансформации, риска и чужих ресурсов.',
            'north': 'Развитие через психологическую силу, трансформацию, кризисоустойчивость, влияние и глубокие '
                     'изменения.'}}
NODE_SIGN_RULERS = {'Овен': 'Марс',
 'Телец': 'Венера',
 'Близнецы': 'Меркурий',
 'Рак': 'Луна',
 'Лев': 'Солнце',
 'Дева': 'Меркурий',
 'Весы': 'Венера',
 'Скорпион': 'Плутон',
 'Стрелец': 'Юпитер',
 'Козерог': 'Сатурн',
 'Водолей': 'Уран',
 'Рыбы': 'Нептун'}
NODE_PAID_TITLE = 'Как пройти свои кармические уроки'
NODE_PAID_POINTS = ['какие качества тебе важно развивать в первую очередь;',
 'что из привычного опыта помогает тебе, а что удерживает на месте;',
 'через какие действия и жизненные выборы ты двигаешься к своей задаче;',
 'как проходить повторяющиеся кармические уроки и не возвращаться в один и тот же сценарий.']

GROWTH_DIGNITY_WEAKNESSES = {'Солнце': {'fall': 'Весы',
            'detriment': 'Водолей',
            'function': 'самовыражение, воля, уверенность, право быть собой и занимать место'},
 'Луна': {'fall': 'Скорпион',
          'detriment': 'Козерог',
          'function': 'эмоциональная безопасность, чувства, привычные реакции, способность принимать заботу'},
 'Меркурий': {'fall': 'Рыбы',
              'detriment': 'Стрелец',
              'function': 'мышление, речь, логика, обучение, обработка информации'},
 'Венера': {'fall': 'Дева',
            'detriment': ['Овен', 'Скорпион'],
            'function': 'любовь, удовольствие, вкус, мягкость, самоценность, способность принимать'},
 'Марс': {'fall': 'Рак',
          'detriment': ['Телец', 'Весы'],
          'function': 'действие, инициатива, злость, границы, способность добиваться'},
 'Юпитер': {'fall': 'Козерог',
            'detriment': ['Близнецы', 'Дева'],
            'function': 'рост, вера, масштаб, образование, расширение возможностей'},
 'Сатурн': {'fall': 'Овен',
            'detriment': ['Рак', 'Лев'],
            'function': 'структура, границы, ответственность, дисциплина, зрелость'}}
GROWTH_OPPOSITES = {'Овен': 'Весы',
 'Телец': 'Скорпион',
 'Близнецы': 'Стрелец',
 'Рак': 'Козерог',
 'Лев': 'Водолей',
 'Дева': 'Рыбы',
 'Весы': 'Овен',
 'Скорпион': 'Телец',
 'Стрелец': 'Близнецы',
 'Козерог': 'Рак',
 'Водолей': 'Лев',
 'Рыбы': 'Дева'}
GROWTH_BALANCE_QUALITIES = {'Овен': ['инициатива', 'движение', 'спорт', 'смелость', 'прямота', 'адреналин', 'право действовать'],
 'Телец': ['телесность',
           'спокойствие',
           'комфорт',
           'удовольствие',
           'качество',
           'ритм',
           'самоценность',
           'материальная опора'],
 'Близнецы': ['гибкость', 'вопросы', 'обмен', 'лёгкость', 'обучение', 'разговор', 'несколько точек зрения'],
 'Рак': ['забота', 'дом', 'мягкость', 'эмоциональная близость', 'принятие чувств', 'умение принимать поддержку'],
 'Лев': ['тепло', 'самовыражение', 'творчество', 'яркость', 'удовольствие от внимания', 'щедрость', 'сердечность'],
 'Дева': ['структура повседневности', 'конкретика', 'режим', 'практичность', 'навык', 'анализ', 'маленькие шаги'],
 'Весы': ['мягкость', 'женственность', 'чувство меры', 'эстетика', 'дипломатия', 'партнёрство', 'уместность', 'баланс'],
 'Скорпион': ['глубина',
              'честность',
              'трансформация',
              'сексуальность',
              'способность выдерживать сильные чувства',
              'кризисоустойчивость'],
 'Стрелец': ['масштаб', 'смысл', 'перспектива', 'юмор', 'обучение', 'путешествия', 'вера в развитие'],
 'Козерог': ['структура', 'границы', 'ответственность', 'дисциплина', 'взрослость', 'долгий результат', 'реализм'],
 'Водолей': ['дистанция', 'свобода', 'друзья', 'интеллект', 'новизна', 'необычный взгляд', 'независимость'],
 'Рыбы': ['доверие', 'интуиция', 'творчество', 'эмпатия', 'отпускание контроля', 'воображение', 'восстановление']}
GROWTH_PLANET_LOGIC = {'Солнце': {'question': 'Что мешает тебе проявляться, чувствовать уверенность и занимать своё место?',
            'resource': 'сильное Солнце даёт ощущение опоры, волю, жизненность, творчество и ясное ощущение себя',
            'paid_focus': 'Показать, где человек гасит себя, зависит от чужой оценки или не разрешает себе быть '
                          'заметным. Дать способы проявлять Солнце безопасно и уверенно.'},
 'Луна': {'question': 'Что нарушает твоё чувство безопасности и как ты реагируешь эмоционально?',
          'resource': 'гармоничная Луна даёт эмоциональную устойчивость, контакт с собой, способность '
                      'восстанавливаться и принимать заботу',
          'paid_focus': 'Показать триггеры, автоматические эмоциональные реакции и способы возвращения в комфорт через '
                        'качества противоположного знака и реальные бытовые действия.'},
 'Меркурий': {'question': 'Что мешает мыслить ясно, говорить прямо и доверять собственным выводам?',
              'resource': 'гармоничный Меркурий даёт ясность, гибкость мышления, обучаемость и точную коммуникацию',
              'paid_focus': 'Показать, как уменьшать путаницу, сомнения, перегрузку, резкость или ментальную '
                            'фиксацию.'},
 'Венера': {'question': 'Что мешает получать удовольствие, чувствовать свою ценность и строить гармоничные отношения?',
            'resource': 'гармоничная Венера даёт вкус, удовольствие, самоценность, способность принимать любовь и '
                        'создавать красоту',
            'paid_focus': 'Показать, как снизить ревность, крайности, холодность, самокритику или чрезмерную резкость '
                          'и добавить качества противоположного знака без потери природной привлекательности.'},
 'Марс': {'question': 'Что мешает действовать, отстаивать себя и безопасно выражать злость?',
          'resource': 'гармоничный Марс даёт инициативу, смелость, здоровые границы, скорость и способность добиваться',
          'paid_focus': 'Показать безопасный выход энергии через движение, спорт, действия, границы и подходящий '
                        'ритм.'}}
GROWTH_BALANCE_EXAMPLES = {'venus_scorpio': {'tension': 'Венера в Скорпионе может проживать любовь через крайность, сильную привязанность, '
                              'ревность, проверки, драму и желание контролировать глубину связи.',
                   'keep': 'Не подавлять глубину, страсть, сексуальность, эмоциональную честность и способность любить '
                           'очень сильно.',
                   'balance': 'Добавлять качества Тельца: устойчивость, телесность, спокойствие, простые удовольствия, '
                              'качество жизни, чувство собственной ценности и предсказуемость.',
                   'actions': ['укреплять телесный комфорт и бытовую устойчивость',
                               'не проверять любовь через конфликт',
                               'возвращать внимание к собственным удовольствиям и ценности',
                               'строить близость через стабильность, а не только через интенсивность']},
 'moon_capricorn': {'tension': 'Луна в Козероге может слишком быстро брать себя в руки, запрещать себе слабость и '
                               'воспринимать чувства как то, что нужно контролировать.',
                    'keep': 'Сохранять собранность, ответственность, стойкость характера и способность выдерживать '
                            'сложные периоды.',
                    'balance': 'Добавлять качества Рака: мягкость, заботу, дом, эмоциональную близость, право '
                               'чувствовать и способность принимать поддержку.',
                    'actions': ['говорить о чувствах до момента перегруза',
                                'создавать дома пространство эмоционального восстановления',
                                'просить о помощи, а не только быть сильным для других',
                                'разрешать себе отдых без чувства вины']},
 'venus_aries': {'tension': 'Венера в Овне может проявляться слишком резко, импульсивно или ярко, когда не хватает '
                            'чувства меры и мягкости.',
                 'keep': 'Сохранять смелость, инициативу, сексуальность, яркость, драйв и способность первой проявлять '
                         'симпатию.',
                 'balance': 'Добавлять качества Весов: мягкость, женственность, чувство меры, эстетику, уместность, '
                            'дипломатию и партнёрский баланс.',
                 'actions': ['давать огненной энергии выход через спорт и движение',
                             'использовать яркость дозированно: акцент, красная помада, смелая деталь',
                             'учиться не только завоёвывать, но и слышать партнёра',
                             'сочетать дерзость с элегантностью']},
 'moon_aquarius': {'tension': 'Луна в Водолее может уходить в дистанцию, рационализацию и попытку понять чувства '
                              'вместо того, чтобы прожить их.',
                   'keep': 'Сохранять независимость, интеллектуальную свободу, способность видеть ситуацию со стороны '
                           'и не тонуть в эмоциях.',
                   'balance': 'Добавлять качества Льва: тепло, сердечность, творчество, открытое проявление чувств и '
                              'право быть заметным.',
                   'actions': ['не только анализировать эмоцию, но и называть её',
                               'добавлять творчество и живое самовыражение',
                               'не исчезать из контакта при сильных чувствах',
                               'разрешать себе тепло и внимание']}}
GROWTH_ASPECT_PATTERNS = {'sun_saturn': {'tension': 'страх ошибки, жёсткая строгая самооценка, ощущение, что признание нужно заслужить',
                'resource': 'дисциплина, зрелость, способность строить результат надолго'},
 'sun_pluto': {'tension': 'борьба за контроль, крайняя реакция на давление, кризисы самоощущения',
               'resource': 'огромная воля, способность трансформироваться и влиять'},
 'sun_uranus': {'tension': 'резкий протест против ограничений, импульсивные разрывы, непереносимость контроля',
                'resource': 'независимость, оригинальность, способность идти своим путём'},
 'sun_neptune': {'tension': 'сомнения в себе, идеализация, размытая цель, уход от прямого действия',
                 'resource': 'воображение, эмпатия, творчество, тонкое восприятие'},
 'moon_saturn': {'tension': 'сложно показывать слабость, просить поддержку и расслабляться эмоционально',
                 'resource': 'эмоциональная выносливость, надёжность, способность держать других'},
 'moon_pluto': {'tension': 'крайние переживания, фиксация, контроль, страх потери, сильные эмоциональные реакции',
                'resource': 'психологическая глубина, интуиция, кризисоустойчивость'},
 'moon_uranus': {'tension': 'эмоциональная резкость, скачки состояния, потребность резко дистанцироваться',
                 'resource': 'быстрая перестройка, независимость, эмоциональная оригинальность'},
 'moon_neptune': {'tension': 'идеализация, эмоциональная путаница, сильная впечатлительность',
                  'resource': 'эмпатия, интуиция, музыкальность, воображение'},
 'venus_saturn': {'tension': 'страх отвержения, сдержанность, ощущение, что любовь нужно заслужить',
                  'resource': 'верность, серьёзность, способность строить долгие отношения'},
 'venus_pluto': {'tension': 'ревность, контроль, эмоциональные крайности, страх потери',
                 'resource': 'магнетизм, глубина чувств, сильная сексуальность, способность к глубокой близости'},
 'venus_uranus': {'tension': 'страх скуки, резкие изменения в симпатиях, потребность в большой свободе',
                  'resource': 'оригинальный вкус, необычная привлекательность, способность строить нестандартные '
                              'отношения'},
 'venus_neptune': {'tension': 'идеализация, размытые границы, влюблённость в образ',
                   'resource': 'романтичность, искусство, эмпатия, тонкий вкус'},
 'mars_saturn': {'tension': 'ощущение заблокированного действия, подавленная злость, движение через сопротивление',
                 'resource': 'выносливость, дисциплина, способность работать на длинной дистанции'},
 'mars_pluto': {'tension': 'силовая борьба, крайняя реакция на давление, накопленная агрессия',
                'resource': 'огромная энергия, смелость, способность действовать в кризисе'},
 'mars_uranus': {'tension': 'импульсивность, резкие действия, вспышки раздражения',
                 'resource': 'быстрота, смелость, инновационность, способность моментально реагировать'},
 'mercury_neptune': {'tension': 'путаница, сомнения, ментальная перегрузка, склонность додумывать',
                     'resource': 'образное мышление, интуиция, творчество, способность чувствовать подтекст'},
 'mercury_saturn': {'tension': 'страх сказать неправильно, чрезмерный самоконтроль речи, тяжёлое обдумывание',
                    'resource': 'серьёзность мышления, точность, способность глубоко учиться'},
 'mercury_pluto': {'tension': 'ментальная фиксация, подозрительность, желание докопаться до всего любой ценой',
                   'resource': 'исследовательский ум, способность видеть скрытое и глубоко анализировать'}}
GROWTH_PAID_TITLE = 'Как превратить слабые места в силу'
GROWTH_PAID_POINTS = ['как снизить напряжение по своим самым сложным планетам;',
 'какие качества помогают гармонизировать их, не подавляя себя;',
 'как перестать повторять разрушительный сценарий и дать энергии безопасный выход;',
 'как превратить напряжённые аспекты в ресурс, силу и новые возможности.']


def _bullets(title, points):
    cleaned=[]
    for p in points:
        p=p.strip().rstrip(";").rstrip(".")
        cleaned.append("• " + p + ".")
    return f"\n\n🔒 {title}\n\n" + "\n".join(cleaned)

def _point_sign_text(point):
    return SIGN_PREP.get(point.sign, point.sign)

def _aspect_nature(row):
    return "tense" if row.get("aspect") in TENSE else "supportive"

def _first_meaningful_aspects(c, planet, limit=3, max_orb=6.0):
    rows=[]
    for row in _aspect_rows(c, planet, False, max_orb):
        other=row.get("planet")
        if other and other != planet:
            rows.append(row)
    return rows[:limit]

def _planets_in_house(c, house):
    return [n for n,h in c.get("houses",{}).items()
            if isinstance(h,int) and h==house and n in c.get("planets",{})]

def _stellium_houses(c):
    result={}
    for h in range(1,13):
        names=[n for n in _planets_in_house(c,h)
               if n not in ("Северный узел","Южный узел")]
        if len(names)>=3:
            result[h]=names
    return result

def _ruled_houses(c, planet):
    rows=c.get("ruled_houses",{}).get(planet,[]) or []
    out=[]
    for row in rows:
        if isinstance(row,(list,tuple)) and row:
            out.append(int(row[0]))
        elif isinstance(row,int):
            out.append(row)
    return sorted(set(out))


PLANET_ASPECT_SYMBOLISM = {
    "Солнце": {
        "supportive": "усиливает уверенность в себе, цельность, жизненную силу и способность проявляться",
        "tense": "может создавать личный конфликт вокруг самооценки, воли и права проявляться так, как хочется",
    },
    "Луна": {
        "supportive": "помогает лучше понимать свои чувства, восстанавливаться и чувствовать эмоциональную устойчивость",
        "tense": "может усиливать эмоциональные качели, чувствительность и сложность быстро возвращаться в спокойное состояние",
    },
    "Меркурий": {
        "supportive": "усиливает мышление, речь, обучаемость, способность договариваться и быстро связывать информацию",
        "tense": "может давать переанализ, сомнения, нервное напряжение в мыслях и сложности с ясной коммуникацией",
    },
    "Венера": {
        "supportive": "усиливает чувство вкуса, привлекательность, способность нравиться, строить отношения и получать удовольствие",
        "tense": "может давать сложности в отношениях, самоценности, выборе партнёра и способности спокойно получать удовольствие",
    },
    "Марс": {
        "supportive": "смелость, инициативу, скорость действий и способность защищать свои интересы",
        "tense": "может давать импульсивность, раздражительность, конфликты, борьбу за контроль и перегрузку от постоянного напряжения",
    },
    "Юпитер": {
        "supportive": "широкий взгляд, веру в возможности, сильные амбиции, тягу к росту, большим целям, обучению и расширению горизонтов",
        "tense": "может раскачивать крайности, завышать ожидания, провоцировать переоценку возможностей и стремление брать на себя больше, чем реально комфортно",
    },
    "Сатурн": {
        "supportive": "выдержку, дисциплину, ответственность, серьёзность и способность строить результат надолго",
        "tense": "может создавать страх ошибки, излишнюю строгость к себе, ощущение ограничений, задержек и необходимости многое доказывать через усилие",
    },
    "Уран": {
        "supportive": "оригинальность, независимость, быстрые инсайты, интерес к новому и способность находить нестандартные решения",
        "tense": "может давать резкость, непредсказуемость, резкий протест против ограничений и сложности выдерживать рутину",
    },
    "Нептун": {
        "supportive": "усиливает интуицию, воображение, эмпатию, творческое мышление и тонкое восприятие людей и атмосферы",
        "tense": "может давать идеализацию, размытые границы, сомнения, уход в фантазии и сложность сразу видеть ситуацию реалистично",
    },
    "Плутон": {
        "supportive": "психологическую глубину, сильную волю, способность проходить кризисы и влиять на сложные процессы",
        "tense": "может усиливать контроль, крайности, фиксацию, борьбу за власть и болезненную реакцию на давление",
    },
}

ASPECT_TONE = {
    "тригон":"supportive",
    "секстиль":"supportive",
    "соединение":"mixed",
    "квадрат":"tense",
    "оппозиция":"tense",
}

def aspect_effect_text(row):
    other = row.get("planet")
    aspect = row.get("aspect","")
    tone = ASPECT_TONE.get(aspect, "mixed")
    rules = PLANET_ASPECT_SYMBOLISM.get(other, {})
    if tone == "mixed":
        # Conjunction intensifies the planet: use both potential and caution in plain language.
        pos = rules.get("supportive","усиливает качества этой планеты")
        neg = rules.get("tense","может делать её проявление более интенсивным")
        return f"соединение {planet_phrase_with(other)} усиливает влияние этой планеты: {pos}; при перегрузе {neg}"
    meaning = rules.get(tone)
    if not meaning:
        return f"{aspect} {planet_phrase_with(other)} заметно окрашивает эту сферу"
    return f"{aspect} {planet_phrase_with(other)} {meaning}"


def _planet_aspect_sentence(c, planet, domain="общую тему"):
    rows = _first_meaningful_aspects(c, planet, 2, 5.5)
    if not rows:
        return ""

    meanings = []
    for row in rows:
        other = row.get("planet")
        aspect = row.get("aspect", "")
        tone = ASPECT_TONE.get(aspect, "mixed")
        rules = PLANET_ASPECT_SYMBOLISM.get(other, {})

        if tone == "supportive":
            meaning = rules.get("supportive")
            if meaning:
                meanings.append(
                    f"У тебя заметно проявлена связь с {planet_instr(other)}. "
                    f"Она даёт {meaning}."
                )
        elif tone == "tense":
            meaning = rules.get("tense")
            if meaning:
                meanings.append(
                    f"Связь с {planet_instr(other)} здесь может проявляться непросто. "
                    f"Она может давать {meaning}."
                )
        else:
            positive = rules.get("supportive")
            difficult = rules.get("tense")
            if positive:
                sentence = (
                    f"У тебя сильно проявлена связь с {planet_instr(other)}. "
                    f"Она усиливает такие качества, как {positive}."
                )
                if difficult:
                    sentence += (
                        f" При перегрузе эта же энергия может давать {difficult}."
                    )
                meanings.append(sentence)

    if not meanings:
        return ""

    intro = f" В этой части карты особенно важны дополнительные планетарные влияния. "
    return intro + " ".join(meanings)


HOUSE_GENITIVE = {
    1: "личности, самостоятельности, образа и способа проявляться",
    2: "денег, имущества, самоценности и личных ресурсов",
    3: "обучения, речи, языков, письма, информации, поездок и связей",
    4: "дома, семьи, родителей, корней, недвижимости и ощущения защищённости",
    5: "творчества, любви, детей, сцены, хобби и удовольствия",
    6: "работы, здоровья, мастерства, ежедневных навыков и режима",
    7: "партнёрства, брака, клиентов, договоров и взаимодействия один на один",
    8: "кризисов, интимности, чужих ресурсов, инвестиций и трансформации",
    9: "высшего образования, путешествий, иностранной среды, философии и преподавания",
    10: "карьеры, статуса, управления, репутации и общественной реализации",
    11: "друзей, сообществ, аудитории, технологий и коллективных проектов",
    12: "уединения, закулисья, творчества, помощи и скрытых процессов",
}


SUN_HOUSE_HUMAN = {
    1: "Солнце в 1 доме усиливает потребность самой задавать направление, быть заметной и влиять на происходящее. Важно чувствовать себя автором собственной жизни и иметь право действовать от себя.",
    2: "Солнце во 2 доме связывает самореализацию с личными ресурсами, самоценностью и материальной опорой. Важно видеть реальный результат своих усилий и уметь самостоятельно распоряжаться тем, что имеешь.",
    3: "Солнце в 3 доме делает речь, обучение, обмен информацией и контакты важной частью самореализации. Важно говорить, объяснять, учиться, передавать знания и чувствовать, что твои мысли услышаны.",
    4: "Солнце в 4 доме даёт сильную потребность быть хозяйкой своего пространства и выстраивать жизнь вокруг дома, семьи и чувства надёжного тыла. Часто есть тесная связь с родными, любовь к своему гнезду и желание самой задавать правила дома. Человек может быть более закрытым или интровертным, потому что много энергии вкладывает в семью, личное пространство и создание собственной базы.",
    5: "Солнце в 5 доме усиливает творчество, желание сиять, быть замеченной и создавать что-то своё. Важно получать радость от самовыражения, любви, проектов, сцены, детей или хобби, где можно проявить индивидуальность.",
    6: "Солнце в 6 доме раскрывается через мастерство, полезность и качество ежедневной работы. Важно быть компетентной, улучшать процессы и чувствовать, что труд приносит реальный результат.",
    7: "Солнце в 7 доме делает отношения и значимые союзы важной частью самореализации. Многое о себе человек понимает через партнёров, клиентов и взаимодействие один на один.",
    8: "Солнце в 8 доме тянет к глубоким процессам, психологии, кризисам, тайнам и сильным личным переменам. Важно не скользить по поверхности, а понимать скрытые причины и уметь действовать в сложных ситуациях.",
    9: "Солнце в 9 доме раскрывается через знания, путешествия, иностранную среду, преподавание и расширение мировоззрения. Важно видеть большую картину и постоянно расти.",
    10: "Солнце в 10 доме усиливает амбиции, стремление к статусу и желание оставить заметный профессиональный след. Важно видеть рост, признание и результат, который заметен не только тебе.",
    11: "Солнце в 11 доме раскрывается через друзей, сообщества, аудиторию, технологии и идеи будущего. Важно чувствовать участие в чём-то большем, чем только личная история.",
    12: "Солнце в 12 доме делает мир переживаний очень значимым и часто усиливает потребность в уединении, творчестве и работе за кадром. Важно иметь пространство, где можно восстановиться и услышать себя."
}

def _house_ruler(c, house):
    return RULERS[_cusp_sign(c, house)]

def _node_conjunctions(c, node_name, orb=6.0):
    node = _p(c, node_name)
    found = []
    for name, point in c["planets"].items():
        if name in ("Северный узел", "Южный узел"):
            continue
        diff = abs((node.longitude - point.longitude + 180.0) % 360.0 - 180.0)
        if diff <= orb:
            found.append((diff, name))
    found.sort(key=lambda x: x[0])
    return [name for _, name in found]

def _growth_problem(planet, other):
    pairs = {
        ("Луна","Плутон"): "чувства могут становиться слишком интенсивными, а в сложные моменты усиливается потребность всё контролировать",
        ("Луна","Сатурн"): "может быть трудно показывать слабость, просить о поддержке и вовремя расслабляться",
        ("Луна","Уран"): "эмоциональные реакции могут быть резкими, а ограничения быстро вызывают желание дистанцироваться",
        ("Луна","Нептун"): "может усиливаться впечатлительность, идеализация и путаница в чувствах",
        ("Венера","Плутон"): "в близости могут включаться ревность, проверки, крайности или страх потери",
        ("Венера","Сатурн"): "может появляться страх отвержения и ощущение, что любовь нужно заслуживать",
        ("Венера","Уран"): "сильна потребность в свободе, а давление или скука могут резко выключать интерес",
        ("Марс","Плутон"): "давление может запускать борьбу за контроль и сильную накопленную агрессию",
        ("Марс","Сатурн"): "энергия может долго сдерживаться, а потом выходить резко",
        ("Солнце","Плутон"): "может быть болезненная реакция на давление, контроль и борьбу за влияние",
        ("Солнце","Сатурн"): "может включаться слишком строгая самооценка и страх ошибиться",
        ("Солнце","Уран"): "ограничения могут вызывать резкий протест и желание всё изменить сразу",
        ("Меркурий","Нептун"): "мысли могут перегружаться догадками, сомнениями и неясностью",
        ("Меркурий","Плутон"): "ум способен слишком долго фиксироваться на одной теме и истощаться от этого",
        ("Меркурий","Сатурн"): "можно слишком долго проверять себя и бояться сказать или решить неправильно",
    }
    return pairs.get((planet, other)) or pairs.get((other, planet)) or "эта связь может создавать напряжение и требовать более осознанной реакции"

def who_preview(c):
    asc=c["asc"]; sun=_p(c,"Солнце"); moon=_p(c,"Луна")
    sh=_house(c,"Солнце"); mh=_house(c,"Луна")
    sm=WHO_SIGN_MEANINGS[sun.sign]
    am=WHO_SIGN_MEANINGS[asc.sign]
    mm=WHO_SIGN_MEANINGS[moon.sign]
    disp=WHO_DISPOSITOR[sun.sign]
    dp=_p(c,disp); dh=_house(c,disp)
    shadow=WHO_SUN_SHADOW[sun.sign]
    ruled=_ruled_houses(c,disp)
    stelliums=_stellium_houses(c)

    asc_ruler=RULERS[asc.sign]
    arp=_p(c,asc_ruler); arh=_house(c,asc_ruler)

    asc_extra=(
        f" Управитель Асцендента {asc_ruler} находится в {SIGN_PREP[arp.sign]} в {arh} доме. "
        f"Поэтому твоя внешняя манера раскрывается не сама по себе, а особенно через {HOUSE_SYMBOL[arh]}."
    )
    asc_extra += _planet_aspect_sentence(c,asc_ruler,"то, как ты проявляешь себя в жизни")

    sun_core="; ".join(sm["sun_core"][:3])
    sun_ignite="; ".join(sm["sun_ignite"][:2])
    sun_aspects=_planet_aspect_sentence(c,"Солнце","твоё самоощущение и способ проявлять волю")

    disp_text=(
        f"Управитель Солнца - {disp}. Он находится в {SIGN_PREP[dp.sign]} в {dh} доме. "
        f"Это важный второй слой характера: солнечная энергия реализуется через {HOUSE_SYMBOL[dh]}."
    )
    if ruled:
        ruled_meanings = []
        for h in ruled:
            meaning = HOUSE_GENITIVE.get(h, "")
            if meaning and meaning not in ruled_meanings:
                ruled_meanings.append(meaning)
        if ruled_meanings:
            natural = "; ".join(ruled_meanings[:3])
            disp_text += (
                f" Поэтому темы {natural} связаны у тебя с солнечной реализацией. "
                f"На практике это означает, что через них ты можешь сильнее проявлять инициативу, характер, личную волю "
                f"и ощущение собственного направления, особенно когда действуешь через сферу {HOUSE_SYMBOL[dh]}."
            )
    disp_text += _planet_aspect_sentence(c, disp, "реализации твоего Солнца")
    st_text = ""

    return (
        "КТО Я ✦\n\n"
        "Как ты проявляешься\n\n"
        "Асцендент показывает, как ты проявляешься внешне, как входишь в новые ситуации и какое первое впечатление производишь. "
        f"Твой Асцендент в {SIGN_PREP[asc.sign]}. {am['asc']}"
        f"{asc_extra}\n\n"
        "Твоё ядро\n\n"
        f"У тебя Солнце в {SIGN_PREP[sun.sign]} в {sh} доме. "
        f"В основе твоего характера: {sun_core}. "
        f"{SUN_HOUSE_HUMAN[sh]} "
        f"Тебя особенно зажигают {sun_ignite}.{sun_aspects}\n\n"
        f"{disp_text}\n\n"
        "Твоя тень\n\n"
        f"Противоположный знак твоего Солнца - {shadow}. Это не «плохая» часть карты, а ресурс, который помогает Солнцу не уходить в крайность. "
        f"{sm['shadow_resource']} {sm['shadow_extreme']}\n\n"
        "Что тебе нужно эмоционально\n\n"
        f"Луна в {SIGN_PREP[moon.sign]} в {mh} доме показывает твой способ эмоционально реагировать и восстанавливаться. "
        f"{mm['moon_comfort']} {mm['moon_stress']} "
        f"Дом Луны добавляет сферу {HOUSE_SYMBOL[mh]}, поэтому именно события здесь особенно легко затрагивают эмоциональное состояние."
        f"{_planet_aspect_sentence(c,'Луна','эмоциональные реакции')}\n\n"
        "Если коротко\n\n"
        f"Снаружи ты чаще проявляешь качества {SIGN_GEN[asc.sign]}, ядро личности строится {sign_phrase_po(sun.sign)}, "
        f"а эмоциональный комфорт зависит от Луны в {SIGN_PREP[moon.sign]}. "
        "Самое важное в твоей карте - не читать эти три слоя отдельно, а видеть, как они поддерживают или корректируют друг друга."
        + _bullets(WHO_PAID_TITLE, WHO_PAID_POINTS)
    )

def who_deep(c):
    sun=_p(c,"Солнце"); moon=_p(c,"Луна")
    sm=WHO_SIGN_MEANINGS[sun.sign]; mm=WHO_SIGN_MEANINGS[moon.sign]
    shadow=WHO_SUN_SHADOW[sun.sign]
    disp=WHO_DISPOSITOR[sun.sign]; dh=_house(c,disp)
    tension=_aspect_rows(c,"Солнце",True,6)
    blockers=[]
    for row in tension[:2]:
        other=row.get("planet")
        if other:
            blockers.append(_growth_problem("Солнце",other))
    blocker_text=" ".join(blockers) if blockers else "Тебя чаще гасит жизнь в режиме, где нет места собственному интересу, инициативе и выбранному тобой способу проявляться."

    return (
        who_preview(c).split("\n\n🔒 "+WHO_PAID_TITLE)[0]
        + f"\n\n🔓 {WHO_PAID_TITLE.upper()}\n\n"
        "Как проявлять себя через своё Солнце\n\n"
        f"Твоя сильная стратегия - сознательно создавать в жизни больше того, что зажигает Солнце в {SIGN_PREP[sun.sign]}: "
        + "; ".join(sm["sun_ignite"]) + ". "
        f"Особенно продуктивно делать это через {HOUSE_SYMBOL[_house(c,'Солнце')]}. "
        f"Управитель Солнца дополнительно ведёт в сферу {HOUSE_SYMBOL[dh]}, поэтому именно здесь часто находится практический канал самореализации.\n\n"
        "Что тебя гасит\n\n"
        f"{blocker_text} Важно не подавлять солнечную природу ради соответствия чужому ожиданию, а искать зрелую форму её выражения.\n\n"
        "Как использовать ресурс своей тени\n\n"
        f"Тень твоего Солнца - {shadow}. {sm['shadow_resource']} "
        f"Задача не стать «{shadow}ом», а добавить недостающие качества так, чтобы сильные стороны {SIGN_GEN[sun.sign]} работали устойчивее. "
        f"{sm['shadow_extreme']}\n\n"
        "Как возвращать себе ресурс\n\n"
        f"Луна в {SIGN_PREP[moon.sign]} прямо показывает, что помогает нервной системе и эмоциональной части восстанавливаться: {mm['moon_comfort']} "
        "Не жди полного перегруза. Чем раньше ты даёшь Луне её базовые условия, тем легче Солнцу снова проявляться уверенно."
    )

def love_preview(c):
    v=_p(c,"Венера"); vh=_house(c,"Венера")
    vm=LOVE_VENUS_SIGNS[v.sign]
    dsc=_dsc_sign(c); dm=LOVE_DESC[dsc]
    moon=_p(c,"Луна"); mars=_p(c,"Марс")
    issue=_dignity_issue("Венера",v.sign)

    aspect_bits=[]
    for row in _first_meaningful_aspects(c,"Венера",3,6):
        other=row.get("planet"); nat=_aspect_nature(row)
        rule=LOVE_VENUS_ASPECTS.get(other,{}).get(nat)
        if rule:
            aspect_bits.append(rule)
    aspect_text=" ".join(aspect_bits[:2])

    dignity_text=""
    if issue:
        dignity_text=f" Венера находится в положении {issue}. Это не делает любовь «плохой», но означает, что гармония и удовольствие требуют более осознанного способа проживания. "

    return (
        "ВСЁ О ЛЮБВИ ❤\n\n"
        "Венера - планета любви, красоты, удовольствия и материального благополучия. "
        "Её положение в натальной карте показывает, как ты проживаешь любовь, что приносит тебе удовольствие, "
        "что ты считаешь красивым и ценным и через что тебе легче чувствовать гармонию с собой и жизнью.\n\n"
        "Кто тебя притягивает и кто притягивается к тебе\n\n"
        f"Твой Десцендент в {SIGN_PREP[dsc]}. Особенно часто тебя могут привлекать {dm['attracts']}. "
        f"{dm['dynamic']} Десцендент описывает не только романтического партнёра, но и значимые отношения один на один: супругов, клиентов, союзников и деловых партнёров.\n\n"
        "Как ты любишь и что тебе нужно в отношениях\n\n"
        f"Венера в {SIGN_PREP[v.sign]}: {vm['love']} "
        f"Она находится в {vh} доме. {LOVE_VENUS_HOUSES[vh]}{dignity_text}"
        f" Луна в {SIGN_PREP[moon.sign]} показывает эмоциональную потребность в {MOON_NEED[moon.sign]}, "
        f"а Марс в {SIGN_PREP[mars.sign]} добавляет в притяжение темы {SIGN_SYMBOL[mars.sign]}.\n\n"
        "Что приносит тебе удовольствие\n\n"
        f"{vm['pleasure']} Здесь Венера показывает не только отношения, но и вкус, красоту, отдых, способ получать приятные впечатления и ощущать собственную ценность.\n\n"
        "Что может мешать гармонии\n\n"
        f"{vm['risk']} {aspect_text}\n\n"
        "Если коротко\n\n"
        f"Твой любовный сценарий нельзя свести к одному знаку Венеры. Его собирают Венера в {SIGN_PREP[v.sign]}, её {vh} дом, "
        f"аспекты, Луна, Марс и Десцендент в {SIGN_PREP[dsc]}. Вместе они показывают, что тебе важно и в чувствах, и в реальном формате отношений."
        + _bullets(LOVE_PAID_TITLE, LOVE_PAID_POINTS)
    )

def love_deep(c):
    v=_p(c,"Венера"); vm=LOVE_VENUS_SIGNS[v.sign]; dsc=_dsc_sign(c)
    dm=LOVE_DESC[dsc]; moon=_p(c,"Луна")
    rows=_first_meaningful_aspects(c,"Венера",3,6)
    vulnerabilities=[]
    for r in rows:
        if r.get("aspect") in TENSE:
            rule=LOVE_VENUS_ASPECTS.get(r.get("planet"),{}).get("tense")
            if rule: vulnerabilities.append(rule)
    vuln=" ".join(vulnerabilities[:2]) or vm["risk"]

    return (
        love_preview(c).split("\n\n🔒 "+LOVE_PAID_TITLE)[0]
        + f"\n\n🔓 {LOVE_PAID_TITLE.upper()}\n\n"
        "Какой партнёр подходит тебе для долгих отношений\n\n"
        f"На уровне партнёрского типа тебе важны качества Десцендента в {SIGN_PREP[dsc]}: {dm['attracts']}. "
        f"Но удерживает отношения не только притяжение. Венере в {SIGN_PREP[v.sign]} необходимо, чтобы в союзе было место для её способа любить: {vm['love']}\n\n"
        "Что помогает сохранять близость, доверие и интерес\n\n"
        f"Для эмоциональной устойчивости учитывай Луну: тебе нужны {MOON_NEED[moon.sign]}. "
        f"Для удовольствия и живости отношений регулярно возвращай то, что питает Венеру: {vm['pleasure']}\n\n"
        "Где твои уязвимые места\n\n"
        f"{vuln} Важно замечать этот сценарий до того, как он превращается в привычный способ проверять отношения или защищаться от близости.\n\n"
        "Как возвращать наполненность и гармонию\n\n"
        f"Не перекладывай всю функцию удовольствия на партнёра. Создавай собственную наполненную жизнь через то, что любит твоя Венера в {SIGN_PREP[v.sign]}: {vm['pleasure']} "
        "Тогда отношения становятся частью жизни, а не единственным источником любви к себе, удовольствия и ценности."
    )

def career_preview(c):
    sun=_p(c,"Солнце"); sh=_house(c,"Солнце")
    sm=CAREER_SUN_SIGNS[sun.sign]
    h2=_cusp_sign(c,2); h6=_cusp_sign(c,6); h10=_cusp_sign(c,10)
    r2=_house_ruler(c,2); r6=_house_ruler(c,6); r10=_house_ruler(c,10)
    p2=_p(c,r2); p6=_p(c,r6); p10=_p(c,r10)
    mh=c["mc"]

    talents=list(sm["talents"])
    # Add actual chart factors instead of generic sign-only output.
    mercury=_p(c,"Меркурий"); mars=_p(c,"Марс"); venus=_p(c,"Венера")
    talent_extra=[
        f"Меркурий в {SIGN_PREP[mercury.sign]} усиливает темы {SIGN_SYMBOL[mercury.sign]}",
        f"Марс в {SIGN_PREP[mars.sign]} показывает рабочий стиль через {SIGN_SYMBOL[mars.sign]}",
        f"Венера в {SIGN_PREP[venus.sign]} добавляет способности в темах {SIGN_SYMBOL[venus.sign]}",
    ]

    dirs=[]
    dirs.extend(sm["directions"])
    dirs.extend(CAREER_SUN_SIGNS[p10.sign]["directions"])
    dirs.extend(CAREER_SUN_SIGNS[p6.sign]["directions"])
    uniq=[]
    for d in dirs:
        if d not in uniq: uniq.append(d)

    stelliums=_stellium_houses(c)
    st=""
    if stelliums:
        hs=sorted(stelliums, key=lambda x: -len(stelliums[x]))
        h=hs[0]
        st=f" Самый заполненный акцент карты приходится на {h} дом ({', '.join(stelliums[h])}), поэтому сфера {HOUSE_SYMBOL[h]} обязательно учитывается при выборе направления."

    return (
        "ТАЛАНТЫ, КАРЬЕРА И ДЕНЬГИ ✦\n\n"
        "Твои таланты\n\n"
        f"Солнце в {SIGN_PREP[sun.sign]} даёт базовые сильные стороны: {', '.join(talents)}. "
        f"{CAREER_SUN_HOUSES[sh]} "
        + " ".join(talent_extra) + "." + st + "\n\n"
        "Как ты проявляешься в работе\n\n"
        f"VI дом начинается в {SIGN_PREP[h6]}. Его управитель {r6} находится в {SIGN_PREP[p6.sign]} в {_house(c,r6)} доме. "
        f"Поэтому ежедневная работа лучше раскрывается через {HOUSE_SYMBOL[_house(c,r6)]}. "
        f"MC находится в {SIGN_PREP[mh.sign]}, а управитель X дома {r10} стоит в {SIGN_PREP[p10.sign]} в {_house(c,r10)} доме. "
        f"Это связывает профессиональную реализацию с темами {HOUSE_SYMBOL[_house(c,r10)]}.\n\n"
        "Какие профессии тебе могут подойти\n\n"
        f"Когда несколько показателей складываются вместе, особенно интересны направления: {', '.join(uniq[:5])}. "
        "Это не список единственно возможных профессий. Сильнее работает не один показатель, а повторение одной темы через Солнце, X дом, VI дом, управителей и заполненные дома.\n\n"
        "Как у тебя устроена тема денег\n\n"
        f"II дом начинается в {SIGN_PREP[h2]}, а его управитель {r2} находится в {SIGN_PREP[p2.sign]} в {_house(c,r2)} доме. "
        f"Это показывает, что отношение к собственным ресурсам связано с темами {SIGN_SYMBOL[h2]}, а практическая финансовая сфера тянется к {HOUSE_SYMBOL[_house(c,r2)]}. "
        "В бесплатной части это описывает устройство темы денег, но не выдаёт весь алгоритм монетизации.\n\n"
        "Если коротко\n\n"
        f"Твой профессиональный вектор соединяет Солнце в {SIGN_PREP[sun.sign]}, работу по VI дому в {SIGN_PREP[h6]}, "
        f"карьерную ось с MC в {SIGN_PREP[mh.sign]} и денежный II дом в {SIGN_PREP[h2]}. "
        "Именно повторяющиеся темы между ними важнее любой одной планеты."
        + _bullets(CAREER_PAID_TITLE, CAREER_PAID_POINTS)
    )

def career_deep(c):
    h2=_cusp_sign(c,2); r2=_house_ruler(c,2); p2=_p(c,r2); r2h=_house(c,r2)
    h8=_cusp_sign(c,8); r8=_house_ruler(c,8); p8=_p(c,r8); r8h=_house(c,r8)
    h10=_cusp_sign(c,10); r10=_house_ruler(c,10); p10=_p(c,r10); r10h=_house(c,r10)
    money_dirs=CAREER_SUN_SIGNS[p2.sign]["directions"]
    career_dirs=CAREER_SUN_SIGNS[p10.sign]["directions"]

    return (
        career_preview(c).split("\n\n🔒 "+CAREER_PAID_TITLE)[0]
        + f"\n\n🔓 {CAREER_PAID_TITLE.upper()}\n\n"
        "Где прячется твой финансовый ресурс\n\n"
        f"Ключевой показатель - управитель II дома {r2}. Он стоит в {SIGN_PREP[p2.sign]} в {r2h} доме. "
        f"Поэтому собственный доход легче связывать со сферой {HOUSE_SYMBOL[r2h]}, используя качества и навыки {SIGN_GEN[p2.sign]}.\n\n"
        "Через что к тебе легче приходят деньги\n\n"
        f"В карте денежная тема особенно поддерживается направлениями: {', '.join(money_dirs[:5])}. "
        f"Если это пересекается с профессиональной линией X дома ({', '.join(career_dirs[:4])}), такая связка становится сильнее, потому что один и тот же навык одновременно работает на компетенцию, статус и доход.\n\n"
        "Что лучше всего монетизируется\n\n"
        "Лучше монетизировать не абстрактную «сильную планету», а конкретную связку навыков, которая повторяется в нескольких частях карты. "
        f"В твоём случае обязательно проверяй темы {SIGN_GEN[p2.sign]}, сферу {HOUSE_SYMBOL[r2h]} и профессиональную линию {HOUSE_SYMBOL[r10h]}.\n\n"
        "Куда могут уходить деньги и что мешает зарабатывать больше\n\n"
        f"VIII дом начинается в {SIGN_PREP[h8]}, его управитель {r8} находится в {SIGN_PREP[p8.sign]} в {r8h} доме. "
        f"Это место, где особенно важно следить за общими ресурсами, обязательствами, инвестициями и зависимостью от чужих денег. "
        "Практически рост дохода требует одного сильного предложения рынку, понятной цены, регулярной практики и отказа от распыления между слишком большим количеством направлений."
    )

def _node_sign_text(sign, side):
    d=NODE_SIGN_SYMBOLISM[sign]
    # files use keys south/north/teachers
    return d.get(side,"")

def nodes_preview(c):
    nn=_p(c,"Северный узел"); sn=_p(c,"Южный узел")
    nh=_house(c,"Северный узел"); sh=_house(c,"Южный узел")
    ns=NODE_SIGN_SYMBOLISM[nn.sign]; ss=NODE_SIGN_SYMBOLISM[sn.sign]
    ncon=_node_conjunctions(c,"Северный узел")
    scon=_node_conjunctions(c,"Южный узел")
    nr=NODE_SIGN_RULERS[nn.sign]
    nrp=_p(c,nr); nrh=_house(c,nr)

    s_extra=""
    if scon:
        bits=[NODE_PLANETS_FULL[p]["south"] for p in scon if p in NODE_PLANETS_FULL]
        if bits: s_extra=" " + " ".join(bits[:2])
    n_extra=""
    if ncon:
        bits=[NODE_PLANETS_FULL[p]["north"] for p in ncon if p in NODE_PLANETS_FULL]
        if bits: n_extra=" " + " ".join(bits[:2])

    return (
        "КАРМИЧЕСКИЕ УЗЛЫ ✦\n\n"
        "В астрологии Южный узел описывает знакомый опыт и естественные паттерны, а Северный - направление развития. "
        "Это символическая интерпретация, а не доказанное описание прошлых жизней.\n\n"
        "С чем ты пришёл\n\n"
        f"Южный узел в {SIGN_PREP[sn.sign]} в {sh} доме. {ss['south']} "
        f"Дом добавляет знакомую сферу: {NODE_HOUSES_FULL[sh]}.{s_extra} "
        "Здесь обычно легко действовать по привычке, поэтому сильная сторона Южного узла одновременно может стать зоной застревания.\n\n"
        "Куда тебя ведёт жизнь\n\n"
        f"Северный узел в {SIGN_PREP[nn.sign]} в {nh} доме. {ns['north']} "
        f"Сфера развития - {NODE_HOUSES_FULL[nh]}.{n_extra} "
        f"Управитель Северного узла {nr} находится в {SIGN_PREP[nrp.sign]} в {nrh} доме, поэтому задача дополнительно реализуется через {HOUSE_SYMBOL[nrh]}.\n\n"
        "Что притягивается в твою жизнь\n\n"
        f"Ось {sn.sign} - {nn.sign} снова и снова сталкивает знакомый способ {SIGN_GEN[sn.sign]} с необходимостью осваивать качества {SIGN_GEN[nn.sign]}. "
        "Это может проявляться через повторяющиеся обстоятельства, выборы и отношения, где старый навык уже есть, а нового ещё не хватает.\n\n"
        "Твои кармические учителя\n\n"
        f"{ns['teachers']} Такие люди могут не всегда быть комфортными, но символически они показывают качества, которые Северный узел предлагает развивать.\n\n"
        "Если коротко\n\n"
        f"Главный переход идёт от опыта Южного узла в {SIGN_PREP[sn.sign]} и {sh} доме к Северному узлу в {SIGN_PREP[nn.sign]} и {nh} доме. "
        "Южный узел не нужно отбрасывать. Его задача - стать опорой, а не местом постоянного возврата."
        + _bullets(NODE_PAID_TITLE, NODE_PAID_POINTS)
    )

def nodes_deep(c):
    nn=_p(c,"Северный узел"); sn=_p(c,"Южный узел")
    ns=NODE_SIGN_SYMBOLISM[nn.sign]; ss=NODE_SIGN_SYMBOLISM[sn.sign]
    nh=_house(c,"Северный узел")
    return (
        nodes_preview(c).split("\n\n🔒 "+NODE_PAID_TITLE)[0]
        + f"\n\n🔓 {NODE_PAID_TITLE.upper()}\n\n"
        "Какие качества развивать в первую очередь\n\n"
        f"{ns['north']} Не пытайся освоить всё сразу. Выбирай реальные действия, где качества {SIGN_GEN[nn.sign]} нужны регулярно, а не только символически.\n\n"
        "Что из привычного помогает, а что удерживает\n\n"
        f"{ss['south']} Это твой готовый ресурс. Он начинает удерживать на месте тогда, когда любой новый выбор автоматически возвращается к знакомой модели Южного узла и не требует развития новых качеств.\n\n"
        "Через какие действия ты двигаешься к задаче\n\n"
        f"Северный узел стоит в {nh} доме, поэтому практическая тренировка идёт через {NODE_HOUSES_FULL[nh]}. "
        "Полезны решения, где ты сама входишь в эту сферу, получаешь опыт и становишься в ней компетентнее.\n\n"
        "Как не возвращаться в один и тот же сценарий\n\n"
        f"В повторяющейся ситуации отделяй вопрос «что мне уже легко {sign_phrase_po(sn.sign)}?» от вопроса «какого нового качества {SIGN_GEN[nn.sign]} требует эта ситуация?». "
        "Так узловая ось перестаёт работать как качели между крайностями и становится направлением развития."
    )

def _growth_key(planet, other):
    mapn={"Солнце":"sun","Луна":"moon","Венера":"venus","Марс":"mars","Меркурий":"mercury",
          "Сатурн":"saturn","Плутон":"pluto","Уран":"uranus","Нептун":"neptune"}
    a=mapn.get(planet); b=mapn.get(other)
    if a and b:
        for k in (f"{a}_{b}",f"{b}_{a}"):
            if k in GROWTH_ASPECT_PATTERNS: return k
    return None

def _growth_candidates_full(c):
    candidates=[]
    for planet in ("Солнце","Луна","Венера","Марс","Меркурий"):
        p=_p(c,planet)
        issue=_dignity_issue(planet,p.sign)
        rows=_aspect_rows(c,planet,True,6)
        score=(4 if issue else 0)+(3 if planet in ("Солнце","Луна") else 0)+sum(max(1,7-float(r.get("orb",6))) for r in rows)
        if issue or rows:
            candidates.append((score,planet,issue,rows))
    candidates.sort(key=lambda x:x[0], reverse=True)
    return candidates[:4]

def growth_preview(c):
    cand=_growth_candidates_full(c)
    if not cand:
        return (
            "ТОЧКИ РОСТА ✦\n\n"
            "В карте нет одной личной планеты, которая резко выделяется сочетанием падения, изгнания и тяжёлых напряжённых аспектов. "
            "Поэтому точки роста здесь лучше искать через повторяющиеся конфигурации и управителей домов."
            + _bullets(GROWTH_PAID_TITLE, GROWTH_PAID_POINTS)
        )

    problems=[]; reactions=[]; resources=[]
    for _,planet,issue,rows in cand[:3]:
        p=_p(c,planet)
        bits=[]
        if issue:
            bits.append(f"{planet} в {SIGN_PREP[p.sign]} находится в положении {issue}. Это не «плохая» планета, но её функция может требовать больше осознанности")
        if rows:
            r=rows[0]; other=r.get("planet")
            key=_growth_key(planet,other)
            if key:
                pat=GROWTH_ASPECT_PATTERNS[key]
                bits.append(f"{planet} в напряжении с {other}: {pat['tension']}")
                resources.append(f"{planet}: {pat['resource']}")
            else:
                bits.append(_growth_problem(planet,other))
        if bits:
            problems.append("• " + " ".join(bits))
        logic=GROWTH_PLANET_LOGIC.get(planet,{})
        if logic.get("question"):
            reactions.append(logic["question"])

    return (
        "ТОЧКИ РОСТА ✦\n\n"
        "Что даётся тебе сложнее\n\n"
        + "\n\n".join(problems)
        + "\n\nЧто тебя выбивает\n\n"
        + " ".join(reactions[:3]) + " Именно такие ситуации стоит считать не доказательством слабости, а маркерами, где планета быстрее уходит в напряжённый сценарий.\n\n"
        "Как ты реагируешь под давлением\n\n"
        "Напряжённая планета часто сначала пытается решить ситуацию своим привычным способом, а при перегрузе уходит в крайность: контроль, избегание, импульс, закрытость, идеализацию или излишнюю строгость к себе. "
        "Важнее всего заметить этот момент раньше, чем реакция станет поведением.\n\n"
        "В чём здесь твоя сила\n\n"
        + ("; ".join(resources[:3]) if resources else "В каждом напряжённом факторе есть ресурс, который раскрывается после того, как энергия получает более зрелый способ выражения.")
        + "\n\nЕсли коротко\n\n"
        "Точки роста - это не список недостатков. Это места, где у тебя больше напряжения и одновременно больше энергии для развития."
        + _bullets(GROWTH_PAID_TITLE, GROWTH_PAID_POINTS)
    )

def growth_deep(c):
    cand=_growth_candidates_full(c)
    blocks=[]
    for _,planet,issue,rows in cand[:3]:
        p=_p(c,planet); opp=GROWTH_OPPOSITES[p.sign]
        key=None
        if rows:
            key=_growth_key(planet,rows[0].get("planet"))
        if key:
            pat=GROWTH_ASPECT_PATTERNS[key]
            tension=pat["tension"]; resource=pat["resource"]
        else:
            tension=_growth_problem(planet,rows[0].get("planet")) if rows else f"функция {planet} требует более осознанного использования"
            resource=GROWTH_PLANET_LOGIC.get(planet,{}).get("resource","более зрелое управление этой энергией")

        # Use exact agreed examples whenever chart matches them.
        example_key=None
        if planet=="Венера" and p.sign=="Скорпион": example_key="venus_scorpio"
        elif planet=="Луна" and p.sign=="Козерог": example_key="moon_capricorn"
        elif planet=="Венера" and p.sign=="Овен": example_key="venus_aries"
        elif planet=="Луна" and p.sign=="Водолей": example_key="moon_aquarius"

        if example_key and example_key in GROWTH_BALANCE_EXAMPLES:
            ex=GROWTH_BALANCE_EXAMPLES[example_key]
            action_text="; ".join(ex["actions"])
            block=(
                f"{planet} в {SIGN_PREP[p.sign]}\n\n"
                f"Что создаёт напряжение: {ex['tension']}\n\n"
                f"Что важно сохранить: {ex['keep']}\n\n"
                f"Чего добавить для баланса: {ex['balance']}\n\n"
                f"Что делать на практике: {action_text}."
            )
        else:
            quals=", ".join(GROWTH_BALANCE_QUALITIES.get(opp,[])[:6])
            block=(
                f"{planet} в {SIGN_PREP[p.sign]}\n\n"
                f"Что создаёт напряжение: {tension}.\n\n"
                f"Что важно сохранить: сильные качества исходного знака {SIGN_GEN[p.sign]} и саму функцию {planet}, а не пытаться её подавить.\n\n"
                f"Чего добавить для баланса: качества противоположного знака {SIGN_GEN[opp]} - {quals}.\n\n"
                f"Практика: давать энергии {planet} безопасный выход через реальные действия, тело, коммуникацию, творчество, границы или режим - в зависимости от функции планеты. "
                f"Цель - не стать «удобнее», а превратить напряжение в ресурс: {resource}."
            )
        blocks.append(block)

    return (
        growth_preview(c).split("\n\n🔒 "+GROWTH_PAID_TITLE)[0]
        + f"\n\n🔓 {GROWTH_PAID_TITLE.upper()}\n\n"
        + "\n\n".join(blocks)
        + "\n\nОбщий принцип\n\n"
        "Гармонизация не означает отрицать исходный знак или «убирать» сильную планету. "
        "Сначала сохраняется её природная сила, затем добавляются качества противоположного полюса и конкретный безопасный выход энергии. "
        "Так напряжённый аспект перестаёт быть только проблемой и становится рабочим ресурсом."
    )


def russian_language_qa(text):
    # Финальная языковая проверка.
    # Важно: исправляем только отдельные слова, а не подстроки внутри уже склонённых форм.
    bare_planet_fixes = {
        "Плутон":"Плутоном",
        "Нептун":"Нептуном",
        "Уран":"Ураном",
        "Сатурн":"Сатурном",
        "Юпитер":"Юпитером",
        "Марс":"Марсом",
        "Венера":"Венерой",
        "Меркурий":"Меркурием",
        "Луна":"Луной",
        "Солнце":"Солнцем",
        "Асцендент":"Асцендентом",
    }
    for nominative, instrumental in bare_planet_fixes.items():
        text = re.sub(
            rf"(?<![А-Яа-яЁё])с {re.escape(nominative)}(?![А-Яа-яЁё])",
            f"с {instrumental}",
            text,
        )

    sign_fixes = {
        "по Водолея":"по Водолею",
        "по Льва":"по Льву",
        "по Скорпиона":"по Скорпиону",
        "по Козерога":"по Козерогу",
        "по Стрельца":"по Стрельцу",
        "по Тельца":"по Тельцу",
        "по Овна":"по Овну",
        "по Рака":"по Раку",
    }
    for a, b in sign_fixes.items():
        text = text.replace(a, b)

    text = text.replace(
        "проявлять сердце",
        "проявлять сердечность и щедрость",
    )

    # Управление домами.
    text = re.sub(r"управляет (\d+) дома\b", r"управляет \1 домом", text)

    # Защита от случайного двойного окончания.
    text = re.sub(
        r"\b(Юпитером|Ураном|Нептуном|Плутоном|Марсом|Сатурном|Меркурием|Солнцем|Асцендентом)ом\b",
        r"\1",
        text,
    )
    text = re.sub(r"\b(Венерой|Луной)ой\b", r"\1", text)

    return text


# ---------------------------------------------------------------------
# AI TEXT LAYER: карта считается локально, модель только пишет текст.
# ---------------------------------------------------------------------

def _who_ai_facts(c, paid=False):
    asc = c["asc"]
    sun = _p(c, "Солнце")
    moon = _p(c, "Луна")
    disp = WHO_DISPOSITOR[sun.sign]
    asc_ruler = RULERS[asc.sign]

    def aspect_lines(planet, limit=6):
        lines = []
        for row in _first_meaningful_aspects(c, planet, limit=limit, max_orb=6.0):
            other = row.get("planet")
            if not other:
                continue
            nature = "напряжённая" if _aspect_nature(row) == "tense" else "гармоничная"
            lines.append(f"{planet} - {other}: {row.get('aspect')}, {nature}, орб {float(row.get('orb', 0)):.1f}°")
        return lines

    stellium_text = [f"{h} дом: {', '.join(names)}" for h, names in _stellium_houses(c).items()]
    lines = [
        "РЕЖИМ: " + ("ПОЛНЫЙ ПЛАТНЫЙ РАЗБОР" if paid else "БЕСПЛАТНЫЙ ПРЕВЬЮ"),
        f"Асцендент: {asc.sign}",
        f"Управитель Асцендента: {asc_ruler}, знак {_p(c, asc_ruler).sign}, дом {_house(c, asc_ruler)}",
        f"Солнце: {sun.sign}, дом {_house(c, 'Солнце')}",
        f"Диспозитор Солнца: {disp}, знак {_p(c, disp).sign}, дом {_house(c, disp)}",
        f"Дома под управлением диспозитора Солнца: {_ruled_houses(c, disp)}",
        f"Противоположный знак Солнца: {WHO_SUN_SHADOW[sun.sign]}",
        f"Луна: {moon.sign}, дом {_house(c, 'Луна')}",
        f"Стеллиумы: {stellium_text or 'нет выраженного стеллиума'}",
        "Аспекты управителя Асцендента:", *aspect_lines(asc_ruler),
        "Аспекты Солнца:", *aspect_lines("Солнце"),
        "Аспекты диспозитора Солнца:", *aspect_lines(disp),
        "Аспекты Луны:", *aspect_lines("Луна"),
    ]
    return "\n".join(map(str, lines))


WHO_AI_INSTRUCTIONS = """
Ты пишешь персональный астрологический разбор для Telegram-бота EGOIST.

Главный принцип: внутри анализируй карту профессионально, наружу говори как живой грамотный астролог. Клиент не должен видеть техническую кухню.

ЯЗЫК:
- русский, естественный и литературный;
- обращение только на «ты»;
- никаких переходов на «вы»;
- проверяй падежи, род, число, управление и согласование;
- не используй длинное тире, используй обычный дефис или перестрой фразу;
- запрещена механическая склейка шаблонов;
- запрещены повторы слов и смыслов;
- один смысл сообщается один раз;
- не пиши конструкции вроде «может давать может давать», «может создавать может», «даёт даёт»;
- не злоупотребляй словами «внутренний», «энергия», «ресурс».

МАРКЕТИНГОВАЯ ЛОГИКА FREE -> PAID:
- бесплатная часть должна давать точное узнавание: человек читает и думает «это про меня»;
- в бесплатной части покажи не только сильные стороны, но и 1-2 индивидуальные точки напряжения, которые естественно следуют из карты;
- проблему подсвечивай конкретно через поведение и жизненное ощущение, но НЕ давай полный способ решения;
- не создавай искусственную тревогу, не дави и не используй рекламные манипуляции; интерес к платной части должен возникать из точности разбора;
- бесплатная часть должна естественно оставлять вопрос «почему у меня так и как это изменить/усилить?»;
- платная часть обязана отвечать ИМЕННО на вопросы и сложности, которые были подсвечены бесплатно;
- платная часть объясняет причину, механизм и даёт конкретные действия, ориентиры и способы применения сильных сторон;
- не вводи в платной части случайные новые проблемы вместо тех, которые логично следуют из бесплатной диагностики;
- продающий эффект создаётся полезностью и персональной точностью, а не фразами «купи», «секрет», «ты должна узнать».

АСТРОЛОГИЧЕСКАЯ ЛОГИКА:
- знак = базовая символика, дом = сфера, планета = функция, аспект = способ проявления, управитель/диспозитор = канал реализации;
- синтезируй факторы между собой, не описывай каждый отдельно;
- гармоничные связи превращай в понятные сильные стороны;
- напряжённые связи описывай как конкретную сложность и одновременно потенциальную силу;
- не делай фатальных выводов;
- не придумывай факты, которых нет во входных данных.

НЕ ПОКАЗЫВАЙ КЛИЕНТУ без крайней необходимости:
- названия аспектов «секстиль», «тригон», «квадрат», «оппозиция», «соединение»;
- орбисы;
- номера домов в формулировках об управлении;
- слово «диспозитор»;
- списки планет стеллиума;
- техническую цепочку рассуждений.
Вместо этого сразу объясняй человеческий результат сочетания факторов.

БЕСПЛАТНЫЙ ПРЕВЬЮ:
1. «КТО Я ✦»
2. «Как ты проявляешься» - сначала в 1-2 коротких фразах объясни простыми словами, что такое Асцендент, что он показывает внешнюю манеру проявления, вход в новые ситуации и первое впечатление, и почему это важно для понимания того, как человек взаимодействует с миром. Затем дай 2-3 содержательных предложения персонального синтеза.
3. «Твоё ядро» - Солнце главный герой. Учитывай знак, точный дом, важные связи, диспозитора, управителя Асцендента и доминирующие темы карты, но сведи всё к 3-4 сильным выводам. Не перечисляй механику и не превращай раздел в каталог факторов.
4. «Твоя тень» - противоположный знак Солнца как полезное дополнение. 2-3 предложения: какой перекос возможен и какое качество противоположного знака помогает его уравновесить.
5. «Что тебе нужно эмоционально» - Луна, её знак, дом и только самые важные связи. 3-4 предложения: что даёт чувство комфорта, что перегружает и что помогает восстановиться.
6. «Если коротко» - максимум 2-3 предложения цельного синтеза. Не пересказывай предыдущие разделы другими словами.
7. До платного тизера человек должен уже понимать минимум одну свою сильную сторону и минимум одну реальную трудность: например, что именно его гасит, заставляет тормозить себя, перегружаться или уходить в крайность. Опиши это узнаваемо, но не выдавай готовый алгоритм.
8. В конце блок «🔒 КАК РАСКРЫТЬ СЕБЯ СИЛЬНЕЕ» с четырьмя короткими пунктами: как проявлять себя через Солнце и что зажигает; что гасит; как использовать ресурс тени; как возвращаться к эмоциональному комфорту. Формулируй пункты как естественное продолжение уже подсвеченных особенностей, а не как рекламу. Не раскрывай ответы бесплатно.

ВАЖНО ДЛЯ СОКРАЩЕНИЯ:
- сохраняй смысл более глубокого анализа, но убирай второстепенные детали;
- выбирай только 3-5 действительно ключевых факторов карты;
- если несколько факторов говорят об одном и том же, объединяй их в один вывод;
- не объясняй один и тот же вывод через разные планеты повторно;
- каждый абзац должен добавлять новую информацию;
- лучше одна точная формулировка, чем три похожих;
- не добавляй советы в бесплатную часть, кроме короткого объяснения эмоционального восстановления в разделе Луны.

ПОЛНЫЙ ПЛАТНЫЙ РАЗБОР:
Это не просто более длинное описание личности. Он должен отвечать на вопрос «ПОЧЕМУ у меня так и КАК мне с этим работать». Кратко напомни индивидуальную основу карты, затем раскрой именно те напряжения, которые естественно были бы подсвечены в бесплатной версии: что реально зажигает и помогает проявляться; что конкретно гасит и тормозит и почему; как использовать ресурс противоположного знака без крайностей; как восстанавливаться эмоционально. Для каждой проблемы дай понятный механизм и 2-4 реалистичных действия или ориентира. Не повторяй бесплатный текст дословно и не заполняй объём общей психологией.

Объём бесплатного текста: примерно 420-600 слов.
Объём полного текста: примерно 700-950 слов.
Перед ответом молча отредактируй текст как редактор русского языка. Верни только готовый клиентский текст.
"""


def _ai_text_qa(text):
    text = (text or "").strip()
    text = re.sub(r"\b([А-Яа-яЁёA-Za-z]+)(\s+\1\b)+", r"\1", text, flags=re.IGNORECASE)
    for a, b in {
        "может давать может": "может",
        "может создавать может": "может",
        "даёт даёт": "даёт",
        "дает дает": "дает",
    }.items():
        text = text.replace(a, b)
    return russian_language_qa(text)


async def who_ai_text(c, paid=False):
    fallback = russian_language_qa(who_deep(c) if paid else who_preview(c))
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return fallback
    try:
        client = AsyncOpenAI(api_key=key, timeout=60.0)
        response = await client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5.6-terra"),
            instructions=WHO_AI_INSTRUCTIONS,
            input=_who_ai_facts(c, paid=paid),
            max_output_tokens=5000,
        )
        text = _ai_text_qa(response.output_text)
        return text if len(text) >= 800 else fallback
    except Exception as e:
        print(f"[EGOIST AI] WHO generation failed: {type(e).__name__}: {e}")
        return fallback



# ---------------------------------------------------------------------
# AI TEXT LAYER FOR ALL OTHER EGOIST SECTIONS
# ---------------------------------------------------------------------

SECTION_AI_RULES = {
    "love": """
ЗАГОЛОВОК: «ВСЁ О ЛЮБВИ ❤».
Главный герой блока - Венера. Обязательно синтезируй её знак, дом, достоинства/ослабления и значимые связи. Десцендент обязателен. При первом упоминании в 1-2 коротких фразах объясни, что это точка значимых отношений один на один и зачем она важна для понимания партнёрского типа. После этого сразу переходи к персональному смыслу: какой тип людей особенно часто притягивается в отношения. Внутренне учитывай Луну, Марс, Солнце, управителя VII дома, V и VIII дома, если они реально добавляют смысл.

БЕСПЛАТНЫЙ ПРЕВЬЮ:
1. Короткое вступление: Венера показывает любовь, удовольствие, вкус, ценности и способность чувствовать гармонию.
2. «Кто тебя притягивает и кто притягивается к тебе».
3. «Как ты любишь и что тебе нужно в отношениях».
4. «Что приносит тебе удовольствие».
5. «Что может мешать гармонии» - только 1-2 самые сильные темы. Опиши их так, чтобы человек узнал реальный повторяющийся паттерн в отношениях и захотел понять, почему он возникает и как его изменить. Без нагнетания и без готового решения.
6. «Если коротко» - новый синтез, без повторов.
7. В конце только тизер «🔒 ТВОЙ СЦЕНАРИЙ ЛЮБВИ И ГАРМОНИИ»: какой партнёр подходит для долгих отношений; что сохраняет близость и интерес; уязвимые места и что с ними делать; как получать больше удовольствия и возвращать гармонию. Ответы в бесплатной части не раскрывай полностью.

ПОЛНЫЙ ПЛАТНЫЙ РАЗБОР:
Раскрой именно практическую часть и ответь на те уязвимости, которые бесплатный разбор подсветил бы по этой карте: почему именно такой тип отношений/реакций повторяется и как с этим работать. Разбери подходящий тип партнёра и динамику долгих отношений; сохранение близости, доверия и интереса; уязвимые сценарии и способы не разрушать отношения; способы расслабляться, получать удовольствие и поддерживать самоценность. На каждую ключевую трудность дай конкретные действия и ориентиры. Не повторяй бесплатный текст дословно.
""",
    "career_money": """
ЗАГОЛОВОК: «ТАЛАНТЫ, КАРЬЕРА И ДЕНЬГИ ✦».
Внутренне обязательно анализируй II, VI и X дома, VIII когда он действительно важен, их управителей и планеты в этих домах. Учитывай Солнце и его канал реализации, управителя Асцендента, Меркурий, Венеру, Марс, Юпитер, Сатурн и Луну. Сильные заполненные дома и стеллиумы делай центральными жизненными темами. Профессии называй только когда несколько независимых факторов сходятся в одно направление. Не обещай конкретный доход и не делай фатальных финансовых прогнозов.

БЕСПЛАТНЫЙ ПРЕВЬЮ:
1. «Твои таланты» - 3-5 самых сильных, с объяснением, как они проявляются.
2. «Как ты проявляешься в работе».
3. «Какие профессии тебе могут подойти» - 3-5 конкретных направлений только на основе синтеза карты.
4. «Как у тебя устроена тема денег» - отношение к ресурсам, способ зарабатывать, сильные и слабые привычки. Обязательно подсвети 1 конкретное противоречие или тормоз, если карта его показывает, чтобы возник естественный вопрос «почему я так делаю и как превратить потенциал в деньги?». Не раскрывай весь денежный алгоритм.
5. «Если коротко» - новый вывод, без повторов.
6. В конце тизер «🔒 КАК УВЕЛИЧИТЬ СВОЙ ДОХОД»: где финансовый ресурс; через что легче приходят деньги; что лучше монетизируется; куда уходят деньги и что мешает зарабатывать больше. Не раскрывай ответы полностью.

ПОЛНЫЙ ПЛАТНЫЙ РАЗБОР:
Дай практический финансово-профессиональный разбор, который отвечает на вопросы, возникшие из бесплатной диагностики: почему сильные способности не всегда превращаются в результат/доход, что именно тормозит и как это изменить. Раскрой главный ресурс, каналы монетизации, подходящие модели работы, что масштабировать, что мешает доходу, где возможны утечки и какие конкретные шаги помогут. Не повторяй бесплатную часть дословно.
""",
    "nodes": """
ЗАГОЛОВОК: «КАРМИЧЕСКИЕ УЗЛЫ ☊».

В самом начале ОБЯЗАТЕЛЬНО дай короткое и очень простое пояснение природы узлов для человека, который вообще не разбирается в астрологии.
Смысл передай так:
- Южный узел - то, что в астрологической традиции связывают с уже наработанным прошлым опытом: знакомыми качествами, реакциями и сценариями, которые включаются почти автоматически.
- Северный узел - направление, к которому важно постепенно прийти в этом воплощении: новые качества, выборы и опыт, через которые человек развивается и раскрывает своё предназначение.
- Обязательно поясни: задача не отказаться от Южного узла. Он остаётся базой и ресурсом. Важно не застревать только в знакомом, а учиться добавлять качества Северного узла.
Это символическая астрологическая интерпретация, а не доказанный факт о прошлых жизнях. Не пугай и не используй фатализм.

Затем переходи к персональному разбору. Анализируй Северный и Южный узлы по знакам и точным домам, их ось, соединения и существенные аспекты, управителей знаков узлов, а также связи с Солнцем, Луной, Асцендентом, MC, стеллиумами и доминирующими темами.

БЕСПЛАТНЫЙ ПРЕВЬЮ:
1. «С чем ты пришёл» - что уже знакомо и естественно по Южному узлу, какие способности и привычные сценарии человек несёт как готовый опыт. Формулируй гендерно нейтрально, если возможно.
2. «Куда тебя ведёт жизнь» - к каким качествам, решениям и опыту ведёт Северный узел.
3. «Где можно застревать» - 1-2 индивидуальные болевые точки по оси узлов: чего человек может бояться, что ему трудно отпускать, какой привычный сценарий возвращает назад.
4. «Что притягивается в твою жизнь» - повторяющиеся темы и ситуации, если карта это подтверждает. Покажи, где знакомый паттерн удобен, но уже ограничивает развитие, чтобы естественно возник вопрос «как выйти из повтора и что делать дальше?».
5. «Твои кармические учителя» - какие типы людей или жизненные обстоятельства особенно двигают развитие, только если это следует из карты.
6. «Если коротко» - новый синтез без повторов.
7. В конце тизер «🔒 КАК ПРОРАБОТАТЬ СВОИ КАРМИЧЕСКИЕ УЗЛЫ»: подведи к платной части через уже найденные в карте болевые точки и повторяющиеся сценарии. Тизер должен вызывать естественный вопрос «что мне конкретно делать?», но НЕ выдавать сами решения бесплатно. Не перечисляй конкретные действия, привычки и проработки, которые будут даны в платной части.

ПОЛНЫЙ ПЛАТНЫЙ РАЗБОР:
Главная задача платной части - дать человеку инструменты, а не ещё одно описание.

ОБЯЗАТЕЛЬНО РАСКРОЙ ВСЕ ПЯТЬ ПУНКТОВ:
1. «Как проработать свои кармические узлы через конкретные действия»
   - объясни, как переводить понимание оси узлов в реальные действия и поведение.

2. «Что делать, чтобы двигаться к Северному узлу и выполнять своё направление развития»
   - дай конкретные решения, выборы, навыки и жизненные шаги, соответствующие именно Северному узлу этого человека.

3. «Какие привычки Южного узла помогают, а какие уже удерживают на месте»
   - раздели полезный ресурс Южного узла и автоматические сценарии, которые мешают развитию.

4. «Какие конкретные действия, решения и новые модели поведения стоит вводить в жизнь»
   - дай практический список действий, которые можно реально применять в повседневности, отношениях, работе, обучении и других релевантных сферах карты.

5. «Какие 2-4 индивидуальные проработки подходят именно этому человеку по его знакам, домам, аспектам и управителям узлов»
   - каждая проработка должна быть связана с конкретным фактором карты;
   - объясняй простыми словами, без перегруза технической лексикой;
   - для каждой проработки покажи: болевую точку -> старую автоматическую реакцию -> новый выбор -> конкретное действие.

Также обязательно:
- объясни, почему человеку психологически проще оставаться в сценарии Южного узла;
- покажи, где Южный узел является сильной базой, а где превращается в ловушку;
- раскрой 2-4 индивидуальные болевые точки по знакам, домам, аспектам и управителям узлов;
- дай 3-6 практических действий, которые можно реально внедрять;
- покажи, какие навыки стоит развивать, какие ситуации сознательно выбирать чаще, а от каких автоматических реакций постепенно отходить;
- объясни признаки того, что человек действительно движется в сторону Северного узла;
- свяжи рекомендации с реальными сферами жизни, которые показывают дома и управители узлов;
- не ограничивайся общими фразами вроде «развивай коммуникацию» или «будь самостоятельнее»: переводи это в конкретные действия, привычки, решения, обучение, тип взаимодействия с людьми или жизненные задачи;
- не повторяй бесплатную часть дословно.

КЛЮЧЕВОЙ ПРИНЦИП:
Бесплатная часть = узнавание, болевые точки и понимание того, где человек застревает.
Платная часть = инструмент, конкретные действия и путь, как двигаться к Северному узлу.
""",
    "growth": """
ЗАГОЛОВОК: «ТОЧКИ РОСТА ✦».
Выбери только 2-4 действительно значимые темы. Внутренне учитывай планеты в изгнании/падении и сильные напряжённые связи, особенно Солнца, Луны, Венеры, Марса, Меркурия, Сатурна, Плутона, Урана и Нептуна. Важнее повторяющиеся сигнатуры, чем одиночный слабый фактор. Для каждой темы объясняй: что мешает; что запускает реакцию; как человек ведёт себя под давлением; какая сила скрыта в том же факторе. Гармонизация не должна подавлять исходную планету/знак, а должна добавлять балансирующие качества и реальные действия.

БЕСПЛАТНЫЙ ПРЕВЬЮ:
1. 2-4 основные точки роста, каждая отдельным небольшим смысловым блоком.
2. Покажи триггеры и реакцию под давлением максимально узнаваемо: что человек обычно делает, чувствует или откладывает. Не давай полный способ коррекции здесь.
3. Покажи сильную сторону той же конфигурации.
4. «Если коротко» - новый вывод, без повторов.
5. В конце тизер «🔒 КАК ПРЕВРАТИТЬ СЛАБЫЕ МЕСТА В СИЛУ»: как снизить напряжение; какие качества добавлять; как перестать повторять разрушительный сценарий и давать энергии безопасный выход; как превращать напряжённые связи в силу и новые возможности. Не раскрывай весь алгоритм бесплатно.

ПОЛНЫЙ ПЛАТНЫЙ РАЗБОР:
Для каждой выбранной точки роста сначала коротко объясни, почему этот механизм включается именно так, затем дай конкретные способы гармонизации, поведенческие действия, безопасные выходы напряжения, признаки перегруза и зрелую форму этой же особенности. Платная часть должна закрывать вопрос «что мне делать с тем, что я только что узнала о себе». Не повторяй бесплатную часть дословно.
""",
    "child": """
ЗАГОЛОВОК: «ПОТЕНЦИАЛ РЕБЁНКА 🌱».
Это разбор О РЕБЁНКЕ. Повествование о ребёнке всегда веди в третьем лице. НИКОГДА не обращайся к ребёнку на «ты». Когда даёшь рекомендации, обращайся к родителю нейтрально: «родителю полезно», «можно поддерживать», «важно давать ребёнку». Следи за родом и не придумывай пол ребёнка, если он не передан во входных данных.
Внутренне синтезируй Солнце, Луну, Асцендент, Меркурий, Венеру, Марс, Юпитер, Сатурн, сильные дома, аспекты и отдельный обязательный блок кармических узлов. Узлы анализируй по знакам, точным домам, аспектам, соединениям и управителям. Не делай фатальных выводов и не навязывай одну профессию.

БЕСПЛАТНЫЙ ПРЕВЬЮ:
1. «Главные сильные стороны».
2. «Как ребёнок воспринимает мир».
3. «Как ему легче учиться» - только третье лицо.
4. «Что важно эмоционально». Если есть выраженная трудность, мягко покажи родителю, в каких ситуациях ребёнку особенно тяжело и как это выглядит, но полный алгоритм поддержки оставь платной части.
5. «Кармические узлы» - знакомые паттерны Южного и направление развития Северного.
6. «Если коротко».
7. В конце тизер полного детского разбора: таланты, обучение и возможные профессиональные направления, мотивация, эмоциональные особенности, сильные стороны и практические рекомендации родителю. Не раскрывай всё бесплатно.

ПОЛНЫЙ ПЛАТНЫЙ РАЗБОР:
Дай подробный, но бережный разбор потенциала как ответ на вопросы родителя «почему ребёнок так реагирует/учится/мотивируется и как его лучше поддержать». Раскрой таланты, стиль обучения, мотивацию, эмоциональные потребности, коммуникацию, инициативность, социализацию, возможные направления интересов/профессий и практические рекомендации родителю. Для каждой заметной сложности дай понятный способ поддержки. Отдельно полно раскрой кармические узлы. Не повторяй бесплатную часть дословно.
""",
}

COMMON_AI_INSTRUCTIONS = """
Ты пишешь персональный астрологический разбор для Telegram-бота EGOIST.

ГЛАВНЫЙ ПРИНЦИП:
Сначала профессионально синтезируй карту внутри, затем выдай готовый человеческий текст. Клиент не должен видеть техническую кухню. Не превращай входные факты в последовательный пересказ. Найди 3-6 центральных смыслов и строй текст вокруг них.

ЯЗЫК И РЕДАКТУРА:
- русский, естественный, современный и литературный;
- во взрослых разборах обращение только на «ты»;
- не используй длинное тире, ставь обычный дефис или перестраивай предложение;
- обязательно проверь падежи, склонения, род, число, согласование, управление и местоимения;
- запрещены механические склейки и обрывочные шаблоны;
- запрещены повторы слов и повтор одного смысла соседними предложениями;
- один смысл - один раз, если не появляется новый угол;
- избегай канцелярита, пустых общих фраз и чрезмерного «может»;
- не злоупотребляй словами «энергия», «внутренний», «ресурс», «сценарий»;
- не пиши «может давать может», «может создавать может», «даёт даёт» и любые подобные дубли;
- перед выдачей молча перечитай текст как строгий редактор русского языка.

ПРОФЕССИОНАЛЬНЫЕ ТЕРМИНЫ ДЛЯ НЕ-АСТРОЛОГА:
- считай, что клиент вообще не знает астрологической терминологии;
- если в клиентском тексте появляется профессиональный термин, в первом его появлении объясни его простыми словами в 1-2 коротких предложениях и сразу скажи, почему он важен именно в этом разделе;
- не давай учебниковых определений и не перегружай терминологией; если смысл можно передать без термина, лучше передай смысл без него;
- Асцендент: коротко объясни, что он показывает внешнюю манеру проявления, способ входить в новые ситуации и первое впечатление;
- Десцендент: коротко объясни, что он связан со значимыми отношениями один на один и показывает качества, которые особенно притягиваются через партнёров, союзников и клиентов;
- Кармические узлы: коротко объясни, что Южный узел символически описывает знакомый, привычный опыт и сильные паттерны, а Северный - направление развития и качества, которые постепенно осваиваются;
- если используешь MC, стеллиум, ретроградность, Лилит, VIII дом, управителя дома или другой специальный термин, либо кратко расшифруй его простыми словами, либо убери термин и оставь только понятный клиенту смысл;
- объяснение термина должно помогать читать персональный разбор, а не превращать текст в урок астрологии.

АСТРОЛОГИЧЕСКИЙ СИНТЕЗ:
- знак = базовая символика и жизненные темы;
- дом = сфера жизни;
- планета = функция;
- аспект = характер взаимодействия;
- управитель = канал реализации;
- учитывай прямую символику знаков, а не только абстрактную психологию;
- гармоничные связи превращай в понятные сильные стороны;
- напряжённые связи описывай как конкретную сложность и возможную зрелую силу;
- сильные заполненные дома и стеллиумы используй как центральные темы, но не перечисляй их механически;
- не делай вывод из одного фактора, если он не поддержан общей картой;
- не придумывай фактов, которых нет во входных данных.

НЕ ПОКАЗЫВАЙ КЛИЕНТУ БЕЗ КРАЙНЕЙ НЕОБХОДИМОСТИ:
- слова «секстиль», «тригон», «квадрат», «оппозиция», «соединение»;
- орбисы;
- слово «диспозитор»;
- технические цепочки управителей;
- формулировки «планета управляет 1/2/7 домом»;
- списки планет стеллиума;
- лишние номера домов как доказательство вывода.
Можно назвать ключевое положение вроде «Венера в Скорпионе» или «Солнце в 5 доме», если это делает объяснение понятнее, но не превращай текст в перечень позиций.

ЛОГИКА FREE -> PAID ДЛЯ КАЖДОГО БЛОКА:
- FREE отвечает «ЧТО это говорит обо мне?». Цель - точное узнавание: человек должен несколько раз подумать «да, это я».
- FREE обязательно показывает сильные стороны и 1-2 самые характерные трудности/противоречия именно этого раздела. Показывай их через узнаваемое поведение, реакции и повторяющиеся ситуации.
- FREE не должен оставаться только комплиментарным. Покажи, где сильная черта способна работать против человека, если это подтверждает карта.
- FREE не даёт полный алгоритм решения. Он естественно формирует вопросы «почему это повторяется?», «что с этим делать?», «как использовать это себе в плюс?».
- Не пугай, не дави, не преувеличивай проблему и не используй навязчивые продажи. Мотивация к оплате должна возникать только из точности и полезности персонального анализа.
- PAID отвечает «ПОЧЕМУ так происходит и КАК с этим работать?». Он обязан продолжить именно те индивидуальные вопросы, которые FREE подсветил бы по этой же карте.
- Для каждой ключевой сложности в PAID: объясни механизм простыми словами, покажи зрелую/сильную форму этой же особенности и дай конкретные применимые действия.
- PAID не должен быть просто более длинной бесплатной версией и не должен внезапно уходить в посторонние темы.
- В платной части советы должны быть персонализированы под карту, а не выглядеть как универсальные советы из психологии.

Объём бесплатного текста ориентировочно 430-700 слов. Полного - 700-1000 слов. Не растягивай текст ради объёма.
Верни только готовый клиентский текст без комментариев о своей работе.
"""


def _all_aspect_fact_lines(c, planets=None, max_orb=6.0, per_planet=6):
    planets = planets or list(c.get("planets", {}).keys())
    out = []
    seen = set()
    for planet in planets:
        if planet not in c.get("planets", {}):
            continue
        for row in _first_meaningful_aspects(c, planet, limit=per_planet, max_orb=max_orb):
            other = row.get("planet")
            if not other:
                continue
            key = tuple(sorted((planet, other))) + (row.get("aspect"),)
            if key in seen:
                continue
            seen.add(key)
            nature = "напряжённая" if _aspect_nature(row) == "tense" else "гармоничная"
            out.append(f"{planet} - {other}: {row.get('aspect')}, {nature}, орб {float(row.get('orb', 0)):.1f}°")
    return out


def _section_ai_facts(c, sec, paid=False):
    lines = [
        "РЕЖИМ: " + ("ПОЛНЫЙ ПЛАТНЫЙ РАЗБОР" if paid else "БЕСПЛАТНЫЙ ПРЕВЬЮ"),
        f"Асцендент: {c['asc'].sign}",
        f"MC: {c['mc'].sign}",
    ]

    # Базовая карта. Это служебные факты для модели, не готовый клиентский текст.
    for name, p in c.get("planets", {}).items():
        house = c.get("houses", {}).get(name)
        if name in ("Северный узел", "Южный узел"):
            issue = ""
        else:
            try:
                issue = _dignity_issue(name, p.sign) or ""
            except Exception:
                issue = ""
        lines.append(f"{name}: знак {p.sign}, дом {house}" + (f", достоинство/ослабление: {issue}" if issue else ""))

    try:
        dsc = _dsc_sign(c)
        lines.append(f"Десцендент: {dsc}")
        dsc_ruler = RULERS.get(dsc)
        if dsc_ruler and dsc_ruler in c.get("planets", {}):
            lines.append(f"Управитель Десцендента: {dsc_ruler}, знак {_p(c,dsc_ruler).sign}, дом {_house(c,dsc_ruler)}")
    except Exception:
        pass

    for h in range(1, 13):
        try:
            ruler = _house_ruler(c, h)
        except Exception:
            ruler = None
        occupants = _planets_in_house(c, h)
        if ruler or occupants:
            lines.append(f"Дом {h}: управитель {ruler or 'не определён'}; планеты {', '.join(occupants) if occupants else 'нет'}")

    stelliums = _stellium_houses(c)
    if stelliums:
        lines.append("Сильные заполненные дома/стеллиумы: " + "; ".join(f"{h} дом: {', '.join(ps)}" for h, ps in stelliums.items()))

    if sec == "love":
        focus = ["Венера", "Луна", "Марс", "Солнце", "Сатурн", "Плутон", "Уран", "Нептун"]
        lines.append("ФОКУС: Венера, Десцендент, VII дом, затем Луна, Марс, Солнце, V и VIII дома.")
    elif sec == "career_money":
        focus = ["Солнце", "Меркурий", "Венера", "Марс", "Юпитер", "Сатурн", "Луна", "Плутон", "Уран", "Нептун"]
        lines.append("ФОКУС: II, VI, X дома, VIII при значимости; таланты, работа, профессии, ресурсы и монетизация.")
    elif sec == "nodes":
        focus = ["Северный узел", "Южный узел", "Солнце", "Луна", "Меркурий", "Венера", "Марс", "Юпитер", "Сатурн", "Уран", "Нептун", "Плутон"]
        lines.append("ФОКУС: ось узлов, их дома/знаки, связи, управители знаков узлов и связь с основой карты.")
    elif sec == "growth":
        focus = ["Солнце", "Луна", "Меркурий", "Венера", "Марс", "Сатурн", "Плутон", "Уран", "Нептун", "Юпитер"]
        lines.append("ФОКУС: только 2-4 наиболее сильных повторяющихся напряжения и их зрелый потенциал.")
    elif sec == "child":
        focus = ["Солнце", "Луна", "Меркурий", "Венера", "Марс", "Юпитер", "Сатурн", "Северный узел", "Южный узел", "Уран", "Нептун", "Плутон"]
        lines.append("ФОКУС: потенциал ребёнка, обучение, мотивация, эмоции, таланты, социализация и отдельный блок узлов.")
    else:
        focus = list(c.get("planets", {}).keys())

    lines.append("Значимые аспекты:")
    lines.extend(_all_aspect_fact_lines(c, focus, max_orb=6.0, per_planet=6) or ["нет аспектов в заданном рабочем орбисе"])

    # Управляемые дома важны как внутренний слой, но не должны выводиться клиенту списком.
    for planet in focus:
        if planet in c.get("planets", {}):
            try:
                ruled = _ruled_houses(c, planet)
            except Exception:
                ruled = []
            if ruled:
                lines.append(f"{planet} связан через управление с домами: {ruled}")

    return "\n".join(map(str, lines))


def _safe_ai_unavailable(sec, paid=False):
    titles = {
        "love": "ВСЁ О ЛЮБВИ ❤",
        "career_money": "ТАЛАНТЫ, КАРЬЕРА И ДЕНЬГИ ✦",
        "nodes": "КАРМИЧЕСКИЕ УЗЛЫ ☊",
        "growth": "ТОЧКИ РОСТА ✦",
        "child": "ПОТЕНЦИАЛ РЕБЁНКА 🌱",
    }
    return (
        f"{titles.get(sec, 'EGOIST')}\n\n"
        "Сейчас не удалось сформировать персональный текст. Расчёт карты сохранён, поэтому данные повторно вводить не нужно. "
        "Попробуй открыть этот раздел ещё раз через минуту."
    )


async def section_ai_text(c, sec, paid=False):
    if sec == "who":
        return await who_ai_text(c, paid=paid)
    if sec not in SECTION_AI_RULES:
        raise KeyError(sec)
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return _safe_ai_unavailable(sec, paid)
    try:
        client = AsyncOpenAI(api_key=key, timeout=90.0)
        instructions = COMMON_AI_INSTRUCTIONS + "\n\n" + SECTION_AI_RULES[sec]
        response = await client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5.6-terra"),
            instructions=instructions,
            input=_section_ai_facts(c, sec, paid=paid),
            max_output_tokens=5200,
        )
        result = _ai_text_qa(response.output_text)
        # Не показываем старую шаблонную склейку, если AI вернул пустой/слишком короткий ответ.
        if len(result) < 500:
            raise ValueError("AI response is unexpectedly short")
        return result
    except Exception as e:
        print(f"[EGOIST AI] {sec.upper()} generation failed: {type(e).__name__}: {e}")
        return _safe_ai_unavailable(sec, paid)

def new_preview(c, sec):
    if sec=="who": return russian_language_qa(who_preview(c))
    if sec=="love": return russian_language_qa(love_preview(c))
    if sec=="career_money": return russian_language_qa(career_preview(c))
    if sec=="nodes": return russian_language_qa(nodes_preview(c))
    if sec=="growth": return russian_language_qa(growth_preview(c))
    raise KeyError(sec)

def new_deep(c, sec):
    if sec=="who": return russian_language_qa(who_deep(c))
    if sec=="love": return russian_language_qa(love_deep(c))
    if sec=="career_money": return russian_language_qa(career_deep(c))
    if sec=="nodes": return russian_language_qa(nodes_deep(c))
    if sec=="growth": return russian_language_qa(growth_deep(c))
    raise KeyError(sec)


# ---------------------------------------------------------------------
# ДЕТСКИЙ БЛОК И ТЕХНИЧЕСКАЯ ДИАГНОСТИКА
# Теперь тоже находятся в этом одном файле.
# ---------------------------------------------------------------------

CHILD_SUN = {
    "Овен":"инициативность, смелость, самостоятельность и желание действовать",
    "Телец":"устойчивость, практичность, терпение и чувство качества",
    "Близнецы":"любознательность, речь, обучение и быструю работу с информацией",
    "Рак":"эмпатию, заботу, привязанность к семье и чувствительность к атмосфере",
    "Лев":"творчество, яркость, лидерство и желание проявлять себя",
    "Дева":"наблюдательность, анализ, аккуратность и стремление улучшать результат",
    "Весы":"дипломатичность, чувство красоты, партнёрство и умение видеть две стороны",
    "Скорпион":"глубину, волю, исследовательский интерес и психологическую наблюдательность",
    "Стрелец":"интерес к знаниям, путешествиям, языкам и широкому кругозору",
    "Козерог":"ответственность, упорство, собранность и ориентацию на достижение",
    "Водолей":"нестандартность, независимость, технологии и интерес к новому",
    "Рыбы":"воображение, эмпатию, музыку, творчество и тонкое восприятие"
}

def child_report(c, full=False):
    sun=_p(c,"Солнце"); moon=_p(c,"Луна"); mercury=_p(c,"Меркурий")
    sh=_house(c,"Солнце"); mh=_house(c,"Луна"); meh=_house(c,"Меркурий")
    asc=c["asc"]
    nr=_p(c,"Северный узел"); sr=_p(c,"Южный узел")
    nh=_house(c,"Северный узел"); soh=_house(c,"Южный узел")

    text = (
        "🌱 ПОТЕНЦИАЛ РЕБЁНКА\n\n"
        "Главные сильные стороны\n\n"
        f"Солнце в {SIGN_PREP[sun.sign]} показывает {CHILD_SUN[sun.sign]}. "
        f"Поскольку Солнце находится в {sh} доме, особенно важно давать ребёнку опыт через сферу {HOUSE_SYMBOL[sh]}.\n\n"
        "Как ребёнок воспринимает мир\n\n"
        f"Асцендент в {SIGN_PREP[asc.sign]} делает внешнюю манеру ближе к качествам {SIGN_GEN[asc.sign]}. "
        f"Луна в {SIGN_PREP[moon.sign]} показывает, что для эмоционального комфорта особенно нужны {MOON_NEED[moon.sign]}.\n\n"
        "Как лучше учиться\n\n"
        f"Меркурий в {SIGN_PREP[mercury.sign]} в {meh} доме показывает интерес к темам {SIGN_SYMBOL[mercury.sign]}. "
        f"Обучение лучше связывать со сферой {HOUSE_SYMBOL[meh]} и давать возможность не просто запоминать, а применять информацию.\n\n"
        "Кармические узлы\n\n"
        f"Южный узел в {SIGN_PREP[sr.sign]} в {soh} доме показывает естественно знакомые качества и сценарии, связанные с темами {SIGN_SYMBOL[sr.sign]}. "
        f"Северный узел в {SIGN_PREP[nr.sign]} в {nh} доме показывает направление развития через {SIGN_SYMBOL[nr.sign]}. "
        "Родителю полезно поддерживать эти качества постепенно, без давления и идеи, что ребёнок обязан выбрать один заранее заданный путь."
    )

    if not full:
        return text + (
            "\n\n🔒 В полном детском разборе: таланты, подходящие направления обучения и профессий, "
            "эмоциональные особенности, мотивация, сильные стороны и практические рекомендации родителю."
        )

    return text + (
        "\n\n🔓 КАК ПОМОЧЬ РАСКРЫТЬ ПОТЕНЦИАЛ\n\n"
        f"Опирайтесь на сильные стороны Солнца в {SIGN_PREP[sun.sign]}: создавайте ситуации, где ребёнок может проявлять {CHILD_SUN[sun.sign]}.\n\n"
        f"Для эмоциональной устойчивости особенно важны {MOON_NEED[moon.sign]}. "
        "Если ребёнок перегружен, сначала возвращайте ощущение безопасности, а уже потом требуйте результата.\n\n"
        f"В обучении полезно поддерживать темы {SIGN_GEN[mercury.sign]} и сферу {HOUSE_SYMBOL[meh]}. "
        "Лучше развивать несколько подтверждённых картой сильных направлений и наблюдать, где появляется устойчивый интерес, а не навязывать одну профессию."
    )

def technical(c):
    lines=[
        "EGOIST TECH",
        f"UTC: {c.get('utc')}",
        f"Timezone: {c.get('timezone')}",
        f"ASC: {c['asc'].sign} {c['asc'].degree:.2f}",
        f"MC: {c['mc'].sign} {c['mc'].degree:.2f}",
        "",
        "Планеты:"
    ]
    for name,p in c["planets"].items():
        h=c["houses"].get(name,"?")
        lines.append(f"{name}: {p.sign} {p.degree:.2f}, дом {h}")
    return "\n".join(lines)

@router.message(CommandStart())
async def start(m:Message,state:FSMContext):
    await state.clear()
    await m.answer(
        "ЭГОИСТ |\n\n"
        "Твоя дата рождения может рассказать о тебе намного больше, чем кажется.\n\n"
        "…В чём твоя главная сила и почему, возможно, она до сих пор не раскрыта в полной мере?\n"
        "…Какие у тебя истинные таланты и в чём твоя природная сильная сторона?\n"
        "…Где находится твоя гармония, что даёт тебе энергию и приносит удовольствие?\n"
        "…Что тебе важно усилить, а что, наоборот, научиться ослаблять?\n"
        "…Откуда берутся твои страхи и как с ними работать?\n"
        "…Почему тебя тянет к определённым людям и какие люди притягиваются к тебе?\n"
        "…Где спрятан твой главный потенциал, а где ты, возможно, мешаешь собственной реализации?\n"
        "…Что тебе легче монетизировать, где в натальной карте искать финансовый потенциал и почему, возможно, реализация пока не раскрывается в полной мере?\n\n"
        "На все эти вопросы ответит этот чат 💭\n\n"
        "Начнём с главного - кто же ты?\n\n"
        "Ожидание в несколько секунд - это нормально.\n"
        "Набираемся терпения ✨\n\n"
        "1. Введи дату рождения в формате ДД.ММ.ГГГГ:"
    )
    await state.set_state(Birth.date)


@router.message(Command("menu"))
async def command_menu(m:Message,state:FSMContext):
    await state.clear()
    await m.answer("Куда посмотрим дальше?", reply_markup=menu())

@router.message(Command("author"))
async def command_author(m:Message):
    text=(
        "✨ Личный разбор с астрологом и автором EGOIST\n\n"
        "Если тебе хочется пойти глубже и получить персональный разбор своей карты, "
        "ты можешь обратиться напрямую к астрологу и автору EGOIST.\n\n"
        "Выбери тему, которая тебе сейчас наиболее важна:"
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
        await state.clear()
        who_markup = menu() if has_unlock(m.from_user.id, "who") else unlock_kb("who")
        who_text = await who_ai_text(c, paid=has_unlock(m.from_user.id, "who"))
        await send_long(m.answer, who_text, reply_markup=who_markup)
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

    try:
        unlocked = has_unlock(q.from_user.id, sec)
        text = await section_ai_text(p["chart"], sec, paid=unlocked)
        await send_long(q.message.answer, text, reply_markup=menu() if unlocked else unlock_kb(sec))
    except Exception as e:
        await q.message.answer(f"Не получилось открыть раздел. Ошибка: {type(e).__name__}: {e}")
    await q.answer()


@router.callback_query(F.data=="author:menu")
async def author_menu(q:CallbackQuery):
    text=(
        "✨ Личный разбор с астрологом и автором EGOIST\n\n"
        "Если тебе хочется пойти глубже и получить персональный разбор своей карты, "
        "ты можешь обратиться напрямую к астрологу и автору EGOIST.\n\n"
        "Выбери тему, которая тебе сейчас наиболее важна:"
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
    text=f"Ты выбрала:\n\n{label}\n\n"
    if username:
        text+="Нажми кнопку ниже, чтобы перейти в личный диалог с автором EGOIST."
    else:
        text+="Контакт автора пока не подключён."
    await q.message.answer(text,reply_markup=author_contact_kb())
    await q.answer()

@router.callback_query(F.data.startswith("buy:"))
async def buy(q:CallbackQuery,bot:Bot):
    sec=q.data.split(":")[1]
    if has_unlock(q.from_user.id, sec):
        await q.answer("Этот раздел уже куплен - повторно платить не нужно ✨", show_alert=True)
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
            child_text = await section_ai_text(cc, "child", paid=True)
            await send_long(m.answer, child_text, reply_markup=menu())
        else:
            await m.answer("Введи дату рождения ребёнка ДД.ММ.ГГГГ:")
            await state.set_state(Child.date)
        return

    if not p:
        await m.answer("Теперь нажми /start и введи данные рождения, чтобы открыть разбор.")
        return

    text = await section_ai_text(p["chart"], sec, paid=True)
    await send_long(m.answer, text, reply_markup=menu())


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
        child_text = await section_ai_text(c, "child", paid=unlocked)
        await send_long(m.answer, child_text, reply_markup=menu() if unlocked else unlock_kb("child"))
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
