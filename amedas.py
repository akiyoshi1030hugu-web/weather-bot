"""アメダスの観測値(気象庁)を一覧表の画像にする。

- 値は気象庁アメダスの速報値。品質フラグが0(正常)の値だけを使い、それ以外は「―」にする。
- 3時間気圧変化は、同じ地点の3時間前の海面気圧との差。
- 表示用フォントは BIZ UDゴシック(SIL Open Font License)。初回だけダウンロードして保存する。
  取得できない場合は文字の表で投稿する。
"""
import io
import math
import os
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont

JMA = "https://www.jma.go.jp"
JST = timezone(timedelta(hours=9))

AMEDAS_HOURS = {int(h) for h in os.getenv("AMEDAS_HOURS", "3,9,15,21").split(",")}
AMEDAS_STATIONS = [n.strip() for n in os.getenv(
    "AMEDAS_STATIONS",
    "福岡,佐賀,長崎,熊本,大分,宮崎,鹿児島,下関,広島,松江,松山,高知,室戸岬,徳島,高松",
).split(",") if n.strip()]

FONT_DIR = Path(os.getenv("DATA_DIR", "./data")) / "fonts"
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/bizudgothic/{name}"
FONT_FILES = {"regular": "BIZUDGothic-Regular.ttf", "bold": "BIZUDGothic-Bold.ttf"}

WIND_DIRS = ["静穏", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東", "南",
             "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西", "北"]


@dataclass
class Row:
    name: str
    temp: float | None
    humidity: float | None
    pressure: float | None
    tendency: float | None
    wind_dir: int | None
    wind: float | None
    rain: float | None


def value(obs: dict, key: str) -> float | None:
    v = obs.get(key)
    if not v or v[0] is None or v[1] != 0:
        return None
    return v[0]


def fmt(v: float | None, spec: str) -> str:
    return "―" if v is None else format(v, spec)


# ---------- 文字の表(フォントが使えないときの予備) ----------
def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int, right: bool = False) -> str:
    space = " " * max(0, width - display_width(text))
    return space + text if right else text + space


def text_table(rows: list[Row]) -> str:
    lines = [pad("地点", 8) + pad("気温", 6, True) + pad("湿度", 5, True) + pad("気圧", 8, True)
             + pad("3h", 6, True) + "  " + pad("風", 11) + pad("1h雨", 6, True)]
    for r in rows:
        wind = "―" if r.wind_dir is None else f"{WIND_DIRS[r.wind_dir]}{fmt(r.wind, '.1f')}"
        lines.append(pad(r.name, 8) + pad(fmt(r.temp, ".1f"), 6, True) + pad(fmt(r.humidity, ".0f"), 5, True)
                     + pad(fmt(r.pressure, ".1f"), 8, True) + pad(fmt(r.tendency, "+.1f"), 6, True)
                     + "  " + pad(wind, 11) + pad(fmt(r.rain, ".1f"), 6, True))
    return "\n".join(lines)


# ---------- 画像の表 ----------
def lerp_color(v: float, stops: list[tuple[float, tuple[int, int, int]]]) -> tuple[int, int, int]:
    if v <= stops[0][0]:
        return stops[0][1]
    for (v0, c0), (v1, c1) in zip(stops, stops[1:]):
        if v <= v1:
            k = (v - v0) / (v1 - v0)
            return tuple(round(a + (b - a) * k) for a, b in zip(c0, c1))
    return stops[-1][1]


TEMP_STOPS = [(-5, (120, 170, 245)), (10, (200, 228, 250)), (20, (255, 255, 255)),
              (28, (255, 214, 170)), (35, (245, 110, 100))]
# 気象庁の雨量の配色に近い色(下限mm, 背景色, 文字色)
RAIN_COLORS = [(80, (180, 0, 104), "white"), (50, (255, 40, 0), "white"), (30, (255, 153, 0), "black"),
               (20, (250, 245, 0), "black"), (10, (0, 65, 255), "white"), (5, (33, 140, 255), "white"),
               (1, (160, 210, 255), "black")]


