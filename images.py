"""気象庁の地図タイル(ひまわり・レーダー・解析雨量)を取得して1枚の画像に合成する。

- 最新時刻は必ず気象庁の時刻一覧(targetTimes*.json)から取得する。
  時刻を推測してURLを作ると、存在しない画像へのアクセスで気象庁に負荷をかけるため。
- 背景地図は気象庁サイトが配信している地理院タイル(淡色地図)を使う。
"""
import asyncio
import io
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
from PIL import Image, ImageDraw, ImageFont

JMA = "https://www.jma.go.jp"
TILE = 256

BASE_MAP = f"{JMA}/tile/gsi/pale/{{z}}/{{x}}/{{y}}.png"                      # 地理院タイル(淡色)
MASK = f"{JMA}/bosai/jmatile/data/map/none/none/none/surf/mask/{{z}}/{{x}}/{{y}}.png"  # 海岸線など


@dataclass(frozen=True)
class Area:
    name: str
    bbox: tuple[float, float, float, float]  # 西経度, 南緯度, 東経度, 北緯度
    zoom: int


@dataclass(frozen=True)
class ImageProduct:
    key: str
    title: str
    channel: str
    times_url: str
    tile_url: str            # {bt} {vt} {member} {z} {x} {y} を含む
    areas: tuple[Area, ...]
    kind: str                # "satellite" または "rain"
    element: str | None = None  # 時刻一覧で絞り込む要素名
    page_url: str = ""
    note: str = ""


EAST_ASIA = Area("東アジア", (110.0, 15.0, 160.0, 55.0), 5)
JAPAN = Area("日本全域", (122.0, 24.0, 150.0, 46.0), 6)
WEST_JAPAN = Area("西日本(九州・四国・中国)", (128.5, 30.0, 135.5, 35.8), 8)

SAT_TIMES = f"{JMA}/bosai/himawari/data/satimg/targetTimes_fd.json"
SAT_TILE = f"{JMA}/bosai/himawari/data/satimg/{{bt}}/fd/{{vt}}/{{band}}/{{z}}/{{x}}/{{y}}.jpg"

IMAGE_PRODUCTS = [
    ImageProduct(
        key="himawari_ir", title="ひまわり 赤外画像(B13)", channel="ひまわり-赤外",
        times_url=SAT_TIMES, tile_url=SAT_TILE.replace("{band}", "B13/TBB"),
        areas=(EAST_ASIA,), kind="satellite",
        page_url=f"{JMA}/bosai/map.html#contents=himawari",
        note="雲頂温度が低い(背が高い)雲ほど白く写る"),
    ImageProduct(
        key="himawari_wv", title="ひまわり 水蒸気画像(B08)", channel="ひまわり-水蒸気",
        times_url=SAT_TIMES, tile_url=SAT_TILE.replace("{band}", "B08/TBB"),
        areas=(EAST_ASIA,), kind="satellite",
        page_url=f"{JMA}/bosai/map.html#contents=himawari",
        note="暗域は上・中層が乾燥した領域。トラフや乾燥貫入の把握に"),
    ImageProduct(
        key="radar", title="気象レーダー(高解像度降水ナウキャスト 実況)", channel="気象レーダー",
        times_url=f"{JMA}/bosai/jmatile/data/nowc/targetTimes_N1.json",
        tile_url=f"{JMA}/bosai/jmatile/data/nowc/{{bt}}/none/{{vt}}/surf/hrpns/{{z}}/{{x}}/{{y}}.png",
        areas=(JAPAN, WEST_JAPAN), kind="rain", element="hrpns",
        page_url=f"{JMA}/bosai/nowc/",
        note="5分ごとの降水強度の実況。凡例は気象庁「雨雲の動き」と同じ"),
    ImageProduct(
        key="rasrf", title="解析雨量(1時間降水量)", channel="解析雨量",
        times_url=f"{JMA}/bosai/jmatile/data/rasrf/targetTimes.json",
        tile_url=f"{JMA}/bosai/jmatile/data/rasrf/{{bt}}/{{member}}/{{vt}}/surf/rasrf/{{z}}/{{x}}/{{y}}.png",
        areas=(JAPAN, WEST_JAPAN), kind="rain", element="rasrf",
        page_url=f"{JMA}/bosai/kaikotan/",
        note="レーダーを雨量計で補正した1時間雨量。凡例は気象庁「今後の雨」と同じ"),
]


# ---------- 座標計算(Webメルカトル) ----------
def lonlat_to_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = TILE * 2 ** z
    x = (lon + 180.0) / 360.0 * n
    r = math.radians(lat)
    y = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
    return x, y


def parse_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def pick_latest(entries: list[dict], element: str | None, interval_min: int) -> dict | None:
    """実況(basetime == validtime)のうち、interval_min 分おきの最新時刻を選ぶ。"""
    best = None
    for e in entries:
        bt, vt = e.get("basetime"), e.get("validtime")
        if not bt or bt != vt:
            continue
        if element and "elements" in e and element not in e["elements"]:
            continue
        t = parse_utc(vt)
        if (t.hour * 60 + t.minute) % interval_min:
            continue
        if best is None or vt > best["validtime"]:
            best = e
    return best


