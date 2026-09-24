"""アメダスの観測値(気象庁)を一覧表にする。

- 値は気象庁アメダスの速報値。品質フラグが0(正常)の値だけを使い、それ以外は「―」にする。
- 3時間気圧変化は、同じ地点の3時間前の海面気圧との差。
"""
import os
import unicodedata
from datetime import datetime, timedelta, timezone

import aiohttp

JMA = "https://www.jma.go.jp"
JST = timezone(timedelta(hours=9))

# 投稿する時刻(JST)。地上の総観観測時刻 00・06・12・18UTC に合わせている
AMEDAS_HOURS = {int(h) for h in os.getenv("AMEDAS_HOURS", "3,9,15,21").split(",")}
# 表に載せる地点(アメダスの地点名)。九州・四国・中国地方の気象台など
AMEDAS_STATIONS = [n.strip() for n in os.getenv(
    "AMEDAS_STATIONS",
    "福岡,佐賀,長崎,熊本,大分,宮崎,鹿児島,下関,広島,松江,松山,高知,室戸岬,徳島,高松",
).split(",") if n.strip()]

WIND_DIRS = ["静穏", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東", "南",
             "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西", "北"]


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int, right: bool = False) -> str:
    space = " " * max(0, width - display_width(text))
    return space + text if right else text + space


def value(obs: dict, key: str) -> float | None:
    v = obs.get(key)
    if not v or v[0] is None or v[1] != 0:
        return None
    return v[0]


def fmt(v: float | None, spec: str) -> str:
    return "―" if v is None else format(v, spec)


class AmedasTable:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.names: dict[str, list[str]] | None = None  # 地点名 -> 地点IDの候補

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

    async def build(self, t: datetime) -> tuple[str, list[str]]:
        """表の文字列と、見つからなかった地点名を返す。"""
        if self.names is None:
            await self._load_names()
        obs = await self.observation(t)
        try:
            before = await self.observation(t - timedelta(hours=3))
        except aiohttp.ClientError:
            before = {}

        header = (pad("地点", 8) + pad("気温", 6, True) + pad("湿度", 5, True)
                  + pad("海面気圧", 9, True) + pad("3h変化", 7, True)
                  + "  " + pad("風向", 6) + pad("風速", 5, True) + pad("1h雨", 6, True))
        lines = [header, "-" * display_width(header)]
        missing = []
        for name in AMEDAS_STATIONS:
            sid = self._resolve(name, obs)
            if sid is None:
                missing.append(name)
                continue
            o = obs.get(sid, {})
            p = value(o, "normalPressure")
            p0 = value(before.get(sid, {}), "normalPressure")
            tendency = None if p is None or p0 is None else p - p0
            wd = value(o, "windDirection")
            lines.append(
                pad(name, 8)
                + pad(fmt(value(o, "temp"), ".1f"), 6, True)
                + pad(fmt(value(o, "humidity"), ".0f"), 5, True)
                + pad(fmt(p, ".1f"), 9, True)
                + pad(fmt(tendency, "+.1f"), 7, True)
                + "  " + pad("―" if wd is None else WIND_DIRS[int(wd)], 6)
                + pad(fmt(value(o, "wind"), ".1f"), 5, True)
                + pad(fmt(value(o, "precipitation1h"), ".1f"), 6, True)
            )
        return "\n".join(lines), missing

    @staticmethod
    def latest_synoptic(latest: datetime) -> datetime | None:
        """観測済みの時刻のうち、直近の投稿対象時刻(3・9・15・21時など)。"""
        t = latest.replace(minute=0, second=0, microsecond=0)
        for _ in range(24):
            if t.hour in AMEDAS_HOURS:
                return t
            t -= timedelta(hours=1)
        return None
