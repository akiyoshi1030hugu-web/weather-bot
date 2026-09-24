"""データ取得元とDiscordチャンネル構成の定義。

ここに書くのは観測・予報機関(一次情報源)のURLだけにする。
新しい図を足すときは PDF_SOURCES に1行追加すればよい。
"""
from dataclasses import dataclass

USER_AGENT = "KochiUniv-WeatherStudyBot/0.1 (personal study use)"


@dataclass(frozen=True)
class PdfSource:
    key: str            # 状態保存用のID(一意)
    title: str          # 投稿タイトル
    url: str            # PDFの公開URL(一次情報源)
    channel: str        # 投稿先チャンネル名
    note: str           # 観測・初期時刻などの説明
    page_url: str       # 元ページ(人が開く用)
    pages: int = 1      # 画像化するページ数(0なら全ページ)
    attach_pdf: bool = False  # PDF本体も添付するか


JMA = "https://www.jma.go.jp"
UPPER_PAGE = f"{JMA}/bosai/numericmap/#type=upper"

PDF_SOURCES = [
    PdfSource(
        key="kaisetsu_tanki",
        title="短期予報解説資料",
        url="https://www.data.jma.go.jp/fcd/yoho/data/jishin/kaisetsu_tanki_latest.pdf",
        channel="短期予報解説資料",
        note="予報官による実況解析と予報の着目点",
        page_url="https://www.data.jma.go.jp/fcd/yoho/data/jishin/kaisetsu_tanki_latest.pdf",
        pages=0,  # 全ページ
        attach_pdf=True,
    ),
    PdfSource(
        key="aupq35_00",
        title="AUPQ35 アジア500hPa・300hPa天気図",
        url=f"{JMA}/bosai/numericmap/data/nwpmap/aupq35_00.pdf",
        channel="高層天気図",
        note="00UTC(日本時間9時)観測",
        page_url=UPPER_PAGE,
    ),
    PdfSource(
        key="aupq35_12",
        title="AUPQ35 アジア500hPa・300hPa天気図",
        url=f"{JMA}/bosai/numericmap/data/nwpmap/aupq35_12.pdf",
        channel="高層天気図",
        note="12UTC(日本時間21時)観測",
        page_url=UPPER_PAGE,
    ),
    PdfSource(
        key="aupq78_00",
        title="AUPQ78 アジア850hPa・700hPa天気図",
        url=f"{JMA}/bosai/numericmap/data/nwpmap/aupq78_00.pdf",
        channel="高層天気図",
        note="00UTC(日本時間9時)観測",
        page_url=UPPER_PAGE,
    ),
    PdfSource(
        key="aupq78_12",
        title="AUPQ78 アジア850hPa・700hPa天気図",
        url=f"{JMA}/bosai/numericmap/data/nwpmap/aupq78_12.pdf",
        channel="高層天気図",
        note="12UTC(日本時間21時)観測",
        page_url=UPPER_PAGE,
    ),
]


@dataclass(frozen=True)
class AsasSource:
    key: str = "asas"
    title: str = "地上実況天気図(ASAS)"
    list_url: str = f"{JMA}/bosai/weather_map/data/list.json"
    image_base: str = f"{JMA}/bosai/weather_map/data/png/"
    channel: str = "地上実況-asas"
    page_url: str = f"{JMA}/bosai/weather_map/"


ASAS = AsasSource()

# /setup_weather で作成するカテゴリーとチャンネル
CHANNEL_LAYOUT = [
    ("📋 今日の予報", ["短期予報解説資料", "予報ノート", "links"]),
    ("🗺️ 天気図", ["地上実況-asas", "高層天気図", "エマグラム", "数値予報天気図"]),
    ("🛰️ 衛星・レーダー", ["ひまわり-赤外", "ひまわり-水蒸気", "気象レーダー", "解析雨量"]),
    ("📊 観測データ", ["アメダス"]),
    ("⚠️ 防災", ["警報注意報", "台風", "地震"]),
]
