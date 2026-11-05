"""台風情報(気象庁)の取得と整形・進路図。

- 発表中の台風: /bosai/typhoon/data/targetTc.json
- 個別の台風:   /bosai/typhoon/data/<TC番号>/specifications.json
  [0] 共通部(名前・番号・発表時刻)、[1]〜 実況・推定・予報(position.deg, pressure, maximumWind,
  stormWarning, galeWarning, probabilityCircleRadius, validtime など)
"""
import math
from datetime import datetime, timedelta, timezone

import aiohttp

JMA = "https://www.jma.go.jp"
BASE = f"{JMA}/bosai/typhoon/data"
PAGE_URL = f"{JMA}/bosai/map.html#contents=typhoon"
JST = timezone(timedelta(hours=9))


def jp(v) -> str:
    """{"jp": "..."} などから日本語の文字列を取り出す。"""
    if isinstance(v, dict):
        for k in ("jp", "JP", "ja"):
            if k in v:
                return jp(v[k])
        for val in v.values():
            s = jp(val)
            if s:
                return s
        return ""
    return "" if v is None else str(v)


def find_num(v, keys=("km",)):
    """入れ子の中から、最初に見つかった数値(keys のいずれかの値)。"""
    if isinstance(v, dict):
        for k in keys:
            if k in v and isinstance(v[k], (int, float)):
                return float(v[k])
            if k in v and isinstance(v[k], str):
                try:
                    return float(v[k])
                except ValueError:
                    pass
        for val in v.values():
            n = find_num(val, keys)
            if n is not None:
                return n
    if isinstance(v, list):
        for val in v:
            n = find_num(val, keys)
            if n is not None:
                return n
    return None


def to_num(v, keys):
    """数値・数字の文字列・{"hPa": 990} のどれでも数値にする。"""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return find_num(v, keys)


def parse_time(v) -> datetime | None:
    s = v.get("UTC") if isinstance(v, dict) else v
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def parse(specs: list) -> dict:
    """specifications.json を扱いやすい形にする。"""
    head = specs[0] if specs and isinstance(specs[0], dict) else {}
    number = head.get("typhoonNumber")
    name = jp(head.get("name"))
    category = jp(head.get("category"))
    points = []
    for e in specs[1:]:
        if not isinstance(e, dict):
            continue
        pos = (e.get("position") or {}).get("deg")
        if not (isinstance(pos, list) and len(pos) >= 2):
            continue
        wind = e.get("maximumWind") or {}
        points.append({
            "part": jp(e.get("part")),
            "time": parse_time(e.get("validtime")),
            "lat": float(pos[0]), "lon": float(pos[1]),
            "location": jp(e.get("location")),
            "pressure": to_num(e.get("pressure"), ("hPa", "value")),
            "wind": find_num(wind.get("sustained"), ("m/s",)),
            "gust": find_num(wind.get("gust"), ("m/s",)),
            "course": jp(e.get("course")),
            "speed": find_num(e.get("speed"), ("km/h",)),
            "speed_note": jp((e.get("speed") or {}).get("note")) if isinstance(e.get("speed"), dict) else "",
            "storm_km": find_num(e.get("stormWarning")),
            "gale_km": find_num(e.get("galeWarning")),
            "circle_km": find_num(e.get("probabilityCircleRadius")),
            "category": jp(e.get("category")),
            "scale": jp(e.get("scale")),
            "intensity": jp(e.get("intensity")),
        })
    return {"number": number, "name": name, "category": category,
            "issue": parse_time(head.get("issue")), "points": points}


def label(tc_id: str, info: dict) -> str:
    num = info.get("number")
    if num and str(num).isdigit() and len(str(num)) > 2:
        title = f"台風第{int(str(num)[-2:])}号"
    elif num:
        title = f"台風第{num}号"
    else:
        title = "熱帯低気圧"
    if info.get("name"):
        title += f"({info['name']})"
    return f"{title} {tc_id}"


def fmt(v, spec="g", unit=""):
    return "—" if v is None else f"{v:{spec}}{unit}"


def point_line(p: dict) -> str:
    t = p["time"].astimezone(JST).strftime("%m/%d %H時") if p["time"] else ""
    move = p["course"]
    if p["speed"] is not None:
        move += f" {p['speed']:g}km/h"
    elif p["speed_note"]:
        move += f" {p['speed_note']}"
    parts = [f"**{p['part'] or '—'}** {t}", f"{p['lat']:.1f}N {p['lon']:.1f}E {p['location']}".strip(),
             f"{fmt(p['pressure'], 'g', 'hPa')}", f"最大風速{fmt(p['wind'], 'g', 'm/s')}"]
    if p["gust"] is not None:
        parts.append(f"最大瞬間{p['gust']:g}m/s")
    if move.strip():
        parts.append(move.strip())
    if p["storm_km"]:
        parts.append(f"暴風{'警戒' if p['circle_km'] else ''}域{p['storm_km']:g}km")
    if p["gale_km"]:
        parts.append(f"強風域{p['gale_km']:g}km")
    if p["circle_km"]:
        parts.append(f"予報円{p['circle_km']:g}km")
    return " / ".join(parts)


