
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
import math
import swisseph as swe
from timezonefinder import TimezoneFinder

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
        degree=lon % 30
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
    return f"{d}°{m:02d}′{s:02d}″"

def utc_from_local(date_str, time_str, lat, lon):
    tf = TimezoneFinder()
    tz_name = tf.timezone_at(lat=lat, lng=lon)
    if not tz_name:
        raise ValueError("Не удалось определить часовой пояс.")
    local_dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
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
        result[name] = point_from_longitude(name, xx[0])
    return result

def calculate_placidus_houses(jd_ut, lat, lon):
    cusps, ascmc = swe.houses(jd_ut, lat, lon, b'P')
    # pyswisseph usually returns 12 cusps already indexed 0..11
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
        start = cusps[i]
        end = cusps[(i + 1) % 12]
        if in_arc(lon, start, end):
            return i + 1
    raise RuntimeError("Не удалось определить дом.")

def cusp_sign(cusp_lon):
    return int(normalize(cusp_lon) // 30)

def intercepted_signs(cusps):
    cusp_signs = [cusp_sign(c) for c in cusps]
    used = set(cusp_signs)
    intercepted = []
    for s in range(12):
        if s not in used:
            # Ищем дом, внутри дуги которого лежит середина знака
            midpoint = s * 30 + 15
            h = house_of_longitude(midpoint, cusps)
            intercepted.append((SIGNS[s], h))
    return intercepted

def angular_distance(a, b):
    d = abs(normalize(a) - normalize(b))
    return min(d, 360 - d)

def exact_orb(a_lon, b_lon, aspect_name):
    d = angular_distance(a_lon, b_lon)
    target = EXACT_ANGLES[aspect_name]
    return abs(d - target)

def whole_sign_aspect(a, b):
    diff = (b.sign_index - a.sign_index) % 12
    return WHOLE_SIGN_ASPECTS.get(diff)

def strength_label(other_name, orb):
    # Авторская MVP-логика: полнознаковый аспект существует всегда,
    # а градусная точность повышает вес.
    if orb <= 1:
        return "ДОМИНИРУЮЩИЙ"
    if other_name in OUTER and orb <= 2:
        return "ОЧЕНЬ СИЛЬНЫЙ"
    if orb <= 3:
        return "СИЛЬНЫЙ"
    if orb <= 6:
        return "ЗАМЕТНЫЙ"
    return "ФОНОВЫЙ"

def venus_aspects(planets):
    venus = planets["Венера"]
    result = []
    for name, p in planets.items():
        if name == "Венера":
            continue
        asp = whole_sign_aspect(venus, p)
        if not asp:
            continue
        orb = exact_orb(venus.longitude, p.longitude, asp)
        result.append({
            "planet": name,
            "aspect": asp,
            "orb": orb,
            "strength": strength_label(name, orb),
            "planet_point": p,
        })
    order = {"ДОМИНИРУЮЩИЙ": 0, "ОЧЕНЬ СИЛЬНЫЙ": 1, "СИЛЬНЫЙ": 2, "ЗАМЕТНЫЙ": 3, "ФОНОВЫЙ": 4}
    result.sort(key=lambda x: (order[x["strength"]], x["orb"]))
    return result

def rulers_for_sign(sign_index):
    # Современное управление с традиционным соуправителем в скобках.
    modern = {
        0: ["Марс"], 1: ["Венера"], 2: ["Меркурий"], 3: ["Луна"],
        4: ["Солнце"], 5: ["Меркурий"], 6: ["Венера"], 7: ["Плутон", "Марс"],
        8: ["Юпитер"], 9: ["Сатурн"], 10: ["Уран", "Сатурн"], 11: ["Нептун", "Юпитер"]
    }
    return modern[sign_index]

def houses_ruled_by_venus(cusps):
    ruled = []
    for idx, cusp in enumerate(cusps, start=1):
        sign_i = cusp_sign(cusp)
        if "Венера" in rulers_for_sign(sign_i):
            ruled.append((idx, SIGNS[sign_i]))
    return ruled

def venus_interpretation(venus, venus_house, aspects, ruled_houses):
    # В MVP трактовка intentionally rule-based, чтобы проверить механику.
    # Позже этот структурированный результат можно отдавать LLM для синтеза.
    sign_text = {
        "Овен": "Венера проявляется прямо, быстро и инициативно: человеку важно живое чувство, движение и искра.",
        "Телец": "Венера сильна в темах чувственности, устойчивости, вкуса, телесного комфорта и материальной ценности.",
        "Близнецы": "Венере нужен интеллектуальный обмен: симпатия часто начинается с разговора, любопытства и лёгкости.",
        "Рак": "Венера связывает любовь с эмоциональной безопасностью, заботой, домом и ощущением близости.",
        "Лев": "Венера хочет яркого проявления чувств, признания, щедрости и ощущения особенной любви.",
        "Дева": "Венера выражает чувство через внимание к деталям, полезность, заботу делами и избирательность.",
        "Весы": "Венера особенно естественно проявляется через партнёрство, эстетику, дипломатичность и поиск гармонии.",
        "Скорпион": "Венера стремится к глубине, интенсивности и полной эмоциональной вовлечённости; поверхностные связи быстро теряют интерес.",
        "Стрелец": "Венере нужны пространство, развитие, совместные впечатления, путешествия или расширение горизонтов.",
        "Козерог": "Венера серьёзно относится к ценности отношений: важны надёжность, зрелость, статус и перспектива.",
        "Водолей": "Венере нужны свобода, интеллектуальная близость и ощущение, что отношения не лишают человека индивидуальности.",
        "Рыбы": "Венера очень восприимчива к атмосфере, романтике и эмоциональному слиянию; важна граница между эмпатией и идеализацией.",
    }
    house_text = {
        1: "Венерианские качества заметны в образе, манере общения и способе предъявлять себя миру.",
        2: "Венера сильно связана с самоценностью, деньгами, ресурсами и тем, что человек считает качественной жизнью.",
        3: "Венера проявляется через речь, обучение, знакомство, переписку, ближайшее окружение и интеллектуальный контакт.",
        4: "Темы любви, вкуса и ценностей глубоко связаны с домом, семьёй, прошлым и внутренним ощущением безопасности.",
        5: "Венера ярко проявляется в романтике, удовольствии, творчестве, сценичности и теме детей.",
        6: "Венерианская тема включается через работу, повседневность, заботу, привычки и качество ежедневной среды.",
        7: "Отношения и партнёрство становятся одной из центральных зон реализации Венеры.",
        8: "Любовь и ценности связаны с глубокой близостью, общими ресурсами, доверием, кризисами и трансформациями.",
        9: "Венера ищет красоту и любовь через знания, путешествия, мировоззрение, иностранную среду или обучение.",
        10: "Венера заметно влияет на статус, профессию, публичный образ и способность нравиться социальной среде.",
        11: "Венера проявляется через дружбу, сообщества, аудиторию, планы на будущее и социальные связи.",
        12: "Венерианская жизнь имеет сильный внутренний, скрытый или приватный пласт; чувства могут долго проживаться не публично.",
    }
    lines = [
        f"Венера: {venus.sign} {format_degree(venus.degree)}, {venus_house} дом.",
        sign_text[venus.sign],
        house_text[venus_house],
    ]
    if ruled_houses:
        rh = ", ".join(f"{h} дом ({s})" for h, s in ruled_houses)
        lines.append(f"В этой карте Венера управляет: {rh}. Поэтому её состояние и аспекты дополнительно затрагивают эти дома.")
    if aspects:
        lines.append("Ключевые полнознаковые аспекты Венеры:")
        for a in aspects:
            lines.append(
                f"• {a['aspect'].capitalize()} с {a['planet']}: "
                f"градусный орбис {a['orb']:.2f}° — {a['strength'].lower()}."
            )
    return "\n".join(lines)

def build_chart(date_str, time_str, lat, lon):
    utc_dt, tz_name = utc_from_local(date_str, time_str, lat, lon)
    jd = julian_day(utc_dt)
    planets = calculate_planets(jd)
    cusps, asc, mc = calculate_placidus_houses(jd, lat, lon)
    venus = planets["Венера"]
    venus_house = house_of_longitude(venus.longitude, cusps)
    aspects = venus_aspects(planets)
    intercepted = intercepted_signs(cusps)
    ruled_houses = houses_ruled_by_venus(cusps)
    return {
        "utc": utc_dt,
        "timezone": tz_name,
        "planets": planets,
        "cusps": cusps,
        "asc": asc,
        "mc": mc,
        "venus": venus,
        "venus_house": venus_house,
        "venus_aspects": aspects,
        "intercepted": intercepted,
        "venus_ruled_houses": ruled_houses,
        "interpretation": venus_interpretation(venus, venus_house, aspects, ruled_houses),
    }
