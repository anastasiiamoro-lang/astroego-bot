
import asyncio, os, sqlite3
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
    "Скорпион": "ядро личности стремится к глубине, контролю над собой и внутренней трансформации",
    "Стрелец": "ядро личности раскрывается через смысл, рост, свободу и расширение горизонтов",
    "Козерог": "ядро личности строится вокруг зрелости, результата, структуры и внутренней ответственности",
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
    "Весы": "для внутреннего равновесия важны гармония, красота, контакт и отсутствие грубой конфронтации",
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
    4: "через дом, семью, корни, приватную жизнь и внутреннюю безопасность",
    5: "через творчество, романтику, удовольствие, детей и личное самовыражение",
    6: "через работу, навыки, рутину, здоровье и ежедневную эффективность",
    7: "через партнёрство, отношения, клиентов и взаимодействие один на один",
    8: "через близость, общие ресурсы, кризисы и глубокие изменения",
    9: "через образование, путешествия, мировоззрение, иностранную среду и смысл",
    10: "через карьеру, статус, цель, репутацию и общественную реализацию",
    11: "через друзей, сообщества, аудиторию, планы и коллективные проекты",
    12: "через внутреннюю жизнь, уединение, скрытые процессы и психологическую глубину",
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

async def locate(city):
    loc=await asyncio.to_thread(geo.geocode,city,language="ru",exactly_one=True)
    if not loc: raise ValueError("Не нашла город. Напиши город и страну.")
    return float(loc.latitude),float(loc.longitude)



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
    4:"дом, семью, родителей, корни, недвижимость и внутреннюю безопасность",
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
        return "добавляет глубину, сильную вовлечённость и тему контроля" if tense else "добавляет психологическую глубину и внутреннюю силу"
    if other=="Сатурн":
        return "добавляет самоконтроль, серьёзность и страх ошибиться" if tense else "даёт выдержку, надёжность и способность строить надолго"
    if other=="Уран":
        return "усиливает потребность в свободе и резкую реакцию на ограничения" if tense else "даёт оригинальность и независимость"
    if other=="Нептун":
        return "усиливает идеализацию, впечатлительность и размытые границы" if tense else "усиливает интуицию, творчество и тонкое восприятие"
    if other=="Марс":
        return "добавляет импульсивность, напор и внутреннюю конфликтность" if tense else "добавляет смелость и инициативу"
    if other=="Юпитер":
        return "может раскачивать крайности и завышать ожидания" if tense else "расширяет возможности и уверенность"
    if other=="Венера":
        return "создаёт внутренний конфликт между желанием и гармонией" if tense else "смягчает проявление и добавляет вкус"
    if other=="Луна":
        return "делает тему эмоционально заряженной" if tense else "соединяет волю с эмоциональными потребностями"
    if other=="Солнце":
        return "делает тему частью самоощущения и личной воли"
    if other=="Меркурий":
        return "сильно связывает тему с мышлением и словами"
    return ""