def render_table(rows: list[Row], t: datetime, fonts: dict[str, Path]) -> bytes:
    font = ImageFont.truetype(str(fonts["regular"]), 22)
    bold = ImageFont.truetype(str(fonts["bold"]), 22)
    small = ImageFont.truetype(str(fonts["regular"]), 16)
    title_font = ImageFont.truetype(str(fonts["bold"]), 26)

    cols = [("地点", 130), ("気温℃", 105), ("湿度%", 90), ("海面気圧", 125),
            ("3h変化", 110), ("風向・風速 m/s", 220), ("1h雨mm", 105)]
    margin, row_h, head_h, title_h, foot_h = 16, 46, 42, 56, 60
    width = margin * 2 + sum(w for _, w in cols)
    height = title_h + head_h + row_h * len(rows) + foot_h
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)

    utc = t.astimezone(timezone.utc)
    d.text((margin, 14), f"アメダス観測値  {t:%m/%d %H:%M} JST({utc:%H}UTC)", fill=(26, 32, 44), font=title_font)

    y = title_h
    d.rectangle([margin, y, width - margin, y + head_h], fill=(45, 55, 72))
    x = margin
    for label, w in cols:
        d.text((x + w / 2, y + head_h / 2), label, fill="white", font=bold, anchor="mm")
        x += w
    y += head_h

    for i, r in enumerate(rows):
        top, mid = y, y + row_h / 2
        if i % 2:
            d.rectangle([margin, top, width - margin, top + row_h], fill=(247, 250, 252))
        x = margin
        cells = []
        for _, w in cols:
            cells.append((x, w))
            x += w

        # 地点
        d.text((cells[0][0] + 12, mid), r.name, fill=(26, 32, 44), font=bold, anchor="lm")
        # 気温(値に応じて背景色)
        cx, cw = cells[1]
        if r.temp is not None:
            d.rectangle([cx + 4, top + 5, cx + cw - 4, top + row_h - 5], fill=lerp_color(r.temp, TEMP_STOPS))
        d.text((cx + cw - 14, mid), fmt(r.temp, ".1f"), fill=(26, 32, 44), font=font, anchor="rm")
        # 湿度
        cx, cw = cells[2]
        d.text((cx + cw - 14, mid), fmt(r.humidity, ".0f"), fill=(26, 32, 44), font=font, anchor="rm")
        # 海面気圧
        cx, cw = cells[3]
        d.text((cx + cw - 14, mid), fmt(r.pressure, ".1f"), fill=(26, 32, 44), font=font, anchor="rm")
        # 3時間気圧変化(1hPa以上の変化を強調)
        cx, cw = cells[4]
        if r.tendency is None:
            text, color, f = "―", (26, 32, 44), font
        elif r.tendency <= -1.0:
            text, color, f = f"▼{abs(r.tendency):.1f}", (220, 38, 38), bold
        elif r.tendency >= 1.0:
            text, color, f = f"▲{r.tendency:.1f}", (37, 99, 235), bold
        else:
            text, color, f = f"{r.tendency:+.1f}", (113, 128, 150), font
        d.text((cx + cw - 14, mid), text, fill=color, font=f, anchor="rm")
        # 風(矢印は風が吹いていく向き)
        cx, cw = cells[5]
        if r.wind_dir is None:
            d.text((cx + cw / 2, mid), "―", fill=(26, 32, 44), font=font, anchor="mm")
        else:
            ax, ay = cx + 26, mid
            if r.wind_dir > 0:
                to = math.radians(r.wind_dir * 22.5 + 180)  # 北を0度、時計回り
                dx, dy = math.sin(to), -math.cos(to)
                tip = (ax + dx * 15, ay + dy * 15)
                tail = (ax - dx * 15, ay - dy * 15)
                d.line([tail, tip], fill=(45, 55, 72), width=3)
                left = (tip[0] - dx * 8 - dy * 6, tip[1] - dy * 8 + dx * 6)
                right = (tip[0] - dx * 8 + dy * 6, tip[1] - dy * 8 - dx * 6)
                d.polygon([tip, left, right], fill=(45, 55, 72))
            else:
                d.ellipse([ax - 7, ay - 7, ax + 7, ay + 7], outline=(45, 55, 72), width=2)
            w = r.wind
            color = (220, 38, 38) if w is not None and w >= 15 else (221, 107, 32) if w is not None and w >= 10 else (26, 32, 44)
            d.text((cx + 50, mid), f"{WIND_DIRS[r.wind_dir]} {fmt(w, '.1f')}", fill=color,
                   font=bold if color != (26, 32, 44) else font, anchor="lm")
        # 1時間降水量(気象庁の配色)
        cx, cw = cells[6]
        text_color = (26, 32, 44)
        if r.rain is not None:
            for low, bg, fg in RAIN_COLORS:
                if r.rain >= low:
                    d.rectangle([cx + 4, top + 5, cx + cw - 4, top + row_h - 5], fill=bg)
                    text_color = fg
                    break
        d.text((cx + cw - 14, mid), fmt(r.rain, ".1f"), fill=text_color, font=font, anchor="rm")
        d.line([margin, top + row_h, width - margin, top + row_h], fill=(226, 232, 240), width=1)
        y += row_h

    d.text((margin, y + 12), "出典:気象庁 アメダス(速報値)   「―」は欠測・未観測・品質確認中", fill=(113, 128, 150), font=small)
    d.text((margin, y + 34), "3h変化:3時間前の海面気圧との差(▼1hPa以上の下降 ▲1hPa以上の上昇)  矢印:風が吹いていく向き",
           fill=(113, 128, 150), font=small)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def summary(rows: list[Row]) -> str:
    """表の要点(最高・最低気温、最大の気圧下降、最大の1時間雨量)。"""
    parts = []
    temps = [r for r in rows if r.temp is not None]
    if temps:
        hi = max(temps, key=lambda r: r.temp)
        lo = min(temps, key=lambda r: r.temp)
        parts.append(f"🌡️ 最高 {hi.name} {hi.temp:.1f}℃/最低 {lo.name} {lo.temp:.1f}℃")
    falls = [r for r in rows if r.tendency is not None]
    if falls:
        f = min(falls, key=lambda r: r.tendency)
        if f.tendency <= -1.0:
            parts.append(f"📉 気圧下降 最大 {f.name} {f.tendency:+.1f}hPa/3h")
    rains = [r for r in rows if r.rain]
    if rains:
        m = max(rains, key=lambda r: r.rain)
        parts.append(f"☔ 1時間雨量 最大 {m.name} {m.rain:.1f}mm")
    winds = [r for r in rows if r.wind is not None]
    if winds:
        w = max(winds, key=lambda r: r.wind)
        if w.wind >= 10:
            parts.append(f"💨 風速 最大 {w.name} {w.wind:.1f}m/s")
    return "\n".join(parts)


