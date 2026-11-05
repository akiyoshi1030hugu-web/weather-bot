"""データ取得元とDiscordチャンネル構成の定義。

ここに書くのは観測・予報機関(一次情報源)のURLだけにする。
新しい図を足すときは PDF_SOURCES に1行追加すればよい。
"""
import os
from dataclasses import dataclass

USER_AGENT = "KochiUniv-WeatherStudyBot/0.1 (personal study use)"


@dataclass(frozen=True)
class Panel:
    """図の中の1面(上下・左右に並んだ図を切り出して、別のチャンネルへ送る)。"""
    row: int            # 0=上段
    col: int            # 0=左
    channel: str
    title: str


@dataclass(frozen=True)
class PdfSource:
    key: str            # 状態保存用のID(一意)
    title: str          # 投稿タイトル
    url: str            # PDF・PNGの公開URL(一次情報源)
    channel: str        # 投稿先チャンネル名(面ごとに分ける場合は、切り出せなかったときの送り先)
    note: str           # 観測・初期時刻などの説明
    page_url: str       # 元ページ(人が開く用)
    pages: int = 1      # 画像化するページ数(0なら全ページ)
    attach_pdf: bool = False  # PDF本体も添付するか
    grid: tuple[int, int] | None = None      # (段数, 列数)。面ごとに分ける場合
    panels: tuple[Panel, ...] = ()           # 主に送る面
    extras: tuple[Panel, ...] = ()           # 主な面と一緒に添付する面(channel は使わない)

    @property
    def is_png(self) -> bool:
        return self.url.lower().endswith((".png", ".gif"))


JMA = "https://www.jma.go.jp"
UPPER_PAGE = f"{JMA}/bosai/numericmap/#type=upper"

PDF_SOURCES = [
    PdfSource(
        key="kaisetsu_tanki",
        title="短期予報解説資料",
        url="https://www.data.jma.go.jp/yoho/data/jishin/kaisetsu_tanki_latest.pdf",
        channel="短期予報解説資料",
        note="気象庁予報部の発表資料",
        page_url="https://www.data.jma.go.jp/yoho/data/jishin/kaisetsu_tanki_latest.pdf",
        pages=0,  # 全ページ
        attach_pdf=True,
    ),
]

# ---------- 高層天気図(解析) ----------
# 気象庁のFAX形式の図は、上空の面ほど上段に置かれる(AUPQ35:上300・下500、AUPQ78:上700・下850)
UPPER_CHARTS = [
    ("aupq35", "AUPQ35 アジア500hPa天気図", "500hpa天気図", (2, 1),
     (Panel(1, 0, "500hpa天気図", "AUPQ35 アジア500hPa天気図"),),
     (Panel(0, 0, "", "AUPQ35 アジア300hPa天気図"),)),
    ("aupq78", "AUPQ78 アジア850hPa天気図", "850hpa天気図", (2, 1),
     (Panel(1, 0, "850hpa天気図", "AUPQ78 アジア850hPa天気図"),),
     (Panel(0, 0, "", "AUPQ78 アジア700hPa天気図"),)),
    ("aupa20", "AUPA20 アジア200hPa天気図", "200-250hpaジェット", None, (), ()),
    ("aupa25", "AUPA25 アジア250hPa天気図", "200-250hpaジェット", None, (), ()),
    ("axfe578", "AXFE578 極東 解析図", "500hpa高度渦度", (2, 1),
     (Panel(0, 0, "500hpa高度渦度", "AXFE578 500hPa高度・渦度 解析"),
      Panel(1, 0, "850hpa気温風-700hpa垂直p速度", "AXFE578 850hPa気温・風、700hPa鉛直p速度 解析")), ()),
]
for code, title, channel, grid, panels, extras in UPPER_CHARTS:
    for hh, jst in (("00", "9"), ("12", "21")):
        PDF_SOURCES.append(PdfSource(
            key=f"{code}_{hh}",
            title=title,
            url=f"{JMA}/bosai/numericmap/data/nwpmap/{code}_{hh}.pdf",
            channel=channel,
            note=f"{hh}UTC(日本時間{jst}時)観測",
            page_url=UPPER_PAGE,
            grid=grid, panels=panels, extras=extras,
        ))

# ---------- 週間・季節予報の解説資料 ----------
PDF_SOURCES.append(PdfSource(
    key="kaisetsu_shukan",
    title="週間天気予報解説資料",
    url="https://www.data.jma.go.jp/yoho/data/jishin/kaisetsu_shukan_latest.pdf",
    channel="週間予報解説資料",
    note="毎日10時ごろ発表",
    page_url="https://www.data.jma.go.jp/yoho/data/jishin/kaisetsu_shukan_latest.pdf",
    pages=0,
))
# 地方ごとの季節予報解説(地方コードは気象庁の予報区コード)
SEASON_AREAS = [(c.strip(), n.strip()) for c, n in (
    item.split(":") for item in os.getenv(
        "SEASON_AREAS", "010800:四国地方,010900:九州北部地方").split(","))]
