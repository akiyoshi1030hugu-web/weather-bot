"""アメダス天気図:気象庁アメダスの観測値(気温・風・1時間降水量)を地図に描く。

- 値は気象庁アメダスの速報値。品質フラグが0(正常)の値だけを使う。
- 風は矢羽(短5kt・長10kt・旗50kt)、気温は点の色、1時間降水量は青い四角(1mm以上)。
"""
import math
import os
from datetime import datetime, timedelta, timezone

import aiohttp

JMA = "https://www.jma.go.jp"
JST = timezone(timedelta(hours=9))
HOURS = {int(h) for h in os.getenv("AMEDAS_MAP_HOURS", "0,3,6,9,12,15,18,21").split(",") if h.strip()}
AREAS = [("全国", (127.0, 24.0, 146.5, 46.0), 0.45), ("西日本", (128.5, 30.0, 135.5, 35.8), 0.0)]
PAGE_URL = f"{JMA}/bosai/amedas/"


def good(obs: dict, key: str):
    v = obs.get(key)
    if isinstance(v, list) and len(v) >= 2 and v[1] == 0 and isinstance(v[0], (int, float)):
        return float(v[0])
    return None


def deg(v) -> float:
    return float(v[0]) + float(v[1]) / 60 if isinstance(v, list) else float(v)


def points(table: dict, data: dict) -> list[dict]:
    out = []
    for sid, obs in data.items():
        st = table.get(sid)
        if not st:
            continue
        p = {"lat": deg(st["lat"]), "lon": deg(st["lon"]), "temp": good(obs, "temp"),
             "rain": good(obs, "precipitation1h"), "u": None, "v": None}
        ws, wd = good(obs, "wind"), good(obs, "windDirection")
        if ws is not None and wd is not None:
            if wd == 0 or ws == 0:
                p["u"] = p["v"] = 0.0
            else:
                a = math.radians(wd * 22.5)                 # 風の吹いてくる方向(16方位、16=北)
                kt = ws / 0.514444
                p["u"], p["v"] = -kt * math.sin(a), -kt * math.cos(a)
        out.append(p)
    return out


def thin(pts: list[dict], cell: float) -> list[dict]:
    """矢羽が重ならないよう、cell 度の升目ごとに1地点だけにする。"""
    if cell <= 0:
        return pts
    seen, out = set(), []
    for p in pts:
        k = (int(p["lat"] // cell), int(p["lon"] // cell))
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def render(t: datetime, pts: list[dict], name: str, bbox, cell: float, font_path=None) -> bytes:
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from charts import draw_coast, to_png, use_font
    use_font(font_path)
    w, s, e, n = bbox
    inside = [p for p in pts if w <= p["lon"] <= e and s <= p["lat"] <= n]
    fig, ax = plt.subplots(figsize=(10, 10 * (n - s) / (e - w) / math.cos(math.radians((n + s) / 2)) + 0.6))
    draw_coast(ax, color="#777777", lw=0.7)
    rain = [p for p in inside if p["rain"] and p["rain"] >= 1]
    if rain:
        rb = [1, 5, 10, 20, 30, 50, 80, 200]
        rc = ListedColormap(["#a0d2ff", "#218cff", "#0041ff", "#fff500", "#ff9900", "#ff2800", "#b40068"])
        ax.scatter([p["lon"] for p in rain], [p["lat"] for p in rain], c=[p["rain"] for p in rain],
                   cmap=rc, norm=BoundaryNorm(rb, rc.N), marker="s", s=55 if cell else 110, zorder=2,
                   edgecolors="none")
    tp = [p for p in inside if p["temp"] is not None]
    if tp:
        sc = ax.scatter([p["lon"] for p in tp], [p["lat"] for p in tp], c=[p["temp"] for p in tp],
                        cmap="turbo", vmin=-10, vmax=35, s=10 if cell else 22, zorder=3, edgecolors="none")
        cb = fig.colorbar(sc, ax=ax, orientation="horizontal", pad=0.04, fraction=0.035, aspect=50)
        cb.set_label("気温 (℃)")
    wind = thin([p for p in inside if p["u"] is not None], cell)
    if wind:
        ax.barbs([p["lon"] for p in wind], [p["lat"] for p in wind], [p["u"] for p in wind],
                 [p["v"] for p in wind], length=5 if cell else 5.5, linewidth=0.7, zorder=4)
    ax.set_xlim(w, e)
    ax.set_ylim(s, n)
    ax.set_aspect(1 / math.cos(math.radians((n + s) / 2)))
    ax.grid(True, color="#e5e5e5", lw=0.5)
    ax.set_title(f"アメダス天気図({name}) {t.astimezone(JST):%Y-%m-%d %H:%M} JST", loc="left")
    ax.text(1, 1.01, "矢羽:風(短5kt・長10kt)/点:気温/四角:1時間降水量(1mm以上)  出典:気象庁", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=8, color="#555555")
    return to_png(fig, dpi=100)


class AmedasMap:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.table: dict | None = None

    async def _json(self, url: str):
        async with self.session.get(url) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def latest_time(self) -> datetime:
        async with self.session.get(f"{JMA}/bosai/amedas/data/latest_time.txt") as r:
            r.raise_for_status()
            return datetime.fromisoformat((await r.text()).strip())

    async def load(self, t: datetime) -> list[dict]:
        if self.table is None:
            self.table = await self._json(f"{JMA}/bosai/amedas/const/amedastable.json")
        data = await self._json(f"{JMA}/bosai/amedas/data/map/{t.astimezone(JST):%Y%m%d%H%M%S}.json")
        return points(self.table, data)


def target_time(latest: datetime) -> datetime | None:
    """latest 以前で、投稿する時刻(HOURS の正時)のうち最新のもの。"""
    t = latest.astimezone(JST).replace(minute=0, second=0, microsecond=0)
    for _ in range(25):
        if t.hour in HOURS and t <= latest:
            return t
        t -= timedelta(hours=1)
    return None