def who_preview(c):
    asc=c["asc"]
    sun=_p(c,"Солнце"); moon=_p(c,"Луна")
    sh=_house(c,"Солнце"); mh=_house(c,"Луна")
    disp=RULERS[sun.sign]; dp=_p(c,disp); dh=_house(c,disp)
    shadow=OPPOSITE[sun.sign]

    sun_mod=""
    rows=_aspect_rows(c,"Солнце",False,5)
    if rows:
        mod=_aspect_modifier("Солнце",rows[0])
        if mod: sun_mod=" В твоей карте это ещё сильнее окрашено тем, что связь Солнца с другими планетами "+mod+"."

    return (
        "КТО Я ✦\n\n"
        "Как ты проявляешься\n\n"
        f"Твой Асцендент в {SIGN_PREP[asc.sign]}. {ASC_PRESENT[asc.sign]}. "
        f"Так ты чаще входишь в новые ситуации и именно такой слой тебя люди замечают одним из первых.\n\n"
        "Твоё ядро\n\n"
        f"У тебя Солнце в {SIGN_PREP[sun.sign]} в {sh} доме. {SUN_CORE[sun.sign].capitalize()}. "
        f"Поскольку Солнце стоит в сфере про {HOUSE_SYMBOL[sh]}, именно здесь особенно важно чувствовать, что ты живёшь свою жизнь."
        f"{sun_mod} "
        f"Управитель твоего Солнца {disp} находится в {SIGN_PREP[dp.sign]} в {dh} доме, поэтому солнечная тема дополнительно ведёт тебя в сферу про {HOUSE_SYMBOL[dh]}.\n\n"
        "Твоя тень\n\n"
        f"Противоположный знак твоего Солнца - {shadow}. Его качества не отменяют твою природу, а помогают не уходить в крайность. "
        f"Тебе полезно добавлять в жизнь качества, связанные с темами: {SIGN_SYMBOL[shadow]}.\n\n"
        "Что тебе нужно эмоционально\n\n"
        f"Луна в {SIGN_PREP[moon.sign]} в {mh} доме показывает, что для восстановления тебе особенно нужны {MOON_NEED[moon.sign]}. "
        f"Сильнее всего эмоциональная тема проявляется через {HOUSE_SYMBOL[mh]}.\n\n"
        "Если коротко\n\n"
        f"В тебе соединяются внешняя манера {SIGN_GEN[asc.sign]}, солнечная природа {SIGN_GEN[sun.sign]} и эмоциональные потребности {SIGN_GEN[moon.sign]}. "
        "Твоя задача не выбирать между этими частями, а научиться использовать каждую в подходящей ситуации."
    )

def who_deep(c):
    sun=_p(c,"Солнце"); moon=_p(c,"Луна")
    shadow=OPPOSITE[sun.sign]
    return (
        who_preview(c)
        + "\n\n🔓 КАК РАСКРЫТЬ СЕБЯ СИЛЬНЕЕ\n\n"
        f"Твоё Солнце лучше всего раскрывается, когда ты регулярно создаёшь условия для тем {SIGN_GEN[sun.sign]} и не ждёшь внешнего разрешения на проявление. "
        f"Особенно важно действовать через сферу {HOUSE_SYMBOL[_house(c,'Солнце')]}.\n\n"
        f"Что может гасить тебя: попытка слишком долго жить только по чужому ожиданию или отрицать собственный солнечный способ проявления. "
        f"Полезный баланс даёт {shadow}: добавляй {SIGN_SYMBOL[shadow]}, не отказываясь от сильных качеств {SIGN_GEN[sun.sign]}.\n\n"
        f"Для эмоционального восстановления ориентируйся на Луну: тебе полезны {MOON_NEED[moon.sign]}. "
        "Чем раньше ты возвращаешь себе базовый эмоциональный комфорт, тем меньше энергии уходит на внутреннее напряжение."
    )

def love_preview(c):
    v=_p(c,"Венера"); vh=_house(c,"Венера")
    moon=_p(c,"Луна"); mars=_p(c,"Марс")
    dsc=_dsc_sign(c)
    vrows=_aspect_rows(c,"Венера",False,5)
    obstacle=""
    for r in vrows:
        if r.get("aspect") in TENSE:
            mod=_aspect_modifier("Венера",r)
            if mod:
                obstacle=f" Один из возможных сложных сценариев: напряжённая связь Венеры {mod}."
                break
    return (
        "ВСЁ О ЛЮБВИ ❤\n\n"
        "Венера - планета любви, красоты, удовольствия и материального благополучия. "
        "Её положение показывает, как ты проживаешь любовь, что приносит тебе удовольствие и что ты считаешь ценным.\n\n"
        "Кто тебя притягивает и кто притягивается к тебе\n\n"
        f"Твой Десцендент в {SIGN_PREP[dsc]}. В значимые отношения могут особенно часто входить люди, в которых заметны {SIGN_SYMBOL[dsc]}. "
        f"Рядом с тобой эти качества партнёра со временем тоже могут проявляться сильнее.\n\n"
        "Как ты любишь и что тебе нужно в отношениях\n\n"
        f"Венера в {SIGN_PREP[v.sign]}: {VENUS_LOVE[v.sign]}. "
        f"Поскольку она находится в {vh} доме, любовь и удовольствие особенно переплетаются со сферой про {HOUSE_SYMBOL[vh]}. "
        f"Луна добавляет потребность в {MOON_NEED[moon.sign]}, а Марс в {SIGN_PREP[mars.sign]} показывает, что в притяжении тебе важны темы {SIGN_SYMBOL[mars.sign]}.\n\n"
        "Что приносит тебе удовольствие\n\n"
        f"Твоя Венера особенно хорошо откликается на {SIGN_SYMBOL[v.sign]}. Это относится не только к отношениям, но и к эстетике, отдыху, вкусу и способу наполнять себя.\n\n"
        "Что может мешать гармонии\n\n"
        f"Главная задача - не заставлять себя любить чужим способом и не уходить в крайности собственного сценария.{obstacle}\n\n"
        "Если коротко\n\n"
        f"Тебе нужен формат отношений, где одновременно уважают твою Венеру в {SIGN_PREP[v.sign]}, эмоциональные потребности Луны в {SIGN_PREP[moon.sign]} и тип партнёрства {SIGN_GEN[dsc]}."
    )

