"""ひまわり9号の観測データ(気象庁のHSD形式)から、短波赤外(B07, 3.9μm)と差分画像を作る。

- 気象庁のホームページのタイルには B07 や差分画像が無いため、気象庁が作成した観測データそのもの
  (NOAA のオープンデータで公開されている Himawari Standard Data、全球・10分ごと)を使う。
- 日本付近(北緯24〜46度)は全球データの10分割のうち第2・第3セグメントに入るので、その部分だけ取る。
- 読み込み・輝度温度への変換は satpy(ahi_hsd)。メモリを多く使うので、別のプロセスで行う
  (`python himawari_hsd.py <入力フォルダ> <出力.npz> <西,南,東,北> <ズーム>`)。
"""
import asyncio
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

# ひまわり9号は2022年12月13日から運用(それより前は8号)
H9_START = datetime(2022, 12, 13, 5, 0, tzinfo=timezone.utc)


def bucket(t: datetime) -> tuple[str, str]:
    """観測時刻 t のデータがある場所 (URL, 衛星の記号)。"""
    if t >= H9_START:
        return "https://noaa-himawari9.s3.amazonaws.com", "H09"
    return "https://noaa-himawari8.s3.amazonaws.com", "H08"


SEGMENTS = (2, 3)
R = 6378137.0
TILE = 256


@dataclass(frozen=True)
class HsdImage:
    key: str
    title: str
    channel: str
    expr: str              # "B07" や "B07-B13"
    vmin: float
    vmax: float
    cmap: str
    center: float | None   # 差分画像は0を中心にした色
    label: str
    note: str


# 名前は気象庁の衛星画像の呼び方(IR=赤外1、I2=赤外2、I4=赤外4(3.9μm)、WV/W2/W3=水蒸気、S1・S2=差分)
HSD_IMAGES = [
    HsdImage("i4", "ひまわり 赤外4画像 I4(B07 3.9μm)", "赤外-i4", "B07", 200, 320, "Greys", None,
             "B07 輝度温度 (K)", "バンド7(3.9μm)の輝度温度。昼間は太陽光の反射分も含む"),
    HsdImage("w2", "ひまわり 水蒸気画像 W2(B09 6.9μm)", "水蒸気-w2", "B09", 205, 265, "Greys", None,
             "B09 輝度温度 (K)", "バンド9(6.9μm)の輝度温度。対流圏中層の水蒸気"),
    HsdImage("w3", "ひまわり 水蒸気画像 W3(B10 7.3μm)", "水蒸気-w3", "B10", 210, 275, "Greys", None,
             "B10 輝度温度 (K)", "バンド10(7.3μm)の輝度温度。対流圏中・下層の水蒸気"),
    HsdImage("s1", "ひまわり 赤外1・2差分画像 S1(B13−B15)", "差分-s1", "B13-B15", -3, 8, "RdBu_r", 0.0,
             "B13 − B15 (K)", "赤外1(10.4μm)と赤外2(12.4μm)の輝度温度の差(スプリットウィンドウ)"),
    HsdImage("s2", "ひまわり 3.9μm差分画像 S2(B13−B07)", "差分-s2", "B13-B07", -20, 10, "RdBu_r", 0.0,
             "B13 − B07 (K)", "赤外1(10.4μm)と赤外4(3.9μm)の輝度温度の差"),
]
ENABLED = {k.strip().lower() for k in os.getenv("HSD_IMAGES", "i4,w2,w3,s1,s2").split(",") if k.strip()}
MIN_INTERVAL = int(os.getenv("HSD_MIN_INTERVAL", "30"))   # 取得量が多いので、最短でもこの間隔(分)


def active_images() -> list[HsdImage]:
    return [i for i in HSD_IMAGES if i.key in ENABLED]


def needed_bands(images: list[HsdImage] | None = None) -> list[str]:
    need = set()
    for i in images if images is not None else active_images():
        need.update(i.expr.split("-"))
    return sorted(need)


def file_name(t: datetime, band: str, seg: int) -> str:
    return f"HS_{bucket(t)[1]}_{t:%Y%m%d_%H%M}_{band}_FLDK_R20_S{seg:02d}10.DAT.bz2"


def prefix(t: datetime) -> str:
    return f"AHI-L1b-FLDK/{t:%Y/%m/%d/%H%M}/"


def candidates(now: datetime, interval: int, back_minutes: int = 90) -> list[datetime]:
    """新しい順の観測時刻(interval 分おき)。"""
    t = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % 10)
    out = []
    while len(out) < 30 and (now - t) <= timedelta(minutes=back_minutes):
        if (t.hour * 60 + t.minute) % interval == 0:
            out.append(t)
        t -= timedelta(minutes=10)
    return out


async def list_keys(session, t: datetime) -> set[str]:
    url = f"{bucket(t)[0]}/?list-type=2&prefix={prefix(t)}"
    async with session.get(url) as r:
        if r.status != 200:
            return set()
        text = await r.text()
    return {k.rsplit("/", 1)[-1] for k in re.findall(r"<Key>([^<]+)</Key>", text)}


async def find_latest(session, now: datetime, interval: int) -> datetime | None:
    """必要なファイルがすべてそろっている、いちばん新しい観測時刻。"""
    bands = needed_bands()
    for t in candidates(now, interval):
        keys = await list_keys(session, t)
        if all(file_name(t, b, s) in keys for b in bands for s in SEGMENTS):
            return t
        await asyncio.sleep(0.5)
    return None


async def download(session, t: datetime, folder: Path, bands: list[str] | None = None) -> list[Path]:
    paths = []
    for b in bands or needed_bands():
        for s in SEGMENTS:
            name = file_name(t, b, s)
            async with session.get(f"{bucket(t)[0]}/{prefix(t)}{name}") as r:
                r.raise_for_status()
                (folder / name).write_bytes(await r.read())
            paths.append(folder / name)
    return paths


