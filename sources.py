"""データ取得元とDiscordチャンネル構成の定義。

ここに書くのは観測・予報機関(一次情報源)のURLだけにする。
新しい図を足すときは PDF_SOURCES に1行追加すればよい。
"""
import os
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
    hint: str = ""          # 見るポイント


JMA = "https://www.jma.go.jp"
UPPER_PAGE = f"{JMA}/bosai/numericmap/#type=upper"

PDF_SOURCES = [
    PdfSource(
        key="kaisetsu_tanki",
        title="短期予報解説資料",
        url="https://www.data.jma.go.jp/yoho/data/jishin/kaisetsu_tanki_latest.pdf",
        channel="短期予報解説資料",
        note="予報官による実況解析と予報の着目点",
        page_url="https://www.data.jma.go.jp/yoho/data/jishin/kaisetsu_tanki_latest.pdf",
        pages=0,  # 全ページ
        attach_pdf=True,
    ),
]

# ---------- 高層天気図(解析) ----------
HINT_UP_500 = ("500hPa:トラフ・リッジの位置と寒気(-30℃以下は大雪・雷の目安)。5880m線は太平洋高気圧の縁の目安。"
               "300hPa:等風速線でジェット気流の位置と強さを確認")
HINT_UP_850 = ("850hPa:等温線と風の交わり方で暖気・寒気の移流を読む。"
               "700hPa:湿数3℃以下の湿った領域が雲の広がりの目安")
HINT_UP_JET = "ジェット気流(強風軸)の位置と蛇行。ジェットの南側・入口と出口付近は擾乱の発達に関係する"
UPPER_CHARTS = [
    # (コード, タイトル, 見るポイント, 投稿先チャンネル)
    ("aupq35", "AUPQ35 アジア500hPa・300hPa天気図", HINT_UP_500, "500-300hpa解析"),
    ("aupq78", "AUPQ78 アジア850hPa・700hPa天気図", HINT_UP_850, "850-700hpa解析"),
    ("aupa20", "AUPA20 アジア200hPa天気図", HINT_UP_JET, "200-250hpaジェット"),
    ("aupa25", "AUPA25 アジア250hPa天気図", HINT_UP_JET, "200-250hpaジェット"),
]
for code, title, hint, channel in UPPER_CHARTS:
    for hh, jst in (("00", "9"), ("12", "21")):
        PDF_SOURCES.append(PdfSource(
            key=f"{code}_{hh}",
            title=title,
            url=f"{JMA}/bosai/numericmap/data/nwpmap/{code}_{hh}.pdf",
            channel=channel,
            note=f"{hh}UTC(日本時間{jst}時)観測",
            page_url=UPPER_PAGE,
            hint=hint,
        ))