# ---------- タイル取得と合成 ----------
class TileComposer:
    def __init__(self, session: aiohttp.ClientSession, concurrency: int = 4) -> None:
        self.session = session
        self.sem = asyncio.Semaphore(concurrency)
        self.static_cache: dict[str, bytes] = {}  # 背景地図・海岸線は変わらないので保持

    async def _get(self, url: str, static: bool) -> Image.Image | None:
        data = self.static_cache.get(url) if static else None
        if data is None:
            async with self.sem:
                async with self.session.get(url) as r:
                    if r.status != 200:
                        return None
                    data = await r.read()
            if static:
                self.static_cache[url] = data
        return Image.open(io.BytesIO(data)).convert("RGBA")

    async def mosaic(self, template: str, area: Area, static: bool = False, **fmt) -> Image.Image:
        lon0, lat0, lon1, lat1 = area.bbox
        z = area.zoom
        x0, y0 = lonlat_to_px(lon0, lat1, z)
        x1, y1 = lonlat_to_px(lon1, lat0, z)
        tx0, ty0, tx1, ty1 = int(x0 // TILE), int(y0 // TILE), int(x1 // TILE), int(y1 // TILE)
        coords = [(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)]
        tiles = await asyncio.gather(*(
            self._get(template.format(z=z, x=tx, y=ty, **fmt), static) for tx, ty in coords))
        canvas = Image.new("RGBA", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), (0, 0, 0, 0))
        for (tx, ty), tile in zip(coords, tiles):
            if tile is not None:
                canvas.paste(tile, ((tx - tx0) * TILE, (ty - ty0) * TILE))
        left, top = x0 - tx0 * TILE, y0 - ty0 * TILE
        return canvas.crop((int(left), int(top), int(left + x1 - x0), int(top + y1 - y0)))

    async def render(self, product: ImageProduct, entry: dict, area: Area) -> tuple[bytes, str]:
        fmt = {"bt": entry["basetime"], "vt": entry["validtime"],
               "member": entry.get("member", "none")}
        if product.kind == "satellite":
            img = await self.mosaic(product.tile_url, area, **fmt)
            mask = await self.mosaic(MASK, area, static=True)
            img = Image.alpha_composite(img, mask)
        else:
            base = await self.mosaic(BASE_MAP, area, static=True)
            rain = await self.mosaic(product.tile_url, area, **fmt)
            img = Image.alpha_composite(base, rain)
        draw_grid(img, area, light=product.kind == "satellite")
        return await asyncio.to_thread(encode, img, product.kind)


RAIN_JPEG_QUALITY = int(os.getenv("RAIN_JPEG_QUALITY", "90"))


def encode(img: Image.Image, kind: str) -> tuple[bytes, str]:
    """画像を保存して (データ, 拡張子) を返す。
    衛星画像はJPEG。雨の図はPNGとJPEGの両方を作り、小さいほうを使う
    (色がべったりした図はPNG、背景地図が細かい図はJPEGが小さくなりやすいため)。"""
    rgb = img.convert("RGB")
    jpg = io.BytesIO()
    if kind == "satellite":
        rgb.save(jpg, "JPEG", quality=92)
        return jpg.getvalue(), "jpg"
    rgb.save(jpg, "JPEG", quality=RAIN_JPEG_QUALITY, subsampling=0, optimize=True)
    png = io.BytesIO()
    rgb.save(png, "PNG", optimize=True)
    if len(jpg.getvalue()) < len(png.getvalue()):
        return jpg.getvalue(), "jpg"
    return png.getvalue(), "png"


def draw_grid(img: Image.Image, area: Area, light: bool = False) -> None:
    """経緯線(5度ごと)と数値を描く。位置の目安用。"""
    lon0, lat0, lon1, lat1 = area.bbox
    z = area.zoom
    step = 5 if lon1 - lon0 > 10 else 1
    ox, oy = lonlat_to_px(lon0, lat1, z)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()
    color = (255, 255, 120, 180) if light else (90, 90, 90, 200)
    lon = math.ceil(lon0 / step) * step
    while lon <= lon1:
        x = lonlat_to_px(lon, 0, z)[0] - ox
        draw.line([(x, 0), (x, img.height)], fill=color, width=1)
        draw.text((x + 3, 3), f"{lon:g}E", fill=color, font=font)
        lon += step
    lat = math.ceil(lat0 / step) * step
    while lat <= lat1:
        y = lonlat_to_px(0, lat, z)[1] - oy
        draw.line([(0, y), (img.width, y)], fill=color, width=1)
        draw.text((3, y + 3), f"{lat:g}N", fill=color, font=font)
        lat += step
