"""ウィンドプロファイラ(気象庁 WINDAS)の観測値を取得し、時間高度断面図にする。

取得元(気象庁ホームページ「ウィンドプロファイラ」が使っているデータ)
- 地点:  /bosai/windprofiler/const/station.json
- 時刻:  /bosai/windprofiler/data/times.json
- 観測値:/bosai/windprofiler/data/<地点番号>.json
  {"YYYYMMDDHHMMSS"(UTC): [{"height": m, "u": m/s, "v": m/s, "w": m/s, ...}, ...], ...}
"""
import math
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import numpy as np

JMA = "https://www.jma.go.jp"
BASE = f"{JMA}/bosai/windprofiler"
PAGE_URL = f"{JMA}/bosai/map.html#contents=windprofiler"
JST = timezone(timedelta(hours=9))
KT = 1 / 0.514444

# 気象庁「ウィンドプロファイラの解説」の観測所一覧(北から順)
STATIONS = [
    ("留萌", "47406"), ("帯広", "47417"), ("室蘭", "47423"), ("宮古", "47585"), ("酒田", "47587"),
    ("仙台", "47590"), ("若松", "47570"), ("高田", "47612"), ("福井", "47616"), ("水戸", "47629"),
    ("熊谷", "47626"), ("勝浦", "47674"), ("八丈島", "47678"), ("河口湖", "47640"), ("静岡", "47656"),
    ("名古屋", "47636"), ("尾鷲", "47663"), ("美浜", "47795"), ("鳥取", "47746"), ("浜田", "47755"),
    ("高松", "47891"), ("高知", "47893"), ("清水", "47898"), ("厳原", "47800"), ("平戸", "47805"),
    ("大分", "47815"), ("熊本", "47819"), ("延岡", "47822"), ("市来", "47848"), ("屋久島", "47836"),
    ("名瀬", "47909"), ("南大東島", "47945"), ("与那国島", "47912"),
]
STATION_IDS = dict(STATIONS)
AUTO_STATIONS = [s.strip() for s in os.getenv("WPR_STATIONS", "高知,清水,大分").split(",") if s.strip()]
AUTO_INTERVAL = int(os.getenv("WPR_INTERVAL_MINUTES", "60"))
DEFAULT_HOURS = int(os.getenv("WPR_HOURS", "6"))