def love_deep(c):
    v=_p(c,"Венера"); opp=OPPOSITE[v.sign]
    return (
        love_preview(c)
        + "\n\n🔓 ТВОЙ СЦЕНАРИЙ ЛЮБВИ И ГАРМОНИИ\n\n"
        f"Для долгих отношений тебе особенно подходит человек, который не ломает природный стиль твоей Венеры в {SIGN_PREP[v.sign]}, но помогает добавлять зрелые качества {SIGN_GEN[opp]}.\n\n"
        f"Чтобы сохранять близость и интерес, важно регулярно оставлять место для тем {SIGN_GEN[v.sign]} и одновременно развивать {SIGN_SYMBOL[opp]}. "
        "Так отношения не превращаются ни в подавление, ни в бесконечное раскачивание крайностей.\n\n"
        "В уязвимые моменты сначала замечай свою автоматическую реакцию, а уже потом выясняй отношения. "
        "Это снижает вероятность действовать из страха, контроля, обиды или импульса.\n\n"
        f"Для наполненности вне отношений тебе особенно полезны удовольствия и среда, связанные с темами {SIGN_GEN[v.sign]}. "
        "Чем больше у тебя собственной наполненной жизни, тем меньше отношениям приходится нести на себе всю функцию удовольствия и самоценности."
    )

CAREER_SIGN = {
    "Овен":"инициатива, быстрые решения, конкуренция и самостоятельные проекты",
    "Телец":"ресурсы, качество, деньги, комфорт, красота и материальный результат",
    "Близнецы":"коммуникация, языки, обучение, продажи, медиа и информация",
    "Рак":"люди, забота, дом, недвижимость, питание и создание атмосферы",
    "Лев":"творчество, лидерство, сцена, дети, события и личный бренд",
    "Дева":"аналитика, систематизация, качество, медицина, обучение и мастерство",
    "Весы":"переговоры, право, эстетика, консультации, клиенты и партнёрство",
    "Скорпион":"психология, кризисы, исследование, финансы, медицина и сложные ресурсы",
    "Стрелец":"образование, международные проекты, право, путешествия и преподавание",
    "Козерог":"управление, стратегия, структура, статус, строительство и долгие проекты",
    "Водолей":"IT, технологии, сообщества, инновации, онлайн и нестандартные продукты",
    "Рыбы":"творчество, психология, музыка, кино, помощь, медицина и работа за кадром"
}

def _house_ruler(c, h):
    return RULERS[_cusp_sign(c,h)]

