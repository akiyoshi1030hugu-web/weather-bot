"""過去データの選択画面(#過去データ に置く、種類ごとの embed+選択メニュー)。

1. 種類ごとの embed のメニューから図を選ぶ → 2. 日付と時刻(日本時間)を入れる
→ 3. その時刻にいちばん近いデータを表示する。探す順番は
   ① このBotがこれまでに投稿した図(9月からの投稿もチャンネルにそのまま残っているので、そこから探す)
   ② 衛星画像は、気象庁のひまわりの観測データ(NOAAで公開、ひまわり8号の2015年7月から)から作る
   ③ 地上天気図は、気象庁の「保存用天気図」の月ごとのページを案内する
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

JST = timezone(timedelta(hours=9))
TIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}) JST")
UTC_RE = re.compile(r"(00|12)UTC")


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    channel: str
    tolerance_h: float      # この時間より離れた投稿は「その時刻のデータ」とみなさない
    hsd: str | None = None  # ひまわりの観測データから作れる場合のバンド式("B13" "B13-B15" など)


CATEGORIES = [
    ("🗺️ 地上天気図", 0x2F855A, [
        Item("pac", "太平洋地上天気図", "太平洋地上天気図", 4),
        Item("sfc", "地上天気図", "地上天気図", 4),
        Item("amd", "アメダス天気図", "アメダス天気図", 2),
        Item("sfcfc", "地上予報天気図", "地上予報天気図", 13),
        Item("mon", "月平均天気図", "月平均天気図", 24 * 40),
    ]),
    ("🎈 高層天気図", 0x2B6CB0, [
        Item("t850w", "850hPa気温・風/700hPa垂直p速度", "850hpa気温風-700hpa垂直p速度", 7),
        Item("u850", "850hPa天気図", "850hpa天気図", 7),
        Item("u500", "500hPa天気図", "500hpa天気図", 7),
        Item("z500", "500hPa高度・渦度", "500hpa高度渦度", 7),
        Item("jet", "200・250hPa天気図", "200-250hpaジェット", 7),
        Item("wpr", "ウィンドプロファイラ", "ウィンドプロファイラ", 2),
    ]),
    ("📈 数値予報天気図", 0x805AD5, [
        Item("f500", "500hPa高度・渦度 24時間予想図", "500hpa高度渦度-24時間予想", 7),
        Item("fsfc", "地上気圧・降水量・風 24時間予想図", "地上気圧降水量風-24時間予想", 7),
        Item("f700", "500hPa気温・700hPa湿数 24時間予想図", "500hpa気温-700hpa湿数-24時間予想", 7),
        Item("f850", "850hPa気温・風/700hPa垂直p速度 24時間予想図", "850hpa気温風-700hpa垂直p速度-24時間予想", 7),
        Item("fept", "850hPa相当温位 予想図", "850hpa相当温位", 7),
        Item("ssi", "SSI分布(MSMから計算)", "ssi分布", 4),
        Item("f72", "36〜72時間予想図", "数値予報-36-72時間予想", 7),
        Item("ens", "アンサンブル予報(気象庁)", "アンサンブル予報", 26),
    ]),
    ("🛰️ 衛星画像", 0x553C9A, [
        Item("ir", "IR 赤外1(B13)", "赤外-ir", 1, "B13"),
        Item("i4", "I4 赤外4 3.9μm(B07)", "赤外-i4", 1, "B07"),
        Item("wv", "WV 水蒸気(B08)", "水蒸気-wv", 1, "B08"),
        Item("w2", "W2 水蒸気(B09)", "水蒸気-w2", 1, "B09"),
        Item("w3", "W3 水蒸気(B10)", "水蒸気-w3", 1, "B10"),
        Item("vs", "VS 可視(B03)", "可視-vs", 1),
        Item("s1", "S1 赤外1・2差分", "差分-s1", 1, "B13-B15"),
        Item("s2", "S2 3.9μm差分", "差分-s2", 1, "B13-B07"),
    ]),
    ("📡 レーダー・海洋", 0xC05621, [
        Item("radar", "レーダーエコー合成図", "レーダーエコー合成図", 1),
        Item("rasrf", "解析雨量", "解析雨量", 1),
        Item("dop", "ドップラーレーダー(竜巻発生確度)", "ドップラーレーダー", 1),
        Item("thn", "雷ナウキャスト", "雷ナウキャスト", 1),
        Item("wave", "沿岸波浪実況図", "沿岸波浪実況図", 7),
    ]),
    ("📋 解説資料", 0x4A5568, [
        Item("tanki", "短期予報解説資料", "短期予報解説資料", 13),
        Item("shukan", "週間予報解説資料", "週間予報解説資料", 26),
        Item("season", "季節予報", "季節予報", 24 * 8),
    ]),
]
ITEMS = {it.key: it for _, _, items in CATEGORIES for it in items}

# ひまわりの観測データから作るときの色の付け方
HSD_STYLE = {
    "B13": (190, 310, "Greys", None), "B07": (200, 320, "Greys", None), "B08": (200, 260, "Greys", None),
    "B09": (205, 265, "Greys", None), "B10": (210, 275, "Greys", None),
    "B13-B15": (-3, 8, "RdBu_r", 0.0), "B13-B07": (-20, 10, "RdBu_r", 0.0),
}


def parse_when(date: str, time: str) -> datetime:
    d = date.strip().replace("/", "-")
    if re.fullmatch(r"\d{1,2}-\d{1,2}", d):                    # 年を省いたら今年
        d = f"{datetime.now(JST).year}-{d}"
    t = (time.strip() or "09:00").replace(":", "")
    if len(t) <= 2:
        t = t.zfill(2) + "00"
    return datetime.strptime(f"{d} {t.zfill(4)}", "%Y-%m-%d %H%M").replace(tzinfo=JST)


def data_time(message: discord.Message) -> datetime:
    """投稿が表している時刻(観測時刻・初期時刻)。書いていなければ投稿した時刻。"""
    for e in message.embeds:
        texts = [e.title or "", e.description or ""] + [f.value for f in e.fields]
        for t in texts:
            if m := TIME_RE.search(t):
                return datetime(*map(int, m.groups()), tzinfo=JST)
        for t in texts:                              # 天気図の「00UTC(日本時間9時)」
            if m := UTC_RE.search(t):
                posted = message.created_at.astimezone(timezone.utc)
                base = posted.replace(hour=int(m.group(1)), minute=0, second=0, microsecond=0)
                if base > posted:
                    base -= timedelta(days=1)
                return base.astimezone(JST)
    if message.content and (m := TIME_RE.search(message.content)):
        return datetime(*map(int, m.groups()), tzinfo=JST)
    return message.created_at.astimezone(JST)


async def search_channel(channel, bot_id: int, when: datetime, tolerance_h: float, limit: int = 4):
    """チャンネルの過去の投稿から、when にいちばん近いもの(近い順に最大 limit 件)。"""
    found = []
    start = when - timedelta(hours=tolerance_h + 6)
    async for m in channel.history(after=start, before=when + timedelta(hours=tolerance_h + 48),
                                   limit=400, oldest_first=True):
        if m.author.id != bot_id or not m.attachments:
            continue
        dt = abs((data_time(m) - when).total_seconds()) / 3600
        if dt <= tolerance_h:
            found.append((dt, m))
    found.sort(key=lambda x: x[0])
    return [m for _, m in found[:limit]]


def result_embeds(item: Item, when: datetime, messages) -> list[discord.Embed]:
    out = []
    for m in messages:
        src = m.embeds[0] if m.embeds else None
        e = discord.Embed(title=(src.title if src and src.title else item.label), url=m.jump_url,
                          color=src.color if src and src.color else 0x4A5568,
                          description=f"{data_time(m):%Y-%m-%d %H:%M} JST のデータ(#{m.channel.name} の投稿)")
        images = [a for a in m.attachments if a.content_type and a.content_type.startswith("image")]
        if images:
            e.set_image(url=images[0].url)
        if len(images) > 1:
            e.set_footer(text=f"ほか{len(images) - 1}枚は元の投稿で見られます")
        out.append(e)
    return out


class WhenModal(discord.ui.Modal):
    def __init__(self, bot, item: Item) -> None:
        super().__init__(title=f"{item.label}"[:45])
        self.bot, self.item = bot, item
        now = datetime.now(JST)
        self.date = discord.ui.TextInput(label="日付(日本時間)", placeholder="2026-10-15", default=f"{now:%Y-%m-%d}",
                                         max_length=10)
        self.time = discord.ui.TextInput(label="時刻(日本時間、24時間制)", placeholder="09:00", default="09:00",
                                         max_length=5, required=False)
        self.add_item(self.date)
        self.add_item(self.time)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            when = parse_when(self.date.value, self.time.value)
        except ValueError:
            await interaction.response.send_message("日付は 2026-10-15、時刻は 09:00 の形で入れてください。",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.answer_past(interaction, self.item, when)


class CategorySelect(discord.ui.Select):
    def __init__(self, bot, index: int) -> None:
        name, _, items = CATEGORIES[index]
        super().__init__(placeholder=f"{name}から選ぶ", custom_id=f"past:{index}", min_values=1, max_values=1,
                         options=[discord.SelectOption(label=it.label[:100], value=it.key) for it in items])
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(WhenModal(self.bot, ITEMS[self.values[0]]))


class CategoryView(discord.ui.View):
    def __init__(self, bot, index: int) -> None:
        super().__init__(timeout=None)
        self.add_item(CategorySelect(bot, index))


class CaseButton(discord.ui.Button):
    def __init__(self, bot) -> None:
        super().__init__(label="記録した事例(台風・大雨)の一覧", style=discord.ButtonStyle.primary, custom_id="past:cases")
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(**self.bot.case_list(), ephemeral=True)


class CaseView(discord.ui.View):
    def __init__(self, bot) -> None:
        super().__init__(timeout=None)
        self.add_item(CaseButton(bot))


def panel_messages(bot) -> list[dict]:
    """#過去データ に置く選択画面(種類ごとに1つの embed)。"""
    msgs = [{"embed": discord.Embed(
        title="📚 過去データ", color=0x2D3748,
        description="種類ごとのメニューから図を選ぶと、日付と時刻(日本時間)を入れる画面が出ます。\n"
                    "その時刻にいちばん近いデータを、自分にだけ見える形で表示します。\n"
                    "・このBotが投稿した図(各チャンネルに残っている過去の投稿)から探します\n"
                    "・衛星画像は、投稿が無い時刻でも、ひまわりの観測データから作ります(2015年7月以降)\n"
                    "・地上天気図は、投稿が無ければ気象庁の「保存用天気図」のページを案内します")}]
    for i, (name, color, items) in enumerate(CATEGORIES):
        e = discord.Embed(title=name, color=color, description="\n".join(f"・{it.label}" for it in items))
        msgs.append({"embed": e, "view": CategoryView(bot, i)})
    msgs.append({"embed": discord.Embed(title="🌀 事例", color=0x2C5282,
                                        description="記録した台風・大雨・手動の事例のスレッドを一覧で表示します。"),
                 "view": CaseView(bot)})
    return msgs