class AmedasTable:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.names: dict[str, list[str]] | None = None
        self.fonts: dict[str, Path] | None = None

    async def _json(self, url: str):
        async with self.session.get(url) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def latest_time(self) -> datetime:
        async with self.session.get(f"{JMA}/bosai/amedas/data/latest_time.txt") as r:
            r.raise_for_status()
            return datetime.fromisoformat((await r.text()).strip()).astimezone(JST)

    async def observation(self, t: datetime) -> dict:
        return await self._json(f"{JMA}/bosai/amedas/data/map/{t:%Y%m%d%H%M%S}.json")

    async def _load_names(self) -> None:
        table = await self._json(f"{JMA}/bosai/amedas/const/amedastable.json")
        names: dict[str, list[str]] = {}
        for sid, s in table.items():
            names.setdefault(s.get("kjName", ""), []).append(sid)
        self.names = names

    def _resolve(self, name: str, obs: dict) -> str | None:
        """同名の地点が複数ある場合は、気圧を観測している地点(気象台など)を優先する。"""
        ids = self.names.get(name, [])
        with_pressure = [i for i in ids if "normalPressure" in obs.get(i, {})]
        return (with_pressure or ids or [None])[0]

    async def ensure_fonts(self) -> dict[str, Path] | None:
        if self.fonts is not None:
            return self.fonts
        FONT_DIR.mkdir(parents=True, exist_ok=True)
        paths = {}
        for key, name in FONT_FILES.items():
            path = FONT_DIR / name
            if not path.exists():
                try:
                    async with self.session.get(FONT_URL.format(name=name)) as r:
                        r.raise_for_status()
                        path.write_bytes(await r.read())
                except aiohttp.ClientError:
                    return None
            paths[key] = path
        self.fonts = paths
        return paths

    async def rows(self, t: datetime) -> tuple[list[Row], list[str]]:
        if self.names is None:
            await self._load_names()
        obs = await self.observation(t)
        try:
            before = await self.observation(t - timedelta(hours=3))
        except aiohttp.ClientError:
            before = {}
        rows, missing = [], []
        for name in AMEDAS_STATIONS:
            sid = self._resolve(name, obs)
            if sid is None:
                missing.append(name)
                continue
            o = obs.get(sid, {})
            p = value(o, "normalPressure")
            p0 = value(before.get(sid, {}), "normalPressure")
            wd = value(o, "windDirection")
            rows.append(Row(name, value(o, "temp"), value(o, "humidity"), p,
                            None if p is None or p0 is None else round(p - p0, 1),
                            None if wd is None else int(wd), value(o, "wind"),
                            value(o, "precipitation1h")))
        return rows, missing

    async def build(self, t: datetime) -> tuple[bytes | None, str, list[str]]:
        """(表の画像 または None, 要点または文字の表, 見つからなかった地点) を返す。"""
        rows, missing = await self.rows(t)
        fonts = await self.ensure_fonts()
        if fonts:
            return render_table(rows, t, fonts), summary(rows), missing
        return None, f"```\n{text_table(rows)}\n```\n{summary(rows)}", missing

    @staticmethod
    def latest_synoptic(latest: datetime) -> datetime | None:
        t = latest.replace(minute=0, second=0, microsecond=0)
        for _ in range(24):
            if t.hour in AMEDAS_HOURS:
                return t
            t -= timedelta(hours=1)
        return None
