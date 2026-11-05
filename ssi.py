"""SSI(ショワルター安定指数)の分布図。

気象庁のメソモデル(MSM)の気圧面の数値予報データ(京都大学生存圏研究所のアーカイブで公開)から、
  SSI = T(500hPa) − (850hPaの空気塊を500hPaまで持ち上げたときの温度)
を格子ごとに計算する。空気塊は持ち上げ凝結高度までは乾燥断熱、その上は湿潤断熱(偽断熱)で上げる。
SSI が小さい(負)ほど大気が不安定。

データの場所(UTC の日付ごとのフォルダ)
  http://database.rish.kyoto-u.ac.jp/arch/jmadata/data/gpv/original/YYYY/MM/DD/
  Z__C_RJTD_<初期時刻14桁>_MSM_GPV_Rjp_L-pall_FH00-15_grib2.bin など
"""
import asyncio
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

RISH = os.getenv("RISH_BASE", "http://database.rish.kyoto-u.ac.jp/arch/jmadata/data/gpv/original")
FILE_RE = re.compile(r"Z__C_RJTD_(\d{14})_MSM_GPV_Rjp_L-pall_FH(\d{2})-(\d{2})_grib2\.bin")
CYCLES = {int(h) for h in os.getenv("SSI_CYCLES", "0,6,12,18").split(",") if h.strip()}
STEPS = [int(h) for h in os.getenv("SSI_HOURS", "0,6,12").split(",") if h.strip()]
ENABLED = os.getenv("SSI_ENABLED", "1") == "1"
JST = timezone(timedelta(hours=9))
PAGE_URL = "http://database.rish.kyoto-u.ac.jp/arch/jmadata/"

RD, CP, LV, EPS = 287.04, 1005.7, 2.501e6, 0.622
KAPPA = RD / CP


# ---------- 計算 ----------
def es_hpa(t_k):
    """飽和水蒸気圧(hPa、Bolton 1980)。"""
    tc = t_k - 273.15
    return 6.112 * np.exp(17.67 * tc / (tc + 243.5))


def dewpoint(t_k, rh):
    """気温(K)と相対湿度(%)から露点温度(K)。"""
    e = es_hpa(t_k) * np.clip(rh, 1, 100) / 100
    ln = np.log(e / 6.112)
    return 243.5 * ln / (17.67 - ln) + 273.15


def moist_lapse(t_k, p_hpa):
    """偽断熱減率 dT/dp(K/hPa)。"""
    es = es_hpa(t_k)
    rs = EPS * es / (p_hpa - es)
    return (RD * t_k + LV * rs) / (p_hpa * (CP + LV * LV * rs * EPS / (RD * t_k * t_k)))


def ssi(t850, rh850, t500):
    """SSI(K)。引数は同じ形の配列(気温はK、湿度は%)。"""
    td = dewpoint(t850, rh850)
    t_lcl = 1 / (1 / (td - 56) + np.log(t850 / td) / 800) + 56          # Bolton (1980) 式15
    p_lcl = 850 * (t_lcl / t850) ** (1 / KAPPA)
    dry = p_lcl <= 500                                                    # 500hPaまで凝結しない
    tp = np.where(dry, t850 * (500 / 850) ** KAPPA, t_lcl)
    p = np.where(dry, 500.0, p_lcl)
    # 持ち上げ凝結高度から500hPaまで、湿潤断熱で上げる(ルンゲ=クッタ、最大で約 2.5hPa きざみ)
    n = int(np.ceil(np.nanmax(p - 500) / 2.5)) if np.any(~dry) else 0
    for _ in range(n):
        dp = np.maximum(500 - p, -2.5)
        k1 = moist_lapse(tp, p)
        k2 = moist_lapse(tp + k1 * dp / 2, p + dp / 2)
        k3 = moist_lapse(tp + k2 * dp / 2, p + dp / 2)
        k4 = moist_lapse(tp + k3 * dp, p + dp)
        tp = tp + (k1 + 2 * k2 + 2 * k3 + k4) * dp / 6
        p = p + dp
    return t500 - tp


# ---------- データの取得 ----------
def day_url(d: datetime) -> str:
    return f"{RISH}/{d:%Y/%m/%d}/"


def latest_file(listing: str) -> tuple[datetime, str] | None:
    """フォルダの一覧から、FH00 から始まる、いちばん新しい初期時刻のファイル。"""
    best = None
    for m in FILE_RE.finditer(listing):
        stamp, f0 = m.group(1), int(m.group(2))
        t = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        if f0 != 0 or t.hour not in CYCLES:
            continue
        if best is None or t > best[0]:
            best = (t, m.group(0))
    return best


async def find_latest(session, now: datetime) -> tuple[datetime, str] | None:
    for back in (0, 1):
        d = (now - timedelta(days=back)).astimezone(timezone.utc)
        async with session.get(day_url(d)) as r:
            if r.status != 200:
                continue
            found = latest_file(await r.text())
        if found:
            return found[0], day_url(d) + found[1]
    return None