for term, label, when in (("P1M", "1か月予報", "毎週木曜14時30分"),
                          ("P3M", "3か月予報", "毎月25日ごろ")):
    for code, name in SEASON_AREAS:
        PDF_SOURCES.append(PdfSource(
            key=f"season_{term}_{code}",
            title=f"{label}の解説({name})",
            url=f"{JMA}/bosai/season/data/pdf/{term}/{code}.pdf",
            channel="季節予報",
            note=f"向こう{label[:-2]}の気温・降水量などの見通し({when}発表)",
            page_url=f"{JMA}/bosai/season/",
            pages=0,
        ))

# ---------- 数値予報天気図(気象庁 数値予報天気図ページで公開されているPDF) ----------
# 内容の説明は気象庁「配信資料に関する技術情報」の図の定義にもとづく
NWP_PAGE = f"{JMA}/bosai/numericmap/#type=nwp"


# 2×2 の図は、左が12時間予想・右が24時間予想、上段が上空の面
NWP_CHARTS = [
    ("fxfe502", "FXFE502 極東 12・24時間予想図", "500hPa高度・渦度/地上気圧・降水量・風", "500hpa高度渦度-24時間予想",
     (2, 2), (Panel(0, 1, "500hpa高度渦度-24時間予想", "FXFE502 500hPa高度・渦度 24時間予想図"),
              Panel(1, 1, "地上気圧降水量風-24時間予想", "FXFE502 地上気圧・降水量・風 24時間予想図"))),
    ("fxfe5782", "FXFE5782 極東 12・24時間予想図", "500hPa気温・700hPa湿数/850hPa気温・風・700hPa鉛直p速度",
     "500hpa気温-700hpa湿数-24時間予想",
     (2, 2), (Panel(0, 1, "500hpa気温-700hpa湿数-24時間予想", "FXFE5782 500hPa気温・700hPa湿数 24時間予想図"),
              Panel(1, 1, "850hpa気温風-700hpa垂直p速度-24時間予想",
                    "FXFE5782 850hPa気温・風、700hPa鉛直p速度 24時間予想図"))),
    ("fxfe504", "FXFE504 極東 36・48時間予想図", "500hPa高度・渦度/地上気圧・降水量・風", "数値予報-36-72時間予想", None, ()),
    ("fxfe507", "FXFE507 極東 72時間予想図", "500hPa高度・渦度/地上気圧・降水量・風", "数値予報-36-72時間予想", None, ()),
    ("fxfe5784", "FXFE5784 極東 36・48時間予想図", "500hPa気温・700hPa湿数/850hPa気温・風・700hPa鉛直p速度",
     "数値予報-36-72時間予想", None, ()),
    ("fxfe577", "FXFE577 極東 72時間予想図", "500hPa気温・700hPa湿数/850hPa気温・風・700hPa鉛直p速度",
     "数値予報-36-72時間予想", None, ()),
    ("fxjp854", "FXJP854 日本 12〜48時間予想図", "850hPa風・相当温位(12・24・36・48時間後)", "850hpa相当温位", None, ()),
]
for code, title, desc, channel, grid, panels in NWP_CHARTS:
    for hh, jst in (("00", "9"), ("12", "21")):
        PDF_SOURCES.append(PdfSource(
            key=f"{code}_{hh}",
            title=title,
            url=f"{JMA}/bosai/numericmap/data/nwpmap/{code}_{hh}.pdf",
            channel=channel,
            note=f"{desc}\n初期時刻 {hh}UTC(日本時間{jst}時)",
            page_url=NWP_PAGE,
            grid=grid, panels=panels,
        ))

# ---------- アンサンブル予報(気象庁) ----------
LONGFCST = "https://www.data.jma.go.jp/cpd/data/longfcst/fax"
ENSEMBLE_CHARTS = [
    ("fefe19", "FEFE19 週間アンサンブル予想図", f"{JMA}/bosai/numericmap/data/nwpmap/fefe19.png",
     "全球アンサンブル予報:500hPa高度・850hPa気温など(毎日12UTC初期値)"),
    ("fzcx50", "FZCX50 週間予報支援図(アンサンブル)", f"{JMA}/bosai/numericmap/data/nwpmap/fzcx50.png",
     "全球アンサンブル予報:アンサンブル平均・スプレッドなど(毎日12UTC初期値)"),
    ("fxxn519", "FXXN519 週間予報支援図", f"{JMA}/bosai/numericmap/data/nwpmap/fxxn519.png",
     "週間予報の支援図(毎日12UTC初期値)"),
    ("fcvx22", "FCVX22 2週間 北半球予想図・高偏差確率", f"{LONGFCST}/fcvx22_12.png", "2週間アンサンブル予報"),
    ("fcvx12", "FCVX12 1か月 北半球予想図", f"{LONGFCST}/fcvx12_12.png", "1か月アンサンブル予報"),
    ("fcvx13", "FCVX13 1か月 スプレッド・高偏差確率", f"{LONGFCST}/fcvx13_12.png", "1か月アンサンブル予報"),
]
for code, title, url, desc in ENSEMBLE_CHARTS:
    PDF_SOURCES.append(PdfSource(key=f"ens_{code}", title=title, url=url, channel="アンサンブル予報",
                                 note=desc, page_url=f"{JMA}/bosai/numericmap/"))