def parse_utc(s: str) -> datetime:
    return datetime.strptime(s[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _num(v):
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    if isinstance(v, list) and v:          # [値, 品質] の形にも対応
        return _num(v[0])
    return None


def parse(data: dict) -> list[tuple[datetime, list[tuple[float, float, float, float | None]]]]:
    """観測値のJSONを [(時刻, [(高度m, u, v, w), ...]), ...](古い順)にする。"""
    out = []
    for key, rows in (data or {}).items():
        try:
            t = parse_utc(key)
        except ValueError:
            continue
        levels = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            h, u, v = _num(r.get("height")), _num(r.get("u")), _num(r.get("v"))
            if h is None or u is None or v is None:
                continue
            levels.append((h, u, v, _num(r.get("w"))))
        if levels:
            out.append((t, sorted(levels)))
    return sorted(out, key=lambda x: x[0])


def resolve(name: str) -> tuple[str, str] | None:
    """地点名(または地点番号)から (地点名, 地点番号)。"""
    name = name.strip()
    if name in STATION_IDS:
        return name, STATION_IDS[name]
    for n, i in STATIONS:
        if name == i or name.startswith(n) or n.startswith(name):
            return n, i
    return None


def render(title: str, series, hours: int, font_path=None) -> bytes:
    """時間高度断面図(矢羽=水平風、色の四角=鉛直流)。"""
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from charts import to_png, use_font

    use_font(font_path)
    if not series:
        raise ValueError("観測値がありません")
    end = series[-1][0]
    series = [s for s in series if s[0] >= end - timedelta(hours=hours)]
    ts, hs, us, vs, ws = [], [], [], [], []
    for t, levels in series:
        for h, u, v, w in levels:
            ts.append(t.astimezone(JST).replace(tzinfo=None))
            hs.append(h / 1000)
            us.append(u * KT)
            vs.append(v * KT)
            ws.append(np.nan if w is None else w)
    ts, hs, us, vs, ws = map(np.array, (ts, hs, us, vs, ws))
    speed = np.hypot(us, vs)

    # 時刻が多いときは間引く(図が真っ黒にならないように、最大36本)
    times = sorted(set(ts))
    step = max(1, math.ceil(len(times) / 24))
    keep_t = set(times[::-1][::step])
    heights = sorted(set(hs))
    hstep = max(1, math.ceil(len(heights) / 22))     # 高度も最大22段に間引く(下から)
    keep_h = set(heights[::hstep])
    keep = np.array([t in keep_t and h in keep_h for t, h in zip(ts, hs)])

    fig, ax = plt.subplots(figsize=(11, 7.5))
    good = keep & np.isfinite(ws)
    if good.any():
        sc = ax.scatter(ts[good], hs[good], c=ws[good], cmap="RdBu_r", vmin=-3, vmax=3, marker="s",
                        s=90, alpha=0.5, linewidths=0, zorder=1)
        cb = fig.colorbar(sc, ax=ax, orientation="horizontal", pad=0.14, fraction=0.04, aspect=50)
        cb.set_label("鉛直流 w (m/s)  +上昇 / −下降")
    bounds = [0, 10, 20, 30, 40, 50, 60, 80, 100, 200]
    colors = ["#9e9e9e", "#2b83ba", "#1a9850", "#a6d96a", "#fee08b", "#fdae61", "#f46d43", "#d73027", "#7b3294"]
    cmap = ListedColormap(colors)
    norm = BoundaryNorm(bounds, cmap.N)
    bb = ax.barbs(ts[keep], hs[keep], us[keep], vs[keep], speed[keep], cmap=cmap, norm=norm,
                  length=5.0, linewidth=0.9, zorder=3)
    cb2 = fig.colorbar(bb, ax=ax, pad=0.01, fraction=0.03, ticks=bounds)
    cb2.set_label("風速 (kt)")
    ax.set_ylim(0, max(1.0, float(np.nanmax(hs)) + 0.3))
    ax.set_ylabel("高度 (km)")
    ax.set_xlabel("観測時刻 (JST)  ※右ほど新しい")
    import matplotlib.dates as mdates
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d日\n%H:%M"))
    ax.set_xlim(times[0] - timedelta(minutes=10), times[-1] + timedelta(minutes=10))
    ax.grid(True, color="#dddddd", lw=0.5, zorder=0)
    ax.set_title(title, loc="left")
    ax.text(1.0, 1.01, "矢羽:短5kt・長10kt・旗50kt  出典:気象庁", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=8, color="#555555")
    return to_png(fig)


class WindProfiler:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.names: dict[str, str] | None = None

    async def _json(self, url: str):
        async with self.session.get(url) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def load_names(self) -> None:
        """地点一覧を気象庁から読む(読めなければ内蔵の一覧を使う)。"""
        if self.names is not None:
            return
        self.names = {i: n for n, i in STATIONS}
        try:
            table = await self._json(f"{BASE}/const/station.json")
            for sid, s in table.items():
                if isinstance(s, dict) and s.get("jpname"):
                    self.names[str(sid)] = s["jpname"]
        except (aiohttp.ClientError, ValueError):
            pass

    async def latest_time(self) -> datetime:
        times = await self._json(f"{BASE}/data/times.json")
        if isinstance(times, dict):
            times = list(times.keys()) or list(times.values())
        return max(parse_utc(str(t)) for t in times)

    async def series(self, station_id: str):
        return parse(await self._json(f"{BASE}/data/{station_id}.json"))