# GRIB2 の要素の番号 (discipline, parameterCategory, parameterNumber)
PARAMS = {(0, 0, 0): "t", (0, 1, 1): "r"}
WANTED = {("t", 850), ("r", 850), ("t", 500)}


def _hours(eccodes, h) -> int | None:
    """予報時間(時間)。単位が分などでも時間に直す。"""
    try:
        ft = eccodes.codes_get_long(h, "forecastTime")
        unit = eccodes.codes_get_long(h, "indicatorOfUnitOfTimeRange")
    except Exception:
        try:
            return int(eccodes.codes_get(h, "endStep"))
        except Exception:
            return None
    return {0: ft // 60, 1: ft, 2: ft * 24, 10: ft * 3, 11: ft * 6, 12: ft * 12, 13: ft // 3600}.get(unit, ft)


def decode(path: str, steps: list[int]) -> dict:
    """GRIB2 から 850hPa の気温・湿度と 500hPa の気温を取り出す。{予報時間: (t850, rh850, t500)}"""
    import eccodes
    out, grid, seen = {}, None, []
    with open(path, "rb") as f:
        while True:
            h = eccodes.codes_grib_new_from_file(f)
            if h is None:
                break
            try:
                g = lambda k: eccodes.codes_get(h, k)  # noqa: E731
                n = lambda k: eccodes.codes_get_long(h, k)  # noqa: E731
                key = (n("discipline"), n("parameterCategory"), n("parameterNumber"))
                name = PARAMS.get(key)
                level = n("level")
                surface = n("typeOfFirstFixedSurface")       # 100 = 等圧面
                if surface == 100 and level > 2000:            # Pa で入っている場合
                    level //= 100
                step = _hours(eccodes, h)
                if len(seen) < 12:
                    seen.append(f"{g('shortName')}/{key}/面{surface}/{level}/{step}h")
                if name is None or surface != 100 or (name, level) not in WANTED or step not in steps:
                    continue
                ni, nj = g("Ni"), g("Nj")
                if grid is None:
                    grid = (np.linspace(g("latitudeOfFirstGridPointInDegrees"),
                                        g("latitudeOfLastGridPointInDegrees"), nj),
                            np.linspace(g("longitudeOfFirstGridPointInDegrees"),
                                        g("longitudeOfLastGridPointInDegrees"), ni))
                out.setdefault(step, {})[f"{name}{level}"] = eccodes.codes_get_values(h).reshape(nj, ni)
            finally:
                eccodes.codes_release(h)
    fields = {s: (v["t850"], v["r850"], v["t500"]) for s, v in out.items() if len(v) == 3}
    return {"grid": grid, "fields": fields, "seen": seen, "size": os.path.getsize(path)}


async def fetch(session, url: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "msm.bin"
        import aiohttp
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=900)) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 20):
                    f.write(chunk)
        return await asyncio.to_thread(decode, str(path), STEPS)


# ---------- 図 ----------
LEVELS = [-15, -9, -6, -3, 0, 3, 6, 9, 15]
COLORS = ["#7b0038", "#c51b29", "#f46d43", "#fdae61", "#fee8a8", "#e0f0f6", "#abd9e9", "#74add1"]


def render(init: datetime, step: int, lats, lons, value, font_path=None) -> bytes:
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from charts import draw_coast, to_png, use_font
    use_font(font_path)
    cmap = ListedColormap(COLORS)
    norm = BoundaryNorm(LEVELS, cmap.N)
    fig, ax = plt.subplots(figsize=(9, 9))
    cf = ax.contourf(lons, lats, np.clip(value, LEVELS[0], LEVELS[-1] - 0.01), levels=LEVELS, cmap=cmap, norm=norm)
    cs = ax.contour(lons, lats, value, levels=[-6, -3, 0, 3], colors="k", linewidths=[0.8, 0.8, 1.4, 0.6])
    ax.clabel(cs, fmt="%d", fontsize=8)
    draw_coast(ax, color="#333333", lw=0.7)
    ax.set_xlim(float(lons.min()), float(lons.max()))
    ax.set_ylim(float(lats.min()), float(lats.max()))
    ax.set_aspect(1 / np.cos(np.radians(float(lats.mean()))))
    ax.grid(True, color="#999999", lw=0.3)
    cb = fig.colorbar(cf, ax=ax, orientation="horizontal", pad=0.05, fraction=0.04, aspect=50, ticks=LEVELS)
    cb.set_label("SSI (K)  小さいほど不安定")
    valid = init + timedelta(hours=step)
    kind = "初期値" if step == 0 else f"{step}時間予想"
    ax.set_title(f"SSI 分布 MSM {kind}  {valid.astimezone(JST):%m/%d %H時} JST"
                 f"(初期時刻 {init:%m/%d %H}UTC)", loc="left", fontsize=11)
    cb.ax.text(1, -2.2, "気象庁MSM(京都大学生存圏研究所のアーカイブ)から計算", transform=cb.ax.transAxes,
               ha="right", va="top", fontsize=8, color="#555555")
    return to_png(fig, dpi=95)
