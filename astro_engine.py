from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
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

# Первый управитель — основной в текущем MVP.
# Для Скорпиона, Водолея и Рыб сохраняем традиционного соуправителя вторым.
RULERS = {
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
        if planet_name in RULERS[sign_index]:
            ruled.append((house, SIGNS[sign_index]))
    return ruled


def main_ruler_for_sign(sign_index):
    return RULERS[sign_index][0]


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
        f"☉ Солнце — {sun.sign} {format_degree(sun.degree)}, {sun_house} дом.",
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
        f"Управитель Солнца — {sun_ruler_name}.",
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
        f"☽ Луна — {moon.sign} {format_degree(moon.degree)}, {moon_house} дом.",
        f"Эмоционально: {MOON_SIGN_TEXT[moon.sign]}. "
        f"Главная зона, где это особенно заметно, — {HOUSE_TEXT[moon_house]}.",
    ]

    if moon_aspects:
        lines.append(
            "Что сильнее всего влияет на Луну: "
            + short_aspects_text("Луна", moon_aspects)
            + "."
        )

    lines += [
        "",
        f"ASC — {asc.sign} {format_degree(asc.degree)}.",
        f"То, как ты входишь в мир: {ASC_TEXT[asc.sign]}.",
        f"Управитель ASC — {asc_ruler_name}: {asc_ruler.sign}, {asc_ruler_house} дом.",
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
            f"• {row['a']} — {row['aspect']} — {row['b']} ({row['strength']})."
        )

    lines += [
        "",
        "Собираем тебя:",
        (
            f"В твоей карте одновременно работают три слоя: "
            f"Солнце в {sun.sign} задаёт центральный вектор личности, "
            f"Луна в {moon.sign} показывает эмоциональную природу, "
            f"а ASC в {asc.sign} — способ проявляться и взаимодействовать с миром. "
            f"Диспозитор Солнца {sun_ruler_name} в {sun_ruler_house} доме и "
            f"управитель ASC {asc_ruler_name} в {asc_ruler_house} доме показывают, "
            f"куда эта личность фактически направляет энергию. "
            f"Ключевые аспекты выше — те напряжения и ресурсы, которые сильнее всего "
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