# ---------- 位置(Webメルカトル。気象庁のタイルと同じ図法にして、海岸線のタイルを重ねる) ----------
def lonlat_to_px(lon, lat, z):
    n = TILE * 2 ** z
    x = (lon + 180.0) / 360.0 * n
    r = math.radians(lat)
    y = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
    return x, y


def mercator_grid(bbox, z):
    """bbox を気象庁タイルの切り出しと同じ画素にしたときの (幅, 高さ, メートルの範囲)。"""
    lon0, lat0, lon1, lat1 = bbox
    x0, y0 = lonlat_to_px(lon0, lat1, z)
    x1, y1 = lonlat_to_px(lon1, lat0, z)
    left, top = x0 - math.floor(x0 / TILE) * TILE, y0 - math.floor(y0 / TILE) * TILE
    w = int(left + x1 - x0) - int(left)
    h = int(top + y1 - y0) - int(top)
    n = TILE * 2 ** z
    m = 2 * math.pi * R
    ext = ((x0 / n - 0.5) * m, (0.5 - y1 / n) * m, (x1 / n - 0.5) * m, (0.5 - y0 / n) * m)
    return w, h, ext


def compute(arrays: dict, expr: str) -> np.ndarray:
    parts = expr.split("-")
    out = arrays[parts[0]].astype("float32")
    for p in parts[1:]:
        out = out - arrays[p]
    return out


def colorize(data: np.ndarray, image: HsdImage) -> np.ndarray:
    """値を色にする(RGBA, 0〜255)。値の無い所は黒。"""
    from matplotlib import colormaps
    from matplotlib.colors import Normalize, TwoSlopeNorm
    cmap = colormaps[image.cmap]
    # Greys は値が小さいほど白になる(低温の雲が白く写る、ふつうの赤外画像と同じ見え方)
    norm = (TwoSlopeNorm(image.center, image.vmin, image.vmax) if image.center is not None
            else Normalize(image.vmin, image.vmax))
    rgba = cmap(norm(np.clip(data, image.vmin, image.vmax)))
    rgba[~np.isfinite(data)] = (0, 0, 0, 1)
    return (rgba * 255).astype("uint8")


def finish(img_rgba, image: HsdImage, t: datetime, font_path=None) -> bytes:
    """色を付けた画像に、凡例と出典を付けてJPEGにする。"""
    import matplotlib.pyplot as plt
    from matplotlib import colormaps
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize, TwoSlopeNorm
    from charts import use_font
    import io
    use_font(font_path)
    h, w = img_rgba.shape[:2]
    fig = plt.figure(figsize=(w / 100, h / 100 + 0.9), dpi=100)
    ax = fig.add_axes([0, 0.9 / (h / 100 + 0.9), 1, h / 100 / (h / 100 + 0.9)])
    ax.imshow(img_rgba)
    ax.set_axis_off()
    norm = (TwoSlopeNorm(image.center, image.vmin, image.vmax) if image.center is not None
            else Normalize(image.vmin, image.vmax))
    cax = fig.add_axes([0.05, 0.45 / (h / 100 + 0.9), 0.62, 0.18 / (h / 100 + 0.9)])
    cb = fig.colorbar(ScalarMappable(norm, colormaps[image.cmap]), cax=cax, orientation="horizontal")
    cb.set_label(image.label, fontsize=9)
    cb.ax.tick_params(labelsize=8)
    jst = t.astimezone(timezone(timedelta(hours=9)))
    fig.text(0.7, 0.55 / (h / 100 + 0.9), f"{jst:%Y-%m-%d %H:%M} JST ({t:%H:%M} UTC)\n"
             "出典:気象庁 ひまわり9号(NOAA Open Data)", fontsize=8, va="center")
    buf = io.BytesIO()
    fig.savefig(buf, format="jpg", dpi=100, pil_kwargs={"quality": 90})
    plt.close(fig)
    return buf.getvalue()


# ---------- 別プロセスで動かす部分(satpy で読み、メルカトル図法の格子にする) ----------
def resample_files(files: list[str], out: str, bbox, zoom: int) -> None:
    from pyresample.geometry import AreaDefinition
    from satpy import Scene
    w, h, ext = mercator_grid(bbox, zoom)
    area = AreaDefinition("wx", "wx", "wx", "EPSG:3857", w, h, ext)
    scn = Scene(filenames=files, reader="ahi_hsd")
    bands = sorted({f.split("_")[4] for f in map(lambda p: Path(p).name, files)})
    scn.load(bands)                                   # 赤外のバンドは輝度温度(K)で読まれる
    res = scn.resample(area, resampler="nearest", radius_of_influence=6000)
    np.savez_compressed(out, **{b: res[b].values.astype("float32") for b in bands})


async def build(session, t: datetime, bbox, zoom: int, bands: list[str] | None = None) -> dict[str, np.ndarray]:
    """観測時刻 t のデータを取り、メルカトル図法の格子にしたバンドの値(K)を返す。"""
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d)
        await download(session, t, folder, bands)
        out = folder / "out.npz"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve()), str(folder), str(out),
            ",".join(map(str, bbox)), str(zoom),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError("ひまわりのデータの変換に失敗: " + err.decode(errors="replace")[-400:])
        with np.load(out) as z:
            return {k: z[k] for k in z.files}


if __name__ == "__main__":
    folder, out, bbox, zoom = sys.argv[1:5]
    files = sorted(str(p) for p in Path(folder).glob("*.DAT.bz2"))
    resample_files(files, out, tuple(float(v) for v in bbox.split(",")), int(zoom))
