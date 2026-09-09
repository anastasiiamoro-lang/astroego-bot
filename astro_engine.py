
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import swisseph as swe
from timezonefinder import TimezoneFinder

SIGNS = ["Овен","Телец","Близнецы","Рак","Лев","Дева","Весы","Скорпион","Стрелец","Козерог","Водолей","Рыбы"]
PLANET_IDS = {
    "Солнце": swe.SUN, "Луна": swe.MOON, "Меркурий": swe.MERCURY, "Венера": swe.VENUS,
    "Марс": swe.MARS, "Юпитер": swe.JUPITER, "Сатурн": swe.SATURN,
    "Уран": swe.URANUS, "Нептун": swe.NEPTUNE, "Плутон": swe.PLUTO,
    "Северный узел": swe.TRUE_NODE,
}
OUTER = {"Уран","Нептун","Плутон"}
RULERS = {
    0:["Марс"],1:["Венера"],2:["Меркурий"],3:["Луна"],4:["Солнце"],5:["Меркурий"],
    6:["Венера"],7:["Плутон","Марс"],8:["Юпитер"],9:["Сатурн"],
    10:["Уран","Сатурн"],11:["Нептун","Юпитер"],
}
ASPECT_BY_SIGN_DIFF = {0:"соединение",2:"секстиль",3:"квадрат",4:"тригон",6:"оппозиция",8:"тригон",9:"квадрат",10:"секстиль"}
ANGLE = {"соединение":0,"секстиль":60,"квадрат":90,"тригон":120,"оппозиция":180}
TF = TimezoneFinder()

@dataclass
class Point:
    name: str
    longitude: float
    sign_index: int
    sign: str
    degree: float
    speed: float = 0.0

