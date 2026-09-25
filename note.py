"""予報ノート:毎朝スレッドを作り、予想を受け付け、翌日にアメダスの実測値で採点する。

採点に使う実測値は、気象庁アメダスの10分値(品質フラグ0のみ)から計算する。
- 最高気温:その日(0時10分〜24時)の10分値の最大
- 降水の有無:その日の10分間降水量の合計が1.0mm以上なら「あり」
※ 公式の日最高気温(より細かい時間間隔の値から求める)とはわずかに異なる場合がある。
"""
import os
from datetime import date, datetime, time, timedelta, timezone

import aiohttp

JMA = "https://www.jma.go.jp"
JST = timezone(timedelta(hours=9))

NOTE_HOUR = int(os.getenv("NOTE_HOUR", "7"))               # スレッドを作る時刻
NOTE_DEADLINE_HOUR = int(os.getenv("NOTE_DEADLINE_HOUR", "12"))  # 予想の締切
SCORE_HOUR = int(os.getenv("SCORE_HOUR", "1"))             # 翌日のこの時刻に採点
NOTE_STATION = os.getenv("NOTE_STATION", "高知")
NOTE_OFFICE = os.getenv("NOTE_OFFICE", "390000")     # 気象庁の予報を取る府県予報区(390000=高知県)
NOTE_POP_AREA = os.getenv("NOTE_POP_AREA", "")       # 降水確率の地域コード(空欄なら府県内の最初の地域)
RAIN_THRESHOLD_MM = 1.0
JMA_RAIN_POP = 50  # 気象庁の降水確率がこの値(%)以上の時間帯があれば「降水あり」の予報とみなす
JMA_ID = "jma"

CHECKLIST = """**今日のチェック項目**(スレッドに自由に書き込んでください)
1. 地上天気図:高気圧・低気圧・前線の位置と、今後の動き
2. 500hPa:トラフ・リッジの位置、渦度の極大域
3. 850hPa:気温・風、相当温位の集中帯(FXJP854)
4. 700hPa:湿数と鉛直流(上昇流域はどこか)
5. 衛星画像:雲域の特徴、水蒸気画像の暗域
6. 短期予報解説資料:予報官が注目しているポイント
7. アメダス:3時間気圧変化や風向の変化"""


def score(pred_temp: float, pred_rain: bool, obs_temp: float, obs_rain: bool) -> tuple[int, str]:
    err = abs(pred_temp - obs_temp)
    temp_pt = 10 if err <= 1 else 7 if err <= 2 else 4 if err <= 3 else 0
    rain_pt = 5 if pred_rain == obs_rain else 0
    return temp_pt + rain_pt, f"気温誤差 {err:.1f}℃ → {temp_pt}点/降水 {'的中' if rain_pt else '外れ'} → {rain_pt}点"


