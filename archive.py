"""事例の記録:台風は1つずつ、大雨は1回ずつ、それぞれ専用のスレッドに記録する。

- 台風:気象庁が台風・熱帯低気圧の情報を出し始めたら #台風 にその台風のスレッドを作り、
  発表のたびに実況・予報の値と進路図を投稿する。関係するチャンネルの投稿へのリンクも集める。
- 大雨:大雨監視モードの間、#事例アーカイブ にスレッドを作り、投稿へのリンクと観測値を集める。
- 手動:/case_start〜/case_end(前線の通過など)。
図そのものは再送せず、元の投稿へのリンクを並べる(通信量を増やさないため)。
"""
from datetime import datetime

ARCHIVE_CHANNEL = "事例アーカイブ"
TYPHOON_CHANNEL = "台風"

# 事例の種類ごとに、スレッドへリンクを集めるチャンネル
SOURCES = {
    "typhoon": {"短期予報解説資料", "地上天気図", "太平洋地上天気図", "赤外-ir", "赤外-i4", "水蒸気-wv", "可視-vs",
                "差分-s1", "差分-s2", "レーダーエコー合成図", "解析雨量", "地上予報天気図", "500hpa天気図",
                "沿岸波浪実況図", "アンサンブル予報"},
    "rain": {"短期予報解説資料", "地上天気図", "アメダス天気図", "レーダーエコー合成図", "解析雨量", "ドップラーレーダー",
             "雷ナウキャスト", "赤外-ir", "赤外-i4", "水蒸気-wv", "水蒸気-w2", "水蒸気-w3", "可視-vs", "差分-s1",
             "差分-s2", "850hpa相当温位", "850hpa天気図", "850hpa気温風-700hpa垂直p速度", "500hpa天気図",
             "ウィンドプロファイラ"},
}
SOURCES["manual"] = set().union(*SOURCES.values())


class Events:
    """記録中の事例(複数を同時に扱う)。state["events"] に保存する。"""

    def __init__(self, state: dict) -> None:
        self.state = state
        old = state.pop("case_event", None)            # 以前の形式(1件だけ)からの移行
        if old and "thread_id" in old:
            kind = "manual" if "手動" in old.get("kinds", []) else "rain"
            state.setdefault("events", {})[kind] = {
                "kind": kind, "title": "", "thread_id": old["thread_id"], "head_id": old.get("head_id"),
                "start": old.get("start"), "count": old.get("count", 0), "reasons": old.get("reasons", [])}

    @property
    def active(self) -> dict:
        return self.state.setdefault("events", {})

    def get(self, key: str) -> dict | None:
        return self.active.get(key)

    def start(self, key: str, kind: str, title: str, thread_id: int, head_id: int, now: datetime,
              reason: str = "") -> dict:
        ev = {"kind": kind, "title": title, "thread_id": thread_id, "head_id": head_id,
              "start": now.isoformat(timespec="minutes"), "count": 0, "reasons": [reason] if reason else []}
        self.active[key] = ev
        return ev

    def add_reason(self, key: str, reason: str) -> bool:
        ev = self.get(key)
        if ev and reason and reason not in ev["reasons"]:
            ev["reasons"].append(reason)
            del ev["reasons"][:-30]
            return True
        return False

    def targets(self, channel_name: str) -> list[dict]:
        """この投稿のリンクを集める、記録中の事例。"""
        return [ev for ev in self.active.values() if channel_name in SOURCES.get(ev["kind"], set())]

    def end(self, key: str, now: datetime) -> dict | None:
        ev = self.active.pop(key, None)
        if ev:
            ev["end"] = now.isoformat(timespec="minutes")
            history = self.state.setdefault("case_history", [])
            history.append({k: ev.get(k) for k in ("kind", "title", "start", "end", "thread_id", "count")})
            del history[:-100]
        return ev
