"""画像の投稿間隔を自動で切り替える。

判定には警報のコードを使わず、次の2つを使う(警報体系の変更に左右されないため)。
- 大雨:監視範囲内のアメダスで、1時間降水量がしきい値以上の地点があるか
- 台風:気象庁が解析中の台風(熱帯低気圧を含む)があるか
"""
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

JMA = "https://www.jma.go.jp"
JST = timezone(timedelta(hours=9))

NORMAL_INTERVAL = int(os.getenv("IMAGE_INTERVAL_MINUTES", "60"))
HEAVY_RAIN_INTERVAL = int(os.getenv("HEAVY_RAIN_INTERVAL", "10"))
TYPHOON_INTERVAL = int(os.getenv("TYPHOON_INTERVAL", "30"))
HEAVY_RAIN_MM = float(os.getenv("HEAVY_RAIN_MM", "30"))          # 30mm/h = 激しい雨
HOLD_MINUTES = int(os.getenv("HEAVY_RAIN_HOLD_MINUTES", "120"))  # 雨が弱まってもしばらく維持
# 監視範囲(西経度,南緯度,東経度,北緯度)。既定は九州・四国・中国地方
WATCH_BBOX = tuple(float(v) for v in os.getenv("WATCH_BBOX", "128.5,30.0,135.5,35.8").split(","))


@dataclass
class Mode:
    name: str
    interval: int
    reason: str


class ModeWatcher:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.stations: dict[str, tuple[str, float, float]] | None = None  # id -> (名前, 緯度, 経度)
        self.heavy_until: datetime | None = None
        self.last_heavy_reason = ""
        self.current = Mode("通常", NORMAL_INTERVAL, "")
        self.previous_name: str | None = None

    async def _json(self, url: str):
        async with self.session.get(url) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def _load_stations(self) -> None:
        table = await self._json(f"{JMA}/bosai/amedas/const/amedastable.json")
        stations = {}
        lon0, lat0, lon1, lat1 = WATCH_BBOX
        for sid, s in table.items():
            lat = s["lat"][0] + s["lat"][1] / 60
            lon = s["lon"][0] + s["lon"][1] / 60
            if lon0 <= lon <= lon1 and lat0 <= lat <= lat1:
                stations[sid] = (s.get("kjName", sid), lat, lon)
        self.stations = stations

    async def heavy_rain(self) -> tuple[float, str] | None:
        """監視範囲内の最大1時間降水量と地点名。しきい値未満ならNone。"""
        if self.stations is None:
            await self._load_stations()
        async with self.session.get(f"{JMA}/bosai/amedas/data/latest_time.txt") as r:
            r.raise_for_status()
            latest = datetime.fromisoformat((await r.text()).strip())
        data = await self._json(f"{JMA}/bosai/amedas/data/map/{latest:%Y%m%d%H%M%S}.json")
        best = None
        for sid, (name, _, _) in self.stations.items():
            value = data.get(sid, {}).get("precipitation1h")
            if not value or value[0] is None or value[1] != 0:  # 品質フラグ0のみ使う
                continue
            if best is None or value[0] > best[0]:
                best = (value[0], f"{name} {value[0]:.1f}mm/h({latest.astimezone(JST):%H:%M}時点)")
        return best if best and best[0] >= HEAVY_RAIN_MM else None

    async def typhoon(self) -> int:
        """気象庁が解析中の台風・熱帯低気圧の数。"""
        return len(await self._json(f"{JMA}/bosai/typhoon/data/targetTc.json"))

    async def evaluate(self) -> tuple[Mode, bool]:
        """現在のモードと、前回から変わったかどうかを返す。"""
        now = datetime.now(JST)
        rain = await self.heavy_rain()
        if rain:
            self.heavy_until = now + timedelta(minutes=HOLD_MINUTES)
            self.last_heavy_reason = rain[1]
        tc_count = await self.typhoon()

        if self.heavy_until and now < self.heavy_until:
            mode = Mode("大雨監視", HEAVY_RAIN_INTERVAL,
                        f"監視範囲で{HEAVY_RAIN_MM:g}mm/h以上を観測:{self.last_heavy_reason}")
        elif tc_count:
            mode = Mode("台風監視", TYPHOON_INTERVAL, f"気象庁が台風・熱帯低気圧を{tc_count}個解析中")
        else:
            mode = Mode("通常", NORMAL_INTERVAL, "")
        changed = mode.name != self.current.name
        self.previous_name = self.current.name
        self.current = mode
        return mode, changed