def career_preview(c):
    sun=_p(c,"Солнце"); sh=_house(c,"Солнце")
    mercury=_p(c,"Меркурий"); mars=_p(c,"Марс")
    h2=_cusp_sign(c,2); h6=_cusp_sign(c,6); h10=_cusp_sign(c,10)
    r2=_house_ruler(c,2); r6=_house_ruler(c,6); r10=_house_ruler(c,10)
    dirs=[
        CAREER_SIGN[sun.sign],
        CAREER_SIGN[_p(c,r10).sign],
        CAREER_SIGN[_p(c,r6).sign],
    ]
    return (
        "ТАЛАНТЫ, КАРЬЕРА И ДЕНЬГИ ✦\n\n"
        "Твои таланты\n\n"
        f"Солнце в {SIGN_PREP[sun.sign]} в {sh} доме даёт сильную способность развиваться через {CAREER_SIGN[sun.sign]}. "
        f"Меркурий в {SIGN_PREP[mercury.sign]} добавляет талант к темам {SIGN_SYMBOL[mercury.sign]}, а Марс в {SIGN_PREP[mars.sign]} показывает, как ты включаешься в действие.\n\n"
        "Как ты проявляешься в работе\n\n"
        f"VI дом начинается в {SIGN_PREP[h6]}, его управитель {r6} стоит в {SIGN_PREP[_p(c,r6).sign]} в {_house(c,r6)} доме. "
        f"Поэтому тебе важно не просто работать, а постепенно наращивать навык через сферу {HOUSE_SYMBOL[_house(c,r6)]}. "
        f"X дом и профессиональная реализация окрашены {SIGN_GEN[h10]}, а его управитель {r10} ведёт в сферу {HOUSE_SYMBOL[_house(c,r10)]}.\n\n"
        "Какие направления тебе могут подойти\n\n"
        f"По повторяющимся темам карты особенно интересны: {_clean_join(dirs,3)}. "
        "Это направления, а не единственная профессия: сильнее всего работает сочетание нескольких компетенций.\n\n"
        "Как у тебя устроена тема денег\n\n"
        f"II дом начинается в {SIGN_PREP[h2]}. Поэтому деньги психологически связаны для тебя с темами {SIGN_SYMBOL[h2]}. "
        "Здесь важно чувствовать, что собственный ресурс зависит не только от обстоятельств, но и от твоих решений.\n\n"
        "Если коротко\n\n"
        f"Твой сильный профессиональный вектор соединяет природу {SIGN_GEN[sun.sign]}, рабочую логику {SIGN_GEN[h6]} и карьерную задачу {SIGN_GEN[h10]}. "
        "Лучше всего ты раскрываешься там, где можешь расти в компетентности и постепенно усиливать влияние на результат."
    )

def career_deep(c):
    r2=_house_ruler(c,2); p2=_p(c,r2); rh=_house(c,r2)
    h8=_cusp_sign(c,8); r8=_house_ruler(c,8)
    return (
        career_preview(c)
        + "\n\n🔓 КАК УВЕЛИЧИТЬ СВОЙ ДОХОД\n\n"
        f"Твой основной финансовый ресурс ищем через управителя II дома - {r2}. Он стоит в {SIGN_PREP[p2.sign]} в {rh} доме. "
        f"Поэтому денежный канал особенно связан со сферой {HOUSE_SYMBOL[rh]} и способом действия {SIGN_GEN[p2.sign]}.\n\n"
        f"Лучше всего монетизируются навыки, в которых повторяются темы {CAREER_SIGN[p2.sign]}. "
        "При выборе направления проверяй не только интерес, но и возможность сделать компетенцию понятной рынку и измеримой по результату.\n\n"
        f"VIII дом окрашен {SIGN_GEN[h8]}, его управитель {r8} находится в {_house(c,r8)} доме. "
        "Это показывает, как в финансовую систему могут включаться совместные ресурсы, бизнес, инвестиции, комиссии или деньги партнёров.\n\n"
        "Чтобы зарабатывать больше, важно не распыляться на все способности сразу. Выбирай одну сильную связку навыков, "
        "делай её узнаваемой и постепенно повышай стоимость своей компетенции."
    )

NODE_PLANET_N = {
    "Солнце":"самовыражение, лидерство и право занимать своё место",
    "Луна":"эмоциональная зрелость, забота, дом и способность принимать поддержку",
    "Меркурий":"речь, письмо, языки, обучение, аналитику и передачу знаний",
    "Венера":"отношения, красоту, вкус, гармонию и собственную ценность",
    "Марс":"решительность, инициативу, действие и защиту своих интересов",
    "Юпитер":"образование, преподавание, международность и масштаб",
    "Сатурн":"дисциплину, терпение, структуру, ответственность и профессионализм",
    "Уран":"нестандартное мышление, технологии, IT, астрологию и инновации",
    "Нептун":"творчество, музыку, образное мышление, интуицию и эмпатию",
    "Плутон":"внутреннюю силу, трансформацию, кризисоустойчивость и глубокие изменения"
}

