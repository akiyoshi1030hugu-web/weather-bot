"""気象庁「過去の気象データ検索(高層)」からラジオゾンデ観測値を取得し、エマグラムを作る。

- 観測は日本時間9時・21時。表が公開されるまでは、30分おきに再確認する。
- 値に付く記号のうち、「]」(資料不足値)・「×」(欠測)・「///」などは使わない。
  「)」(準正常値)は値として使う。
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
BASES = ["https://www.data.jma.go.jp/stats/etrn/upper/view/hourly_usp.php",
         "https://www.data.jma.go.jp/obd/stats/etrn/upper/view/hourly_usp.php"]
PAGE_URL = "https://www.data.jma.go.jp/stats/etrn/upper/index.php"
# 観測地点(地点番号:名前)。四国に近い潮岬と、九州の福岡・鹿児島
EMAGRAM_POINTS = [(c.strip(), n.strip()) for c, n in (
    item.split(":") for item in os.getenv(
        "EMAGRAM_POINTS", "47778:潮岬,47807:福岡,47827:鹿児島").split(","))]
RETRY_MINUTES = 30
LOOKBACK = 6  # 最大3日分(9時・21時×3日)さかのぼる
PLOT_SCRIPT = Path(__file__).with_name("emagram_plot.py")

NUM = re.compile(r"^-?\d+(\.\d+)?\)?$")  # 数値、または準正常値「)」付き


def parse_value(text: str) -> float | None:
    s = text.strip().replace("\u3000", "")
    if not NUM.match(s):
        return None  # 欠測・資料不足値・空欄など
    return float(s.rstrip(")"))


def parse_table(table) -> list[dict]:
    headers = [th.get_text("", strip=True) for th in table.find_all("th")]
    keys = []
    for i, h in enumerate(headers):
        key = ("p" if "気圧" in h else "z" if "高度" in h else "t" if "気温" in h
               else "rh" if "湿度" in h else "ws" if "風速" in h else "wd" if "風向" in h else None)
        # 見出しが読めない列は、並び順(気圧・高度・気温・湿度・風速・風向)で判断
        keys.append(key or ["p", "z", "t", "rh", "ws", "wd"][i] if i < 6 else key)
    rows = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        row = {k: parse_value(td.get_text()) for k, td in zip(keys, tds) if k}
        if row.get("p") is not None:
            rows.append(row)
    return rows


def parse_page(html: str) -> tuple[dict | None, list[dict], str]:
    """(地上の値, 指定気圧面の値, 診断メッセージ) を返す。"""
    soup = BeautifulSoup(html, "html.parser")
    t1, t2 = soup.find("table", id="tablefix1"), soup.find("table", id="tablefix2")
    surface = parse_table(t1) if t1 else []
    levels = parse_table(t2) if t2 else []
    n_temp = sum(1 for lv in levels if lv.get("t") is not None)
    if t2 is None:
        ids = [t.get("id") for t in soup.find_all("table")]
        info = f"観測値の表が見つかりません(ページ内の表: {ids or 'なし'})"
    else:
        info = f"表あり:指定気圧面 {len(levels)}層、うち気温あり {n_temp}層"
    return (surface[0] if surface else None), levels, info


def latest_obs_time(now: datetime) -> datetime:
    """直近の観測時刻(9時・21時JST)。観測から1時間以上たったものだけを対象にする。"""
    t = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    while t.hour not in (9, 21):
        t -= timedelta(hours=1)
    return t


class Emagram:
    def __init__(self, session: aiohttp.ClientSession, state: dict, font_path_getter) -> None:
        self.session = session
        self.state = state
        self.get_font = font_path_getter
        self.next_try: dict[str, datetime] = {}

    async def fetch(self, point: str, t: datetime) -> tuple[dict | None, list[dict], str]:
        """すべてのURLを試し、観測値が最も多く取れたものを返す。"""
        query = f"?year={t.year}&month={t.month:02d}&day={t.day:02d}&hour={t.hour}&atm=&point={point}&view="
        best, notes = None, []
        for base in BASES:
            try:
                async with self.session.get(base + query) as r:
                    if r.status != 200:
                        notes.append(f"HTTP {r.status}")
                        continue
                    surface, levels, info = parse_page(await r.text())
                    notes.append(info)
                    if best is None or len(levels) > len(best[1]):
                        best = (surface, levels)
            except aiohttp.ClientError as e:
                notes.append(str(e))
        if best is None:
            raise RuntimeError(f"高層データを取得できません({' / '.join(notes)})")
        return best[0], best[1], " / ".join(notes)

    async def render(self, title: str, surface: dict | None, levels: list[dict]) -> tuple[bytes, dict]:
        font = await self.get_font()
        if font is None:
            raise RuntimeError("フォントを準備できません")
        # 地上の値を最下層として加える(指定気圧面より気圧が高い場合)
        all_levels = list(levels)
        if surface and surface.get("p") and all(surface["p"] > lv["p"] for lv in levels):
            all_levels.append(surface)
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp, "in.json"), Path(tmp, "out.png")
            src.write_text(json.dumps({"title": title, "levels": all_levels}, ensure_ascii=False),
                           encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(PLOT_SCRIPT), str(src), str(dst), str(font),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode != 0 or not dst.exists():
                raise RuntimeError("エマグラムの描画に失敗しました: " + err.decode(errors="ignore")[-300:])
            return dst.read_bytes(), json.loads(out.decode() or "{}")

    def candidates(self, point: str, now: datetime) -> list[datetime]:
        """まだ投稿していない観測時刻を、新しい順に最大 LOOKBACK 回分。
        当日の観測値がまだ公開されていなくても、公開済みの最新分を投稿できるようにする。"""
        posted = self.state.get(f"emagram_{point}")
        t = latest_obs_time(now)
        out = []
        for _ in range(LOOKBACK):
            if posted and t.isoformat() <= posted:
                break
            out.append(t)
            t -= timedelta(hours=12)
        return out

    async def due(self, now: datetime) -> list[tuple[str, str, list[datetime]]]:
        """(地点番号, 地点名, 試す観測時刻の一覧) を返す。"""
        items = []
        for point, name in EMAGRAM_POINTS:
            key = f"emagram_{point}"
            if self.next_try.get(key) and now < self.next_try[key]:
                continue
            times = self.candidates(point, now)
            if times:
                items.append((point, name, times))
        return items

    def postpone(self, point: str, now: datetime) -> None:
        self.next_try[f"emagram_{point}"] = now + timedelta(minutes=RETRY_MINUTES)

    def done(self, point: str, t: datetime) -> None:
        self.state[f"emagram_{point}"] = t.isoformat()
        self.next_try.pop(f"emagram_{point}", None)