# ---------- 週間・季節予報の解説資料 ----------
PDF_SOURCES.append(PdfSource(
    key="kaisetsu_shukan",
    title="週間天気予報解説資料",
    url="https://www.data.jma.go.jp/yoho/data/jishin/kaisetsu_shukan_latest.pdf",
    channel="週間予報解説資料",
    note="3〜7日先の予報の根拠と、主要なじょう乱の見通し(毎日10時ごろ)",
    page_url="https://www.data.jma.go.jp/yoho/data/jishin/kaisetsu_shukan_latest.pdf",
    pages=0,
    hint="予報期間中の主要じょう乱(低気圧・前線・台風)の動きと、予報の信頼度が低い日を確認",
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

# 見るポイント(図ごとの着目点)
HINT_Z500 = ("正渦度の極大域(トラフ)がどこへ動くか。その前面(東側)で地上低気圧が発達しやすい。"
             "等高線の谷と地上低気圧の位置関係(西に傾いていれば発達傾向)も確認")
HINT_T850 = ("700hPa湿数3℃以下の湿潤域と上昇流域の重なり=雲・降水域。"
             "冬は850hPa -6℃(平地で雪の目安)と500hPa -30℃(大雪の目安)の線に注目")
HINT_EPT = ("相当温位の等値線が混み合う所=前線帯。高相当温位の空気が南から流れ込む所は大雨に注意")
HINT_ANAL = "予想図を見る前の出発点。実際の大気の状態(トラフ・暖気・寒気・上昇流)を把握する"

# (コード, タイトル, 内容, 見るポイント, 投稿先チャンネル)
NWP_CHARTS = [
    ("axfe578", "AXFE578 極東 解析図",
     "500hPa高度・渦度/850hPa気温・風/700hPa鉛直流(初期値の解析)", HINT_ANAL, "初期値解析"),
    ("fxfe502", "FXFE502 極東 12・24時間予想図",
     "500hPa高度・渦度/地上気圧・降水量・海上風", HINT_Z500, "500hpa高度渦度"),
    ("fxfe504", "FXFE504 極東 36・48時間予想図",
     "500hPa高度・渦度/地上気圧・降水量・海上風", HINT_Z500, "500hpa高度渦度"),
    ("fxfe507", "FXFE507 極東 72時間予想図",
     "500hPa高度・渦度/地上気圧・降水量・海上風", HINT_Z500, "500hpa高度渦度"),
    ("fxfe5782", "FXFE5782 極東 12・24時間予想図",
     "500hPa気温/700hPa湿数/850hPa気温・風/700hPa鉛直流", HINT_T850, "850-700hpa気温湿数"),
    ("fxfe5784", "FXFE5784 極東 36・48時間予想図",
     "500hPa気温/700hPa湿数/850hPa気温・風/700hPa鉛直流", HINT_T850, "850-700hpa気温湿数"),
    ("fxfe577", "FXFE577 極東 72時間予想図",
     "500hPa気温/700hPa湿数/850hPa気温・風/700hPa鉛直流", HINT_T850, "850-700hpa気温湿数"),
    ("fxjp854", "FXJP854 日本 12〜48時間予想図",
     "850hPa風・相当温位(12・24・36・48時間後)", HINT_EPT, "850hpa相当温位"),
]
for code, title, desc, hint, channel in NWP_CHARTS:
    for hh, jst in (("00", "9"), ("12", "21")):
        PDF_SOURCES.append(PdfSource(
            key=f"{code}_{hh}",
            title=title,
            url=f"{JMA}/bosai/numericmap/data/nwpmap/{code}_{hh}.pdf",
            channel=channel,
            note=f"{desc}\n{'解析' if code.startswith('a') else '初期'}時刻 {hh}UTC(日本時間{jst}時)",
            page_url=NWP_PAGE,
            hint=hint,
        ))


@dataclass(frozen=True)
class AsasSource:
    key: str = "asas"
    title: str = "地上実況天気図(ASAS)"
    list_url: str = f"{JMA}/bosai/weather_map/data/list.json"
    image_base: str = f"{JMA}/bosai/weather_map/data/png/"
    channel: str = "地上実況-asas"
    page_url: str = f"{JMA}/bosai/weather_map/"
    list_key: str = "now"   # 天気図一覧(list.json)の中のどの種類か
    note: str = ""
    hint: str = ""


HINT_SFC = "実況図(ASAS)と比べて、低気圧・前線がどこへ動き、発達するか。等圧線の間隔(風の強さ)も確認"

ASAS = AsasSource()
WEATHER_MAPS = [
    ASAS,
    AsasSource(key="fsas24", title="FSAS24 24時間予想図", channel="地上予想図",
               list_key="ft24", note="地上気圧配置と前線の24時間後の予想", hint=HINT_SFC),
    AsasSource(key="fsas48", title="FSAS48 48時間予想図", channel="地上予想図",
               list_key="ft48", note="地上気圧配置と前線の48時間後の予想", hint=HINT_SFC),
]

# /setup_weather で作成するカテゴリーとチャンネル
CHANNEL_LAYOUT = [
    ("📋 今日の予報", ["短期予報解説資料", "予報ノート", "links"]),
    ("🗺️ 天気図(実況)", ["地上実況-asas", "850-700hpa解析", "500-300hpa解析", "200-250hpaジェット"]),
    ("🎈 エマグラム", ["エマグラム-全国まとめ", "エマグラム-北日本", "エマグラム-東日本", "エマグラム-西日本",
                    "エマグラム-南西諸島", "エマグラム-小笠原"]),
    ("📈 数値予報", ["初期値解析", "地上予想図", "500hpa高度渦度", "850-700hpa気温湿数", "850hpa相当温位", "予報の答え合わせ"]),
    ("🛰️ 衛星・レーダー", ["ひまわり-赤外", "ひまわり-水蒸気", "気象レーダー", "解析雨量"]),
    ("📅 週間・季節予報", ["週間予報解説資料", "季節予報"]),
    ("📊 観測データ", ["アメダス"]),
    ("📚 事例研究", ["事例アーカイブ"]),
    ("⚠️ 防災", ["警報注意報", "台風"]),
]