def _node_conjunctions(c, node):
    out=[]
    n=_p(c,node)
    for name,p in c["planets"].items():
        if name in ("Северный узел","Южный узел"): continue
        diff=abs((n.longitude-p.longitude+180)%360-180)
        if diff <= 6:
            out.append((diff,name))
    return [name for _,name in sorted(out)]

def nodes_preview(c):
    nn=_p(c,"Северный узел"); sn=_p(c,"Южный узел")
    nh=_house(c,"Северный узел"); sh=_house(c,"Южный узел")
    nr=RULERS[nn.sign]; ncon=_node_conjunctions(c,"Северный узел")
    scon=_node_conjunctions(c,"Южный узел")
    nadd=""
    if ncon:
        nadd=" Особенно важны качества планет рядом с Северным узлом: "+_clean_join([NODE_PLANET_N.get(x,x) for x in ncon],3)+"."
    sadd=""
    if scon:
        sadd=" Рядом с Южным узлом стоят планеты, усиливающие знакомый багаж: "+_clean_join([NODE_PLANET_N.get(x,x) for x in scon],3)+"."
    return (
        "КАРМИЧЕСКИЕ УЗЛЫ ✦\n\n"
        "Южный узел показывает опыт и качества, которые уже хорошо тебе знакомы. Северный узел показывает направление развития.\n\n"
        "С чем ты пришла\n\n"
        f"Южный узел в {SIGN_PREP[sn.sign]} в {sh} доме указывает на знакомый опыт, связанный с темами: {SIGN_SYMBOL[sn.sign]}. "
        f"Эти качества особенно привычно проживаются через сферу {HOUSE_SYMBOL[sh]}.{sadd}\n\n"
        "Куда тебя ведёт жизнь\n\n"
        f"Северный узел в {SIGN_PREP[nn.sign]} в {nh} доме ведёт к развитию через {SIGN_SYMBOL[nn.sign]}. "
        f"Жизнь будет снова возвращать тебя к сфере {HOUSE_SYMBOL[nh]}, пока она не станет твоей собственной компетенцией и опорой.{nadd}\n\n"
        "Что притягивается в твою жизнь\n\n"
        f"Ось {sn.sign} - {nn.sign} создаёт повторяющийся переход: от привычных тем {SIGN_GEN[sn.sign]} к качествам {SIGN_GEN[nn.sign]}. "
        "Поэтому обстоятельства могут снова и снова ставить тебя перед выбором между знакомым способом и новым уровнем зрелости.\n\n"
        "Твои кармические учителя\n\n"
        f"Важными учителями могут становиться {nn.sign}и по типу знака или люди с сильным {nr}. "
        f"Они могут приносить в твою жизнь качества, связанные с темами {SIGN_GEN[nn.sign]}, и тем самым подталкивать тебя к развитию.\n\n"
        "Если коротко\n\n"
        f"Твой путь идёт от хорошо знакомого опыта {SIGN_GEN[sn.sign]} к освоению качеств {SIGN_GEN[nn.sign]}, "
        f"особенно через сферу {HOUSE_SYMBOL[nh]}."
    ).replace("Ракии","Раки").replace("Весыи","Весы").replace("Рыбыи","Рыбы").replace("Близнецыи","Близнецы")

def nodes_deep(c):
    nn=_p(c,"Северный узел"); sn=_p(c,"Южный узел")
    return (
        nodes_preview(c)
        + "\n\n🔓 КАК ПРОЙТИ СВОИ КАРМИЧЕСКИЕ УРОКИ\n\n"
        f"В первую очередь развивай качества Северного узла в {SIGN_PREP[nn.sign]}: {SIGN_SYMBOL[nn.sign]}. "
        f"Не пытайся отказаться от Южного узла в {SIGN_PREP[sn.sign]}. Его опыт нужен тебе как ресурс, но он не должен оставаться единственным способом жить.\n\n"
        f"Практически твой путь проходит через сферу {HOUSE_SYMBOL[_house(c,'Северный узел')]}. "
        "Выбирай действия, в которых приходится осваивать эту сферу самой, а не только наблюдать за ней со стороны.\n\n"
        f"Когда жизнь снова предлагает привычную реакцию {SIGN_GEN[sn.sign]}, задавай себе вопрос: "
        f"«Как сейчас могла бы поступить моя более зрелая часть по {SIGN_GEN[nn.sign]}?» "
        "Так повторяющийся сценарий постепенно превращается из автоматической реакции в осознанный выбор."
    )

