"""エマグラムの指数(全国の高層観測地点)を1枚の表画像にまとめる。"""
import io
import re
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# (見出し, 指数名, 危険側の判定) 判定は値を受け取り (背景色, 太字か) を返す
ORANGE, RED, BLUE, LIGHT = (254, 215, 170), (254, 178, 178), (190, 227, 248), (254, 243, 199)


def num(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"-?\d+(\.\d+)?", text.replace("+", ""))
    return float(m.group()) if m else None


def ssi_color(v):   # SSI:3以下で雷雨の可能性、0以下で活発、-3以下で激しい
    return RED if v <= -3 else ORANGE if v <= 0 else LIGHT if v <= 3 else None


def k_color(v):     # K指数:30以上で雷雨の可能性が高い
    return RED if v >= 35 else ORANGE if v >= 30 else None


def cape_color(v):  # CAPE:1000以上でやや大きい、2500以上で大きい
    return RED if v >= 2500 else ORANGE if v >= 1000 else None


def pw_color(v):    # 可降水量:50mm以上は大雨の目安
    return BLUE if v >= 50 else None


def wet_color(v):   # 850hPa湿数:3℃以下は湿潤
    return BLUE if v <= 3 else None


COLUMNS = [("SSI", "SSI", ssi_color), ("K指数", "K指数", k_color), ("CAPE", "CAPE", cape_color),
           ("可降水量", "可降水量", pw_color), ("850湿数", "850hPa湿数", wet_color), ("LCL", "LCL", None)]


def render_summary(t: datetime, rows: list[tuple[str, str, dict]], fonts: dict[str, Path]) -> bytes:
    """rows: (地域, 地点名, 指数の辞書)。値のない地点は「―」になる。"""
    font = ImageFont.truetype(str(fonts["regular"]), 21)
    bold = ImageFont.truetype(str(fonts["bold"]), 21)
    small = ImageFont.truetype(str(fonts["regular"]), 15)
    title = ImageFont.truetype(str(fonts["bold"]), 25)

    widths = [120, 120] + [115] * len(COLUMNS)
    margin, row_h, head_h, title_h, foot_h = 16, 40, 40, 54, 92
    width = margin * 2 + sum(widths)
    height = title_h + head_h + row_h * len(rows) + foot_h
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    utc = t.astimezone(timezone.utc)
    d.text((margin, 13), f"全国の大気の安定度  {t:%m/%d %H時} JST({utc:%H}UTC)観測",
           fill=(26, 32, 44), font=title)

    y = title_h
    d.rectangle([margin, y, width - margin, y + head_h], fill=(45, 55, 72))
    x = margin
    for label, w in zip(["地域", "地点"] + [c[0] for c in COLUMNS], widths):
        d.text((x + w / 2, y + head_h / 2), label, fill="white", font=bold, anchor="mm")
        x += w
    y += head_h

    prev_region = None
    for i, (region, name, idx) in enumerate(rows):
        top, mid = y, y + row_h / 2
        if i % 2:
            d.rectangle([margin, top, width - margin, top + row_h], fill=(247, 250, 252))
        x = margin
        if region != prev_region:
            d.text((x + 10, mid), region, fill=(74, 85, 104), font=font, anchor="lm")
            if prev_region is not None:
                d.line([margin, top, width - margin, top], fill=(160, 174, 192), width=2)
        prev_region = region
        x += widths[0]
        d.text((x + 10, mid), name, fill=(26, 32, 44), font=bold, anchor="lm")
        x += widths[1]
        for (_, key, color_of), w in zip(COLUMNS, widths[2:]):
            text = idx.get(key) or "―"
            v = num(idx.get(key))
            bg = color_of(v) if (color_of and v is not None) else None
            if bg:
                d.rectangle([x + 4, top + 4, x + w - 4, top + row_h - 4], fill=bg)
            d.text((x + w - 10, mid), text.replace(" J/kg", "").replace(" mm", "").replace(" hPa", ""),
                   fill=(26, 32, 44), font=bold if bg in (RED, ORANGE) else font, anchor="rm")
            x += w
        y += row_h

    d.text((margin, y + 10), "出典:気象庁 高層気象観測(指定気圧面の値から計算した概算)  ―:未公開・欠測  "
           "単位 CAPE:J/kg 可降水量:mm 850湿数:℃ LCL:hPa", fill=(113, 128, 150), font=small)
    d.text((margin, y + 32), "色の目安  SSI:3以下 黄/0以下 橙/-3以下 赤   K指数:30以上 橙/35以上 赤   "
           "CAPE:1000以上 橙/2500以上 赤", fill=(113, 128, 150), font=small)
    d.text((margin, y + 54), "          可降水量:50mm以上 青   850hPa湿数:3℃以下 青(湿潤)",
           fill=(113, 128, 150), font=small)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
