"""月平均天気図と沿岸波浪実況図(気象庁)。

月平均天気図:気象庁「気候系監視速報」の図(毎月10日ごろ、前の月の分が公開される)
  https://www.data.jma.go.jp/cpd/diag/sokuho/<YYYYMM>/fig13.gif など
  図13 500hPa高度・平年偏差 / 図15 海面気圧・平年偏差 / 図16 850hPa気温・平年偏差

沿岸波浪実況図:気象庁「波浪図」のページ(awjp.html)が読み込んでいる画像を、ページと
  スクリプトから探す。見つからないときや変わったときは、WAVE_IMAGE_URL に画像のURLを直接書けば使える
  ({YYYYMMDDHH} などは書かない、いつも最新の図を指すURL)。
"""
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

JST = timezone(timedelta(hours=9))
SOKUHO = "https://www.data.jma.go.jp/cpd/diag/sokuho"
MONTHLY_FIGS = [
    ("fig13.gif", "500hPa高度・平年偏差(月平均)"),
    ("fig15.gif", "海面気圧・平年偏差(月平均)"),
    ("fig16.gif", "850hPa気温・平年偏差(月平均)"),
]
WAVE_PAGE = "https://www.data.jma.go.jp/waveinf/chart/awjp.html"
WAVE_IMAGE_URL = os.getenv("WAVE_IMAGE_URL", "").strip()


def last_month(now: datetime) -> str:
    first = now.astimezone(JST).replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y%m")


def monthly_urls(ym: str) -> list[tuple[str, str]]:
    return [(f"{SOKUHO}/{ym}/{name}", title) for name, title in MONTHLY_FIGS]


IMG_RE = re.compile(r"""["']([^"'\s]*?(?:awjp|AWJP)[^"'\s]*?\.(?:png|gif|jpg))["']""")
SCRIPT_RE = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)


def find_wave_images(html: str, scripts: dict[str, str], base: str) -> list[str]:
    """ページとスクリプトの中から、沿岸波浪実況図の画像らしいURLを探す(新しそうな順)。"""
    found = []
    for text in [html] + list(scripts.values()):   # スクリプトが入れる画像の場所は、ページから見た位置
        where = base
        for m in IMG_RE.findall(text):
            if "{" in m or "+" in m:
                continue
            found.append(urljoin(where, m))
    stamp = lambda u: max(re.findall(r"\d{8,14}", u) or [""])  # noqa: E731
    return sorted(dict.fromkeys(found), key=stamp, reverse=True)


async def wave_image(session) -> tuple[str, bytes] | None:
    if WAVE_IMAGE_URL:
        urls = [WAVE_IMAGE_URL]
    else:
        async with session.get(WAVE_PAGE) as r:
            r.raise_for_status()
            html = await r.text()
        scripts = {}
        for src in SCRIPT_RE.findall(html)[:8]:
            u = urljoin(WAVE_PAGE, src)
            try:
                async with session.get(u) as r:
                    if r.status == 200:
                        scripts[u] = await r.text()
            except Exception:
                pass
        urls = find_wave_images(html, scripts, WAVE_PAGE)
    for u in urls[:3]:
        async with session.get(u) as r:
            if r.status == 200 and r.headers.get("Content-Type", "").startswith("image"):
                return u, await r.read()
    return None