def norm(x): return x % 360.0
def point(name, lon, speed=0.0):
    lon=norm(lon); si=int(lon//30)
    return Point(name,lon,si,SIGNS[si],lon%30,speed)

def fmtdeg(x):
    d=int(x); m=int(round((x-d)*60))
    if m==60: d+=1; m=0
    return f"{d}°{m:02d}′"

def local_to_utc(date_s,time_s,lat,lon):
    tzname=TF.timezone_at(lat=lat,lng=lon)
    if not tzname: raise ValueError("Не удалось определить часовой пояс места.")
    try: naive=datetime.strptime(f"{date_s} {time_s}","%d.%m.%Y %H:%M")
    except ValueError: raise ValueError("Дата/время нужны в формате ДД.ММ.ГГГГ и ЧЧ:ММ.")
    local=naive.replace(tzinfo=ZoneInfo(tzname))
    return local.astimezone(timezone.utc),tzname

def jd(dt):
    h=dt.hour+dt.minute/60+dt.second/3600+dt.microsecond/3.6e9
    return swe.julday(dt.year,dt.month,dt.day,h,swe.GREG_CAL)

def planets_at(jdut):
    flags=swe.FLG_SWIEPH|swe.FLG_SPEED
    out={}
    for name,pid in PLANET_IDS.items():
        xx,_=swe.calc_ut(jdut,pid,flags)
        out[name]=point(name,xx[0],xx[3])
    # South node is always opposite true north node
    n=out["Северный узел"]
    out["Южный узел"]=point("Южный узел",n.longitude+180,-n.speed)
    return out

def houses_at(jdut,lat,lon):
    cusps,ascmc=swe.houses(jdut,lat,lon,b"P")
    return [norm(float(x)) for x in cusps[:12]],point("ASC",ascmc[0]),point("MC",ascmc[1])

def in_arc(x,a,b):
    x,a,b=norm(x),norm(a),norm(b)
    return (a<=x<b) if a<=b else (x>=a or x<b)

def house_of(lon,cusps):
    for i in range(12):
        if in_arc(lon,cusps[i],cusps[(i+1)%12]): return i+1
    raise RuntimeError("Не удалось определить дом.")

def angular(a,b):
    d=abs(norm(a)-norm(b)); return min(d,360-d)

def whole_sign_aspect(a,b):
    return ASPECT_BY_SIGN_DIFF.get((b.sign_index-a.sign_index)%12)

def orb_for(a,b,asp): return abs(angular(a.longitude,b.longitude)-ANGLE[asp])

def strength(other,orb):
    if orb<=1: return "dominant",0
    if other in OUTER and orb<=2: return "very_strong",1
    if orb<=3: return "strong",2
    if orb<=6: return "noticeable",3
    return "background",4

def aspects_for(name,ps):
    a=ps[name]; rows=[]
    for on,b in ps.items():
        if on==name: continue
        asp=whole_sign_aspect(a,b)
        if not asp: continue
        o=orb_for(a,b,asp); label,rank=strength(on,o)
        rows.append({"a":name,"b":on,"aspect":asp,"orb":o,"strength":label,"rank":rank})
    return sorted(rows,key=lambda x:(x["rank"],x["orb"]))

def cusp_sign(x): return int(norm(x)//30)

def rulers_of_house(h,cusps): return RULERS[cusp_sign(cusps[h-1])]

def houses_ruled_by(planet,cusps):
    return [h for h in range(1,13) if planet in rulers_of_house(h,cusps)]

def intercepted(cusps):
    used={cusp_sign(x) for x in cusps}; rows=[]
    for si in range(12):
        if si not in used:
            rows.append((SIGNS[si],house_of(si*30+15,cusps)))
    return rows

def dispositor(name,ps):
    return RULERS[ps[name].sign_index][0]

def chart(date_s,time_s,lat,lon):
    utc,tz=local_to_utc(date_s,time_s,lat,lon); j=jd(utc)
    ps=planets_at(j); cusps,asc,mc=houses_at(j,lat,lon)
    ph={n:house_of(p.longitude,cusps) for n,p in ps.items()}
    asp={n:aspects_for(n,ps) for n in ps}
    sun_disp=dispositor("Солнце",ps); asc_ruler=RULERS[asc.sign_index][0]
    return {
        "date":date_s,"time":time_s,"lat":lat,"lon":lon,"timezone":tz,"utc":utc,
        "planets":ps,"cusps":cusps,"asc":asc,"mc":mc,"houses":ph,"aspects":asp,
        "sun_dispositor":sun_disp,"asc_ruler":asc_ruler,
        "ruled":{n:houses_ruled_by(n,cusps) for n in ps},
        "intercepted":intercepted(cusps),
    }

def strongest(chart,focus,limit=4):
    seen=set(); rows=[]
    for n in focus:
        for a in chart["aspects"].get(n,[]):
            pair=tuple(sorted((a["a"],a["b"])))
            if pair in seen: continue
            seen.add(pair); rows.append(a)
    return sorted(rows,key=lambda x:(x["rank"],x["orb"]))[:limit]

def solar_return(natal,year,lat,lon):
    target=natal["planets"]["Солнце"].longitude
    # search around birthday, then binary-refine signed solar longitude difference
    birth=datetime.strptime(natal["date"],"%d.%m.%Y")
    guess=datetime(year,birth.month,birth.day,12,tzinfo=timezone.utc)
    def signed_diff(dt):
        sl=planets_at(jd(dt))["Солнце"].longitude
        return ((sl-target+180)%360)-180
    lo=guess
    # find bracket across 4 days
    vals=[]
    for k in range(-3*24,4*24+1,3):
        d=guess.replace() + __import__("datetime").timedelta(hours=k)
        vals.append((d,signed_diff(d)))
    bracket=None
    for (d1,v1),(d2,v2) in zip(vals,vals[1:]):
        if v1==0 or v1*v2<0:
            bracket=(d1,d2); break
    if not bracket: raise ValueError("Не удалось найти точный момент соляра.")
    lo,hi=bracket
    for _ in range(45):
        mid=lo+(hi-lo)/2
        if signed_diff(lo)*signed_diff(mid)<=0: hi=mid
        else: lo=mid
    moment=lo+(hi-lo)/2
    ps=planets_at(jd(moment)); cusps,asc,mc=houses_at(jd(moment),lat,lon)
    ph={n:house_of(p.longitude,cusps) for n,p in ps.items()}
    # solar planets in natal houses + solar-to-natal whole-sign aspects
    natal_house={n:house_of(p.longitude,natal["cusps"]) for n,p in ps.items()}
    cross=[]
    for sn,sp in ps.items():
        for nn,np in natal["planets"].items():
            asp=whole_sign_aspect(sp,np)
            if asp:
                o=orb_for(sp,np,asp); lab,r=strength(nn,o)
                cross.append({"solar":sn,"natal":nn,"aspect":asp,"orb":o,"strength":lab,"rank":r})
    cross.sort(key=lambda x:(x["rank"],x["orb"]))
    return {"moment":moment,"planets":ps,"cusps":cusps,"asc":asc,"mc":mc,"houses":ph,
            "solar_in_natal_houses":natal_house,"cross_aspects":cross}