class NoteManager:
    def __init__(self, session: aiohttp.ClientSession, state: dict) -> None:
        self.session = session
        self.state = state
        self.state.setdefault("notes", {})   # "YYYY-MM-DD" -> ノート情報
        self.state.setdefault("scores", {})  # ユーザーID -> 成績
        self.station_id: str | None = None

    @property
    def notes(self) -> dict:
        return self.state["notes"]

    # ---------- 時刻の判定 ----------
    @staticmethod
    def now() -> datetime:
        return datetime.now(JST)

    def should_create(self, now: datetime) -> bool:
        key = now.date().isoformat()
        return (key not in self.notes
                and NOTE_HOUR <= now.hour < NOTE_DEADLINE_HOUR)

    def deadline(self, day: date) -> datetime:
        return datetime.combine(day, time(NOTE_DEADLINE_HOUR), JST)

    def due_for_scoring(self, now: datetime) -> list[str]:
        due = []
        for key, note in self.notes.items():
            day = date.fromisoformat(key)
            score_time = datetime.combine(day + timedelta(days=1), time(SCORE_HOUR), JST)
            if not note.get("scored") and now >= score_time:
                due.append(key)
        return sorted(due)

    # ---------- 予想の受付 ----------
    def submit(self, user_id: int, user_name: str, temp: float, rain: bool, memo: str) -> str:
        now = self.now()
        key = now.date().isoformat()
        note = self.notes.get(key)
        if note is None:
            return f"今日の予報ノートはまだありません({NOTE_HOUR}時に作成されます)。"
        if now >= self.deadline(now.date()):
            return f"今日の締切({NOTE_DEADLINE_HOUR}時)を過ぎています。また明日挑戦してください。"
        note["predictions"][str(user_id)] = {
            "name": user_name, "temp": temp, "rain": rain, "memo": memo,
            "submitted": now.isoformat(timespec="minutes")}
        return (f"受け付けました:{note['station']}の最高気温 {temp:.1f}℃、降水 {'あり' if rain else 'なし'}"
                f"\n締切({NOTE_DEADLINE_HOUR}時)までは何度でも出し直せます。")

    # ---------- 実測値の取得 ----------
    async def _json(self, url: str):
        async with self.session.get(url) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def resolve_station(self) -> str:
        if self.station_id is None:
            table = await self._json(f"{JMA}/bosai/amedas/const/amedastable.json")
            ids = [sid for sid, s in table.items() if s.get("kjName") == NOTE_STATION]
            if not ids:
                raise RuntimeError(f"アメダス地点「{NOTE_STATION}」が見つかりません")
            # 同名が複数ある場合は、気圧を観測している地点(気象台など)を優先
            async with self.session.get(f"{JMA}/bosai/amedas/data/latest_time.txt") as r:
                r.raise_for_status()
                latest = datetime.fromisoformat((await r.text()).strip())
            obs = await self._json(f"{JMA}/bosai/amedas/data/map/{latest:%Y%m%d%H%M%S}.json")
            ids.sort(key=lambda i: "normalPressure" not in obs.get(i, {}))
            self.station_id = ids[0]
        return self.station_id

    async def observed(self, day: date) -> tuple[float, float]:
        """その日の最高気温(10分値の最大)と降水量合計を返す。"""
        sid = await self.resolve_station()
        start = datetime.combine(day, time(0), JST)
        end = start + timedelta(days=1)
        records: dict[str, dict] = {}
        for d, hours in ((day, range(0, 24, 3)), (day + timedelta(days=1), [0])):
            for h in hours:
                url = f"{JMA}/bosai/amedas/data/point/{sid}/{d:%Y%m%d}_{h:02d}.json"
                try:
                    records.update(await self._json(url))
                except aiohttp.ClientResponseError:
                    continue
        temps, rain, count = [], 0.0, 0
        for stamp, obs in records.items():
            t = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=JST)
            if not (start < t <= end):
                continue
            count += 1
            v = obs.get("temp")
            if v and v[0] is not None and v[1] == 0:
                temps.append(v[0])
            p = obs.get("precipitation10m")
            if p and p[0] is not None and p[1] == 0:
                rain += p[0]
        if count < 130 or not temps:  # 1日144個のうち大半がそろっていなければ採点しない
            raise RuntimeError(f"{day} の観測データがそろっていません({count}/144)")
        return max(temps), rain

    # ---------- 気象庁の予報 ----------
    async def jma_forecast(self, day: date) -> dict:
        """その日の気象庁の予報(最高気温・降水確率の最大)を取得する。"""
        sid = await self.resolve_station()
        data = await self._json(f"{JMA}/bosai/forecast/data/forecast/{NOTE_OFFICE}.json")
        short = data[0]
        temp, pops = None, []
        for ts in short["timeSeries"]:
            times = [datetime.fromisoformat(t) for t in ts["timeDefines"]]
            for area in ts["areas"]:
                code = area["area"]["code"]
                if "temps" in area and code == sid:
                    for t, v in zip(times, area["temps"]):
                        if t.date() == day and t.hour == 9 and v != "":  # 9時の値=日中の最高気温
                            temp = float(v)
                if "pops" in area and (code == NOTE_POP_AREA or (not NOTE_POP_AREA and not pops)):
                    pops = [int(v) for t, v in zip(times, area["pops"]) if t.date() == day and v != ""]
        if temp is None and len(data) > 1:  # 短期予報に無ければ週間予報の最高気温を使う
            for ts in data[1]["timeSeries"]:
                times = [datetime.fromisoformat(t) for t in ts["timeDefines"]]
                for area in ts["areas"]:
                    if area["area"]["code"] == sid and "tempsMax" in area:
                        for t, v in zip(times, area["tempsMax"]):
                            if t.date() == day and v != "":
                                temp = float(v)
        if temp is None:
            raise RuntimeError("気象庁の予報から最高気温を読み取れませんでした")
        pop = max(pops) if pops else None
        return {"temp": temp, "pop": pop, "rain": pop is not None and pop >= JMA_RAIN_POP,
                "report": short.get("reportDatetime", "")}

    # ---------- 採点 ----------
    async def grade(self, key: str) -> tuple[str, list[str]]:
        """採点して、結果の文章と、点数の一覧を返す。"""
        note = self.notes[key]
        day = date.fromisoformat(key)
        obs_temp, obs_rain_mm = await self.observed(day)
        obs_rain = obs_rain_mm >= RAIN_THRESHOLD_MM
        lines, graded = [], {}
        jma = note.get("jma")
        jma_pt = None
        if jma:
            jma_pt, detail = score(jma["temp"], jma["rain"], obs_temp, obs_rain)
            report = datetime.fromisoformat(jma["report"]).strftime("%H時") if jma.get("report") else ""
            pop = f"降水確率最大{jma['pop']}%" if jma.get("pop") is not None else "降水確率なし"
            lines.append(f"🏛️ **気象庁**({report}発表):予想 {jma['temp']:.1f}℃・{pop}→{'あり' if jma['rain'] else 'なし'}"
                         f" → **{jma_pt}点**({detail})")
            graded[JMA_ID] = self._grade_row("気象庁", jma["temp"], jma["rain"], obs_temp, obs_rain, jma_pt)
        for uid, p in sorted(note["predictions"].items(), key=lambda x: x[1]["submitted"]):
            pt, detail = score(p["temp"], p["rain"], obs_temp, obs_rain)
            versus = ""
            if jma_pt is not None:
                versus = " 🏆気象庁に勝ち" if pt > jma_pt else " 🤝引き分け" if pt == jma_pt else " 気象庁の勝ち"
            lines.append(f"**{p['name']}**:予想 {p['temp']:.1f}℃・{'あり' if p['rain'] else 'なし'}"
                         f" → **{pt}点**({detail}){versus}")
            self._record(uid, p["name"], key, pt, abs(p["temp"] - obs_temp))
            graded[uid] = self._grade_row(p["name"], p["temp"], p["rain"], obs_temp, obs_rain, pt)
        note["graded"] = graded
        header = (f"**{day:%m/%d}の結果({note['station']})**\n"
                  f"実測:最高気温 **{obs_temp:.1f}℃**/降水量 {obs_rain_mm:.1f}mm"
                  f"(降水{'あり' if obs_rain else 'なし'})\n"
                  "出典:気象庁アメダス(10分値から算出)")
        note["scored"] = True
        note["result"] = {"temp": obs_temp, "rain_mm": obs_rain_mm}
        return header, lines

    @staticmethod
    def _grade_row(name, temp, rain, obs_temp, obs_rain, pt) -> dict:
        return {"name": name, "pt": pt, "err": round(temp - obs_temp, 1),
                "pred_rain": rain, "obs_rain": obs_rain}

    # ---------- 月間の検証 ----------
    def monthly(self, year: int, month: int) -> str | None:
        """その月の成績を、気象庁と比べた検証表にする。"""
        prefix = f"{year:04d}-{month:02d}"
        rows: dict[str, list[dict]] = {}
        jma_by_day: dict[str, dict] = {}
        for key, note in sorted(self.notes.items()):
            if not key.startswith(prefix) or "graded" not in note:
                continue
            for uid, g in note["graded"].items():
                rows.setdefault(uid, []).append(g | {"day": key})
            if JMA_ID in note["graded"]:
                jma_by_day[key] = note["graded"][JMA_ID]
        if not rows:
            return None
        from amedas import display_width, pad

        def cut(text: str, width: int) -> str:
            while display_width(text) > width:
                text = text[:-1]
            return text

        heads = [("参加者", 12, False), ("日数", 5, True), ("平均点", 7, True), ("気温誤差", 9, True),
                 ("RMSE", 7, True), ("降水的中", 9, True), ("見逃し", 7, True), ("空振り", 7, True),
                 ("対気象庁", 12, True)]
        lines = [f"📊 **{year}年{month}月の予報検証({NOTE_STATION})**", "```"]
        lines.append("".join(pad(h, w, r) for h, w, r in heads))
        order = [JMA_ID] + [u for u in rows if u != JMA_ID] if JMA_ID in rows else list(rows)
        for uid in order:
            gs = rows[uid]
            n = len(gs)
            mae = sum(abs(g["err"]) for g in gs) / n
            rmse = (sum(g["err"] ** 2 for g in gs) / n) ** 0.5
            hit = sum(g["pred_rain"] == g["obs_rain"] for g in gs) / n * 100
            miss = sum(g["obs_rain"] and not g["pred_rain"] for g in gs)
            false = sum(g["pred_rain"] and not g["obs_rain"] for g in gs)
            vs = "―"
            if uid != JMA_ID:
                pairs = [(g["pt"], jma_by_day[g["day"]]["pt"]) for g in gs if g["day"] in jma_by_day]
                if pairs:
                    w = sum(a > b for a, b in pairs)
                    l = sum(a < b for a, b in pairs)
                    vs = f"{w}勝{l}敗{len(pairs) - w - l}分"
            cells = [cut(gs[-1]["name"], 11), str(n), f"{sum(g['pt'] for g in gs) / n:.1f}",
                     f"{mae:.1f}℃", f"{rmse:.1f}℃", f"{hit:.0f}%", str(miss), str(false), vs]
            lines.append("".join(pad(c, w, r) for c, (_, w, r) in zip(cells, heads)))
        lines.append("```")
        lines.append("気温誤差:最高気温の誤差の大きさの平均/RMSE:大きく外した日ほど重く数える誤差/"
                     "見逃し:降水なしと予想して降った日/空振り:降水ありと予想して降らなかった日")
        return "\n".join(lines)

    def _record(self, uid: str, name: str, key: str, pt: int, err: float) -> None:
        s = self.state["scores"].setdefault(uid, {
            "name": name, "total": 0, "count": 0, "err_sum": 0.0, "streak": 0, "best_streak": 0, "last": None})
        prev = s["last"]
        yesterday = (date.fromisoformat(key) - timedelta(days=1)).isoformat()
        s["streak"] = s["streak"] + 1 if prev == yesterday else 1
        s["best_streak"] = max(s["best_streak"], s["streak"])
        s.update(name=name, last=key, total=s["total"] + pt, count=s["count"] + 1,
                 err_sum=s["err_sum"] + err)

    def summary(self, uid: str) -> str:
        s = self.state["scores"].get(uid)
        if not s:
            return "まだ採点された予想がありません。"
        avg = s["err_sum"] / s["count"]
        return (f"**{s['name']}さんの成績**\n累計 {s['total']}点({s['count']}回、1回平均 {s['total'] / s['count']:.1f}点)\n"
                f"最高気温の平均誤差 {avg:.1f}℃\n連続提出 {s['streak']}日(最長 {s['best_streak']}日)")

    def prune(self, keep_days: int = 45) -> None:
        limit = (self.now().date() - timedelta(days=keep_days)).isoformat()
        for key in [k for k in self.notes if k < limit and self.notes[k].get("scored")]:
            del self.notes[key]