def summary_text(tc_id: str, info: dict) -> str:
    issue = info["issue"].astimezone(JST).strftime("%m/%d %H:%M") if info["issue"] else "不明"
    lines = [f"🌀 **{label(tc_id, info)}** 気象庁 {issue} 発表"]
    if info.get("category"):
        lines[0] += f"({info['category']})"
    lines += [point_line(p) for p in info["points"]]
    lines.append("図:赤=暴風域・黄=強風域(実況)/白い円=予報円・赤い円=暴風警戒域/黒線=これまでの経路")
    return "\n".join(lines)


# ---------- 進路図(地理院タイルの上に描く) ----------
def choose_area(points: list[tuple[float, float]], extra_deg: float = 4.0):
    from images import Area
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lon0, lon1 = min(lons) - extra_deg, max(lons) + extra_deg
    lat0, lat1 = max(min(lats) - extra_deg, -5), min(max(lats) + extra_deg, 60)
    if lon1 - lon0 < 16:
        c = (lon0 + lon1) / 2
        lon0, lon1 = c - 8, c + 8
    if lat1 - lat0 < 12:
        c = (lat0 + lat1) / 2
        lat0, lat1 = c - 6, c + 6
    span = lon1 - lon0
    z = max(3, min(7, int(math.floor(math.log2(1400 * 360 / (span * 256))))))
    return Area("進路", (lon0, lat0, lon1, lat1), z)


def km_to_px(km: float, lat: float, z: int) -> float:
    mpp = 2 * math.pi * 6378137 * math.cos(math.radians(lat)) / (256 * 2 ** z)
    return km * 1000 / mpp


async def track_map(composer, info: dict, history: list[dict], ens_tracks: list | None = None) -> bytes:
    """実況の経路(これまでの記録)・予報円・暴風域を描いた図(PNG)。"""
    import io
    from PIL import Image, ImageDraw, ImageFont
    from images import BASE_MAP, draw_grid, lonlat_to_px

    pts = [(p["lat"], p["lon"]) for p in info["points"]] + [(h["lat"], h["lon"]) for h in history]
    for tr in ens_tracks or []:
        pts += [(a, b) for a, b in tr]
    area = choose_area(pts)
    base = await composer.mosaic(BASE_MAP, area, static=True)
    img = Image.new("RGBA", base.size, (0, 0, 0, 0))   # 半透明で重ねるため、別の層に描いてから合成する
    draw = ImageDraw.Draw(img)
    z = area.zoom
    ox, oy = lonlat_to_px(area.bbox[0], area.bbox[3], z)

    def xy(lat, lon):
        x, y = lonlat_to_px(lon, lat, z)
        return x - ox, y - oy

    for tr in ens_tracks or []:                         # アンサンブルの各メンバー(細い線)
        if len(tr) > 1:
            draw.line([xy(a, b) for a, b in tr], fill=(40, 70, 220, 150), width=2)
    if len(history) > 1:                                # これまでの実況の経路
        draw.line([xy(h["lat"], h["lon"]) for h in history], fill=(30, 30, 30, 230), width=3)
    centers = []
    for p in info["points"]:
        x, y = xy(p["lat"], p["lon"])
        if p["circle_km"]:                              # 予報円(白)・暴風警戒域(赤)
            r = km_to_px(p["circle_km"], p["lat"], z)
            draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 255, 255, 255), width=2)
            draw.ellipse([x - r, y - r, x + r, y + r], outline=(60, 60, 60, 255), width=1)
            if p["storm_km"]:
                r2 = r + km_to_px(p["storm_km"], p["lat"], z)
                draw.ellipse([x - r2, y - r2, x + r2, y + r2], outline=(220, 30, 30, 230), width=2)
        else:                                           # 実況・推定:暴風域(赤)・強風域(黄)
            if p["gale_km"]:
                r = km_to_px(p["gale_km"], p["lat"], z)
                draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 220, 0, 60), outline=(230, 180, 0, 220))
            if p["storm_km"]:
                r = km_to_px(p["storm_km"], p["lat"], z)
                draw.ellipse([x - r, y - r, x + r, y + r], fill=(230, 30, 30, 80), outline=(200, 0, 0, 230))
        centers.append((x, y))
    if len(centers) > 1:
        draw.line(centers, fill=(40, 40, 40, 200), width=1)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()
    for p, (x, y) in zip(info["points"], centers):
        draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(200, 0, 0, 255) if not p["circle_km"] else (0, 0, 0, 255))
        if p["time"]:
            draw.text((x + 6, y - 7), p["time"].astimezone(JST).strftime("%d/%H"), fill=(0, 0, 0, 255), font=font)
    img = Image.alpha_composite(base.convert("RGBA"), img)
    draw_grid(img, area)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


class TyphoonSource:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def _json(self, url: str):
        async with self.session.get(url) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def active(self) -> list[str]:
        data = await self._json(f"{BASE}/targetTc.json")
        return [d["tropicalCyclone"] for d in data if isinstance(d, dict) and d.get("tropicalCyclone")]

    async def specifications(self, tc_id: str) -> list:
        return await self._json(f"{BASE}/{tc_id}/specifications.json")
