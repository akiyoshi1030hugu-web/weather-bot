"""月平均天気図と沿岸波浪実況図(気象庁)。

月平均天気図:気象庁「気候系監視速報」の図(毎月10日ごろ、前の月の分が公開される)
  https://www.data.jma.go.jp/cpd/diag/sokuho/<YYYYMM>/fig13.gif など
  図13 500hPa高度・平年偏差 / 図15 海面気圧・平年偏差 / 図16 850hPa気温・平年偏差

沿岸波浪実況図:気象庁「波浪図」のPDF
  https://www.data.jma.go.jp/waveinf/data/chart/awjp<YYMMDDHH(UTC)>.pdf
  (例:awjp26110500.pdf = 2026年11月5日00UTC)。新しい時刻から順に、ある時刻を探す。
"""
import os
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
SOKUHO = "https://www.data.jma.go.jp/cpd/diag/sokuho"
MONTHLY_FIGS = [
    ("fig13.gif", "500hPa高度・平年偏差(月平均)"),
    ("fig15.gif", "海面気圧・平年偏差(月平均)"),
    ("fig16.gif", "850hPa気温・平年偏差(月平均)"),
]
WAVE_PAGE = "https://www.data.jma.go.jp/waveinf/chart/awjp.html"
WAVE_PDF = "https://www.data.jma.go.jp/waveinf/data/chart/awjp{t:%y%m%d%H}.pdf"
WAVE_HOURS = [int(h) for h in os.getenv("WAVE_HOURS", "0,6,12,18").split(",") if h.strip()]


def wave_candidates(now: datetime, back_hours: int = 30) -> list[datetime]:
    """新しい順の、沿岸波浪実況図の時刻(UTC)。"""
    t = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    for _ in range(back_hours + 1):
        if t.hour in WAVE_HOURS:
            out.append(t)
        t -= timedelta(hours=1)
    return out


async def wave_latest(session, now: datetime, known: datetime | None = None) -> tuple[datetime, str] | None:
    """公開されている、いちばん新しい図 (時刻, URL)。known より新しいものが無ければ known のまま返す。"""
    for t in wave_candidates(now):
        if known is not None and t <= known:
            return known, WAVE_PDF.format(t=known)
        url = WAVE_PDF.format(t=t)
        async with session.head(url, allow_redirects=True) as r:
            if r.status == 200:
                return t, url
    return None