BALANCE_ACTION = {
    "Овен":"движение, спорт, прямое действие и право быстро обозначать желание",
    "Телец":"телесный комфорт, спокойный ритм, качество, удовольствие и материальную устойчивость",
    "Близнецы":"разговор, вопросы, обучение, гибкость и смену точки зрения",
    "Рак":"дом, мягкость, заботу о себе, близость и способность принимать поддержку",
    "Лев":"тепло, творчество, яркость, игру и открытое выражение чувств",
    "Дева":"режим, конкретные шаги, практичность, навык и порядок",
    "Весы":"мягкость, женственность, эстетику, чувство меры, дипломатичность и партнёрский баланс",
    "Скорпион":"глубину, честность, сильную физическую разрядку и способность выдерживать интенсивность",
    "Стрелец":"перспективу, обучение, движение, юмор и расширение горизонтов",
    "Козерог":"границы, дисциплину, взрослость, реализм и долгий план",
    "Водолей":"дистанцию, свободу, друзей, новизну и интеллектуальное переключение",
    "Рыбы":"творчество, воду, музыку, сон, интуицию и умение отпускать контроль"
}

def _growth_candidates(c):
    score=[]
    for planet in ("Солнце","Луна","Венера","Марс","Меркурий"):
        p=_p(c,planet)
        rows=_aspect_rows(c,planet,True,6)
        issue=_dignity_issue(planet,p.sign)
        s=(3 if issue else 0)+len(rows)*2
        if planet in ("Солнце","Луна"): s+=1
        if s:
            score.append((s,planet,issue,rows))
    score.sort(reverse=True)
    return score[:4]

def _growth_problem(planet, other):
    pairs={
        ("Луна","Плутон"):"чувства могут становиться очень интенсивными, включать фиксацию, контроль или страх потери",
        ("Луна","Сатурн"):"может быть сложно показывать слабость, просить поддержку и вовремя расслабляться",
        ("Луна","Уран"):"эмоции могут резко менять направление, а при перегрузе хочется мгновенно дистанцироваться",
        ("Луна","Нептун"):"может усиливаться впечатлительность, идеализация и путаница в чувствах",
        ("Венера","Плутон"):"в близости могут включаться ревность, проверки, крайности или страх потери",
        ("Венера","Сатурн"):"может появляться страх отвержения и ощущение, что любовь нужно заслуживать",
        ("Венера","Уран"):"сильна потребность в свободе, а скука или давление могут резко выключать интерес",
        ("Марс","Плутон"):"давление способно запускать силовую реакцию, борьбу за контроль и накопленную агрессию",
        ("Марс","Сатурн"):"энергия может то блокироваться, то выходить после долгого терпения",
        ("Солнце","Плутон"):"сильна реакция на контроль, борьбу за влияние и ситуации, где трудно отпустить",
        ("Солнце","Сатурн"):"может быть слишком строгая внутренняя оценка и страх ошибиться",
        ("Солнце","Уран"):"ограничения могут провоцировать резкий протест и желание всё оборвать",
        ("Меркурий","Нептун"):"мысли могут перегружаться догадками, сомнениями и неясностью",
        ("Меркурий","Плутон"):"ум способен фиксироваться на теме и докапываться до неё до полного истощения",
        ("Меркурий","Сатурн"):"можно слишком долго проверять себя и бояться сказать или решить неправильно",
    }
    return pairs.get((planet,other)) or pairs.get((other,planet)) or "напряжение может усиливать крайние реакции и внутренний конфликт"