@dataclass(frozen=True)
class AsasSource:
    key: str = "asas"
    title: str = "地上天気図(日本周辺)"
    list_url: str = f"{JMA}/bosai/weather_map/data/list.json"
    image_base: str = f"{JMA}/bosai/weather_map/data/png/"
    channel: str = "地上天気図"
    page_url: str = f"{JMA}/bosai/weather_map/"
    list_key: str = "now"   # 天気図一覧(list.json)の中のどの種類か
    note: str = ""
    region: str = "near"    # near=日本周辺、asia=アジア太平洋(気象庁の天気図一覧の名前)


ASAS = AsasSource()
WEATHER_MAPS = [
    ASAS,
    AsasSource(key="asas_far", title="太平洋地上天気図(アジア太平洋)", channel="太平洋地上天気図", region="asia"),
    AsasSource(key="fsas24", title="地上予報天気図 24時間予想(日本周辺)", channel="地上予報天気図",
               list_key="ft24"),
    AsasSource(key="fsas48", title="地上予報天気図 48時間予想(日本周辺)", channel="地上予報天気図",
               list_key="ft48"),
    AsasSource(key="fsas24_far", title="地上予報天気図 24時間予想(アジア太平洋)", channel="地上予報天気図",
               list_key="ft24", region="asia"),
    AsasSource(key="fsas48_far", title="地上予報天気図 48時間予想(アジア太平洋)", channel="地上予報天気図",
               list_key="ft48", region="asia"),
]

# /setup_weather で作成するカテゴリーとチャンネル
CHANNEL_LAYOUT = [
    ("📋 解説資料", ["短期予報解説資料", "週間予報解説資料", "季節予報", "links"]),
    ("🗺️ 地上天気図", ["太平洋地上天気図", "地上天気図", "アメダス天気図", "地上予報天気図", "月平均天気図"]),
    ("🎈 高層天気図", ["850hpa気温風-700hpa垂直p速度", "850hpa天気図", "500hpa天気図", "500hpa高度渦度",
                    "200-250hpaジェット", "ウィンドプロファイラ"]),
    ("📈 数値予報天気図", ["500hpa高度渦度-24時間予想", "地上気圧降水量風-24時間予想", "500hpa気温-700hpa湿数-24時間予想",
                      "850hpa気温風-700hpa垂直p速度-24時間予想", "850hpa相当温位", "ssi分布", "数値予報-36-72時間予想",
                      "アンサンブル予報"]),
    ("🛰️ 衛星", ["赤外-ir", "赤外-i4", "水蒸気-wv", "水蒸気-w2", "水蒸気-w3", "可視-vs", "差分-s1", "差分-s2"]),
    ("📡 レーダー", ["レーダーエコー合成図", "解析雨量", "ドップラーレーダー", "雷ナウキャスト"]),
    ("🌊 海洋", ["沿岸波浪実況図"]),
    ("🌀 台風・事例", ["台風", "事例アーカイブ"]),
    ("📚 過去データ", ["過去データ"]),
    ("⚠️ 防災", ["警報注意報"]),
]

# 以前の版のチャンネル → 新しい名前(/setup_weather で名前を変えるので、9月からの投稿はそのまま残る)
CHANNEL_RENAMES = {
    "地上実況-asas": "地上天気図",
    "地上予想図": "地上予報天気図",
    "初期値解析": "850hpa気温風-700hpa垂直p速度",
    "850-700hpa解析": "850hpa天気図",
    "500-300hpa解析": "500hpa天気図",
    "500hpa高度渦度": "500hpa高度渦度-24時間予想",
    "850-700hpa気温湿数": "500hpa気温-700hpa湿数-24時間予想",
    "ひまわり-赤外": "赤外-ir",
    "ひまわり-水蒸気": "水蒸気-wv",
    "ひまわり-短波赤外b07": "赤外-i4",
    "ひまわり-差分画像": "差分-s1",
    "気象レーダー": "レーダーエコー合成図",
    "竜巻発生確度ナウキャスト": "ドップラーレーダー",
}
