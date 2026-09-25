"""事例アーカイブ:大雨監視・台風監視モードの間に投稿された図を、1つのスレッドにまとめる。

図そのものは再送せず、元の投稿へのリンクを並べる(通信量を増やさないため)。
"""
from datetime import datetime

ARCHIVE_CHANNEL = "事例アーカイブ"

# 事例の種類ごとに、スレッドへ集めるチャンネル
ARCHIVE_SOURCES = {
    "大雨監視": {"短期予報解説資料", "地上実況-asas", "気象レーダー", "解析雨量", "ひまわり-赤外",
             "ひまわり-水蒸気", "850hpa相当温位", "850-700hpa解析", "アメダス",
             "エマグラム-西日本", "エマグラム-全国まとめ", "予報の答え合わせ"},
    "台風監視": {"短期予報解説資料", "地上実況-asas", "ひまわり-赤外", "ひまわり-水蒸気", "気象レーダー",
             "地上予想図", "500-300hpa解析", "エマグラム-南西諸島", "エマグラム-全国まとめ", "台風"},
}
ARCHIVE_SOURCES["手動"] = set().union(*ARCHIVE_SOURCES.values())

REVIEW = """**事例を振り返りましょう**(このスレッドに書き込んでください)
1. 数値予報は、この現象をいつ・どの程度予想できていたか(#予報の答え合わせ も参照)
2. 降水が強まった場所と時間に、850hPaの相当温位や下層の風はどう対応していたか
3. エマグラムで大気の不安定さ(SSI・CAPE)や湿り具合はどう変化したか
4. 短期予報解説資料で予報官が注目していた点と、実際の経過の違い
5. 次に似た状況になったら、何を早めに確認するか"""


class CaseArchive:
    def __init__(self, state: dict) -> None:
        self.state = state

    @property
    def event(self) -> dict | None:
        return self.state.get("case_event")

    def start(self, kind: str, reason: str, thread_id: int, head_id: int, now: datetime) -> None:
        self.state["case_event"] = {"kinds": [kind], "reasons": [reason] if reason else [],
                                    "thread_id": thread_id, "head_id": head_id,
                                    "start": now.isoformat(timespec="minutes"), "count": 0}

    def add_kind(self, kind: str) -> bool:
        ev = self.event
        if ev and kind not in ev["kinds"]:
            ev["kinds"].append(kind)
            return True
        return False

    def add_reason(self, reason: str) -> bool:
        ev = self.event
        if ev and reason and reason not in ev["reasons"]:
            ev["reasons"].append(reason)
            del ev["reasons"][:-30]
            return True
        return False

    def wants(self, channel_name: str) -> bool:
        ev = self.event
        return bool(ev) and any(channel_name in ARCHIVE_SOURCES.get(k, set()) for k in ev["kinds"])

    def end(self, now: datetime) -> dict | None:
        ev = self.state.pop("case_event", None)
        if ev:
            ev["end"] = now.isoformat(timespec="minutes")
            history = self.state.setdefault("case_history", [])
            history.append({k: ev[k] for k in ("kinds", "start", "end", "thread_id", "count")})
            del history[:-50]
        return ev