def growth_preview(c):
    cand=_growth_candidates(c)
    if not cand:
        return (
            "ТОЧКИ РОСТА ✦\n\n"
            "В твоей карте нет одной явно доминирующей напряжённой личной планеты. "
            "Поэтому точки роста лучше искать в более тонких повторяющихся сценариях и управителях домов."
        )
    parts=[]
    triggers=[]
    powers=[]
    for _,planet,issue,rows in cand[:3]:
        p=_p(c,planet)
        desc=[]
        if issue:
            desc.append(f"{planet} в {SIGN_PREP[p.sign]} находится в положении {issue}, поэтому его функция требует более осознанного обращения")
        if rows:
            r=rows[0]; desc.append(_growth_problem(planet,r.get("planet","")))
            triggers.append(r.get("planet",""))
        opp=OPPOSITE[p.sign]
        powers.append(f"{planet}: сохранить сильные качества {SIGN_GEN[p.sign]} и добавить зрелые качества {SIGN_GEN[opp]}")
        parts.append(" ".join(x.rstrip(".") for x in desc)+".")
    return (
        "ТОЧКИ РОСТА ✦\n\n"
        "Что даётся тебе сложнее\n\n"
        + "\n\n".join(parts)
        + "\n\nЧто тебя выбивает\n\n"
        + "Сильнее всего напряжение может включаться там, где затронуты контроль, критика, ограничения, страх потери, неопределённость или необходимость резко менять привычный сценарий.\n\n"
        "Как ты реагируешь под давлением\n\n"
        "Сначала ты можешь пытаться удержать ситуацию привычным способом, а если напряжение продолжает расти - уходить в крайность исходной планеты. "
        "Поэтому важно замечать реакцию раньше, чем она начинает управлять поведением.\n\n"
        "В чём здесь твоя сила\n\n"
        + " ".join(powers[:3]) + ". Напряжение не нужно уничтожать: в нём находится большой запас энергии, если дать ему зрелый выход.\n\n"
        "Если коротко\n\n"
        "Твои сложные места одновременно являются зонами большого потенциала. Главная задача - не подавлять их, а научиться управлять интенсивностью."
    )

def growth_deep(c):
    cand=_growth_candidates(c)
    blocks=[]
    for _,planet,issue,rows in cand[:3]:
        p=_p(c,planet); opp=OPPOSITE[p.sign]
        problem=_growth_problem(planet,rows[0].get("planet","")) if rows else (
            f"{planet} в {SIGN_PREP[p.sign]} требует более осознанного использования своей функции"
        )
        blocks.append(
            f"Как гармонизировать {planet}\n\n"
            f"Что создаёт напряжение: {problem}.\n\n"
            f"Что важно сохранить: природную энергию {SIGN_GEN[p.sign]} - {SIGN_SYMBOL[p.sign]}.\n\n"
            f"Чего стоит добавить: качества {SIGN_GEN[opp]} - {SIGN_SYMBOL[opp]}.\n\n"
            f"Что делать на практике: регулярно добавлять в жизнь {BALANCE_ACTION[opp]}, "
            f"при этом давать исходной энергии безопасный выход через {BALANCE_ACTION[p.sign]}.\n\n"
            "Во что это может превратиться: в более зрелую, управляемую версию той же силы, "
            "где энергия работает на тебя, а не захватывает реакцию."
        )
    return (
        growth_preview(c)
        + "\n\n🔓 КАК ПРЕВРАТИТЬ СЛАБЫЕ МЕСТА В СИЛУ\n\n"
        + "\n\n".join(blocks)
    )

def new_preview(c, sec):
    if sec=="who": return who_preview(c)
    if sec=="love": return love_preview(c)
    if sec=="career_money": return career_preview(c)
    if sec=="nodes": return nodes_preview(c)
    if sec=="growth": return growth_preview(c)
    raise KeyError(sec)

def new_deep(c, sec):
    if sec=="who": return who_deep(c)
    if sec=="love": return love_deep(c)
    if sec=="career_money": return career_deep(c)
    if sec=="nodes": return nodes_deep(c)
    if sec=="growth": return growth_deep(c)
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
    await m.answer("ЭГОИСТ\nЗдесь всё о тебе.\n\nНачнём с главного - кто ты?\n\nВведи дату рождения в формате ДД.ММ.ГГГГ:")
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
