"""気象情報Discord Bot:一次情報源の新着を検知して分類ごとのチャンネルへ投稿する。"""
import asyncio
import hashlib
import io
import json
import re
import logging
import os
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp
import discord
import pymupdf
from discord import app_commands
from discord.ext import tasks

from amedas import AmedasTable
from emagram import PAGE_URL as EMAGRAM_PAGE, Emagram
from mode import Mode, ModeWatcher
from note import CHECKLIST, NOTE_DEADLINE_HOUR, NOTE_STATION, NoteManager
from images import IMAGE_PRODUCTS, ImageProduct, TileComposer, parse_utc, pick_latest
from sources import CHANNEL_LAYOUT, PDF_SOURCES, USER_AGENT, WEATHER_MAPS, AsasSource, PdfSource

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("weather-bot")

JST = timezone(timedelta(hours=9))
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "10"))
POST_ON_FIRST_RUN = os.getenv("POST_ON_FIRST_RUN", "0") == "1"
STATE_PATH = Path(os.getenv("DATA_DIR", "./data")) / "state.json"
RENDER_ZOOM = float(os.getenv("RENDER_ZOOM", "3.0"))  # PDF→画像の拡大率(2.0で約144dpi、3.0で約216dpi)
MAX_FILE_BYTES = 9 * 1024 * 1024  # Discordの添付上限(10MB)より少し小さく
MAX_FILES_PER_MESSAGE = 10        # Discordの1メッセージあたりの添付数上限


# ---------- 状態の保存(どこまで投稿したか) ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- PDF → PNG ----------
def render_pdf(pdf: bytes, pages: int, zoom: float = RENDER_ZOOM) -> list[bytes]:
    """pages=0 なら全ページを画像化する。10MBを超える場合だけ解像度を下げる。"""
    images = []
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        count = doc.page_count if pages <= 0 else min(pages, doc.page_count)
        for i in range(count):
            z = zoom
            while True:
                png = doc[i].get_pixmap(matrix=pymupdf.Matrix(z, z)).tobytes("png")
                if len(png) <= MAX_FILE_BYTES or z <= 0.8:
                    break
                z *= 0.75
            images.append(png)
    return images


def make_batches(items: list[tuple[str, bytes]]) -> list[list[tuple[str, bytes]]]:
    """添付を「10個以内・合計9MB以内」のまとまりに分ける。"""
    batches, current, size = [], [], 0
    for name, data in items:
        if current and (len(current) >= MAX_FILES_PER_MESSAGE or size + len(data) > MAX_FILE_BYTES):
            batches.append(current)
            current, size = [], 0
        current.append((name, data))
        size += len(data)
    if current:
        batches.append(current)
    return batches


def latest_file(files: list[str]) -> str:
    """ファイル名に含まれる日時(12〜14桁の数字)が最も新しいものを選ぶ。
    一覧の並び順に頼らないため。日時が読み取れない場合は一覧の最後を使う。"""
    def stamp(name: str) -> str:
        found = re.findall(r"\d{12,14}", name)
        return max(found) if found else ""
    best = max(files, key=stamp)
    return best if stamp(best) else files[-1]


def to_jst_text(http_date: str | None) -> str:
    if not http_date:
        return "不明"
    return parsedate_to_datetime(http_date).astimezone(JST).strftime("%Y-%m-%d %H:%M JST")


def now_jst_text() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")


# ---------- Bot本体 ----------
class WeatherBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.state = load_state()
        self.session: aiohttp.ClientSession | None = None
        self.composer: TileComposer | None = None
        self.mode: ModeWatcher | None = None
        self.amedas: AmedasTable | None = None
        self.notes: NoteManager | None = None
        self.emagram: Emagram | None = None
        self.lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=90),
        )
        self.composer = TileComposer(self.session)
        self.mode = ModeWatcher(self.session)
        self.amedas = AmedasTable(self.session)
        self.notes = NoteManager(self.session, self.state)
        self.emagram = Emagram(self.session, self.state, self.emagram_font)
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.Forbidden:
            log.error(
                "スラッシュコマンドを登録できません(403 Missing Access)。"
                "GUILD_ID=%s が正しいサーバーIDか、Botが 'bot' と 'applications.commands' "
                "の両方のスコープで招待されているか確認してください。", GUILD_ID)
        self.poll.start()

    async def close(self) -> None:
        if self.session:
            await self.session.close()
        await super().close()

    def find_channel(self, name: str) -> discord.TextChannel | None:
        guild = self.get_guild(GUILD_ID)
        return discord.utils.get(guild.text_channels, name=name) if guild else None

    async def get_bytes(self, url: str) -> bytes:
        async with self.session.get(url) as r:
            r.raise_for_status()
            return await r.read()

    async def fingerprint(self, url: str) -> tuple[str, str | None]:
        """ファイルが更新されたか判定するための値。HEADだけで済ませて負荷を減らす。"""
        async with self.session.head(url, allow_redirects=True) as r:
            r.raise_for_status()
            etag, lm = r.headers.get("ETag"), r.headers.get("Last-Modified")
        if etag or lm:
            return f"{etag}|{lm}", lm
        return hashlib.sha256(await self.get_bytes(url)).hexdigest(), None

    # ----- 巡回 -----
    @tasks.loop(minutes=POLL_MINUTES)
    async def poll(self) -> None:
        await self.check_all()

    @poll.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()

    async def check_all(self) -> list[str]:
        async with self.lock:
            results = []
            for src in PDF_SOURCES:
                results.append(await self.safe(src.title + f"({src.note})", self.check_pdf(src)))
                await asyncio.sleep(2)  # 連続アクセスを避ける
            for wm in WEATHER_MAPS:
                results.append(await self.safe(wm.title, self.check_weather_map(wm)))
            results.append(await self.safe("アメダス一覧表", self.check_amedas()))
            results.append(await self.safe("予報ノート", self.check_note()))
            results.append(await self.safe("エマグラム", self.check_emagram()))
            results.append(await self.safe("投稿間隔の自動判定", self.update_mode()))
            for product in IMAGE_PRODUCTS:
                results.append(await self.safe(product.title, self.check_image(product)))
                await asyncio.sleep(2)
            save_state(self.state)
            return results

    async def safe(self, label: str, coro) -> str:
        try:
            return f"{label}: {await coro}"
        except Exception as e:  # 1つ失敗しても他は続ける
            log.exception("取得失敗: %s", label)
            return f"{label}: ⚠️ エラー ({e})"

    def is_new(self, key: str, value: str) -> str | None:
        """新着ならNone、そうでなければ結果メッセージを返す。"""
        prev = self.state.get(key)
        if prev == value:
            return "更新なし"
        if prev is None and not POST_ON_FIRST_RUN:
            self.state[key] = value
            return "初回のため記録のみ(次の更新から投稿)"
        return None

    # ----- PDF系(短期予報解説資料・高層天気図) -----
    async def check_pdf(self, src: PdfSource) -> str:
        fp, last_modified = await self.fingerprint(src.url)
        if (msg := self.is_new(src.key, fp)) is not None:
            return msg
        channel = self.find_channel(src.channel)
        if channel is None:
            return f"#{src.channel} が見つかりません(/setup_weather を実行)"

        pdf = await self.get_bytes(src.url)
        digest = hashlib.sha256(pdf).hexdigest()
        if self.state.get(f"{src.key}:sha256") == digest:
            self.state[src.key] = fp
            return "更新なし(内容が同じ)"
        images = await asyncio.to_thread(render_pdf, pdf, src.pages)

        description = src.note
        if len(images) > 1:
            description += f"\n全{len(images)}ページ"
        embed = discord.Embed(title=src.title, url=src.page_url, description=description,
                              color=0x2B6CB0)
        embed.add_field(name="出典", value="気象庁", inline=True)
        embed.add_field(name="公開ファイル更新", value=to_jst_text(last_modified), inline=True)
        if src.hint:
            embed.add_field(name="見るポイント", value=src.hint, inline=False)
        embed.set_footer(text=f"検知 {now_jst_text()}|図中の時刻はUTC(JST=UTC+9)")
        embed.set_image(url="attachment://page1.png")

        items = [(f"page{i + 1}.png", img) for i, img in enumerate(images)]
        if src.attach_pdf and len(pdf) <= MAX_FILE_BYTES:
            items.append((f"{src.key}.pdf", pdf))
        batches = make_batches(items)
        for n, batch in enumerate(batches):
            kwargs = {"embed": embed} if n == 0 else {
                "content": f"{src.title}(続き {n + 1}/{len(batches)})"}
            try:
                await channel.send(files=[discord.File(io.BytesIO(d), filename=nm)
                                          for nm, d in batch], **kwargs)
            except discord.HTTPException as e:
                if e.status != 413 or len(batch) == 1:
                    raise
                # まとめて送れない場合は1枚ずつ送る
                for i, (nm, d) in enumerate(batch):
                    await channel.send(file=discord.File(io.BytesIO(d), filename=nm),
                                       **(kwargs if i == 0 else {}))
        self.state[src.key] = fp
        self.state[f"{src.key}:sha256"] = digest
        return "✅ 投稿しました"

    # ----- 地上実況天気図 -----
    async def check_weather_map(self, src: AsasSource) -> str:
        async with self.session.get(src.list_url) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        files = data.get("near", {}).get(src.list_key)
        if not files:
            keys = ", ".join(data.get("near", {}).keys())
            return f"天気図一覧に '{src.list_key}' がありません(ある種類: {keys})"
        filename = latest_file(files)
        if (msg := self.is_new(src.key, filename)) is not None:
            return msg
        channel = self.find_channel(src.channel)
        if channel is None:
            return f"#{src.channel} が見つかりません(/setup_weather を実行)"

        png = await self.get_bytes(src.image_base + filename)
        embed = discord.Embed(title=src.title, url=src.page_url, color=0x2F855A,
                              description=src.note or None)
        embed.add_field(name="出典", value="気象庁", inline=True)
        embed.add_field(name="ファイル", value=filename, inline=True)
        if src.hint:
            embed.add_field(name="見るポイント", value=src.hint, inline=False)
        embed.set_footer(text=f"検知 {now_jst_text()}|図中の時刻はUTC(JST=UTC+9)")
        embed.set_image(url=f"attachment://{src.key}.png")
        await channel.send(embed=embed,
                           file=discord.File(io.BytesIO(png), filename=f"{src.key}.png"))
        self.state[src.key] = filename
        return "✅ 投稿しました"

    # ----- アメダス一覧表 -----
    def amedas_message(self, t: datetime, image: bytes | None, text: str,
                       missing: list[str]) -> dict:
        utc = t.astimezone(timezone.utc)
        embed = discord.Embed(title=f"アメダス観測値 {t:%m/%d %H:%M} JST({utc:%H} UTC)",
                              url="https://www.jma.go.jp/bosai/amedas/",
                              description=text or None, color=0xDD6B20)
        embed.set_footer(text="出典:気象庁 アメダス(速報値)")
        if missing:
            embed.add_field(name="見つからなかった地点", value="、".join(missing), inline=False)
        if image is None:
            return {"embed": embed}
        embed.set_image(url="attachment://amedas.png")
        return {"embed": embed, "file": discord.File(io.BytesIO(image), filename="amedas.png")}

    async def check_amedas(self) -> str:
        latest = await self.amedas.latest_time()
        t = self.amedas.latest_synoptic(latest)
        if t is None:
            return "投稿対象の時刻がありません(AMEDAS_HOURS を確認)"
        if (msg := self.is_new("amedas", t.isoformat())) is not None:
            return msg
        channel = self.find_channel("アメダス")
        if channel is None:
            return "#アメダス が見つかりません(/setup_weather を実行)"
        image, text, missing = await self.amedas.build(t)
        await channel.send(**self.amedas_message(t, image, text, missing))
        self.state["amedas"] = t.isoformat()
        return "✅ 投稿しました"

    # ----- エマグラム -----
    async def emagram_font(self):
        fonts = await self.amedas.ensure_fonts()
        return fonts["regular"] if fonts else None

    async def check_emagram(self) -> str:
        now = datetime.now(JST)
        items = await self.emagram.due(now)
        if not items:
            return "待機中"
        channel = self.find_channel("エマグラム")
        if channel is None:
            return "#エマグラム が見つかりません(/setup_weather を実行)"
        messages = []
        for point, name, t in items:
            try:
                surface, levels = await self.emagram.fetch(point, t)
                if sum(1 for lv in levels if lv.get("t") is not None) < 5:
                    self.emagram.postpone(point, now)
                    messages.append(f"{name}:まだ公開されていません(30分後に再確認)")
                    continue
                utc = t.astimezone(timezone.utc)
                title = f"エマグラム {name}  {t:%m/%d %H時} JST({utc:%H}UTC)"
                png, indices = await self.emagram.render(title, surface, levels)
            except Exception as e:
                log.exception("エマグラム作成失敗: %s", name)
                self.emagram.postpone(point, now)
                messages.append(f"{name}:⚠️ {e}")
                continue
            embed = discord.Embed(title=title, url=EMAGRAM_PAGE, color=0xC53030)
            for k, v in indices.items():
                embed.add_field(name=k, value=v, inline=True)
            embed.add_field(
                name="見るポイント", inline=False,
                value="気温と露点の差が小さい層=湿った層(雲の目安)。SSIは3以下で雷雨の可能性、"
                      "0以下で活発、-3以下で激しい対流の目安。K指数は30以上で雷雨の可能性が高い。"
                      "逆転層(上空ほど気温が高い層)の有無と高さも確認")
            embed.add_field(name="出典", value="気象庁 高層気象観測(指定気圧面)", inline=False)
            embed.set_footer(text="指数は指定気圧面の値だけから計算した概算です")
            embed.set_image(url="attachment://emagram.png")
            await channel.send(embed=embed, file=discord.File(io.BytesIO(png), filename="emagram.png"))
            self.emagram.done(point, t)
            messages.append(f"{name}:✅ 投稿しました")
            await asyncio.sleep(2)
        return "、".join(messages)

    # ----- 予報ノート -----
    async def note_thread(self, key: str):
        note = self.notes.notes.get(key, {})
        thread_id = note.get("thread_id")
        if not thread_id:
            return None
        try:
            return self.get_channel(thread_id) or await self.fetch_channel(thread_id)
        except discord.HTTPException:
            return None

    async def check_note(self) -> str:
        now = self.notes.now()
        messages = []
        if self.notes.should_create(now):
            channel = self.find_channel("予報ノート")
            if channel is None:
                return "#予報ノート が見つかりません(/setup_weather を実行)"
            key = now.date().isoformat()
            head = await channel.send(
                f"📝 **{now:%m/%d}の予報ノート**\n今日の **{NOTE_STATION}の最高気温** と **降水の有無(1mm以上)** を予想しましょう。"
                f"\n提出は `/yoso`、締切は **{NOTE_DEADLINE_HOUR}時** です。")
            thread = await head.create_thread(name=f"{now:%m/%d} 予報ノート",
                                              auto_archive_duration=1440)
            await thread.send(CHECKLIST)
            self.notes.notes[key] = {"station": NOTE_STATION, "thread_id": thread.id,
                                     "predictions": {}, "scored": False}
            messages.append("今日のスレッドを作成しました")

        for key in self.notes.due_for_scoring(now):
            try:
                header, lines = await self.notes.grade(key)
            except RuntimeError as e:
                if now.date() - date.fromisoformat(key) > timedelta(days=3):
                    self.notes.notes[key]["scored"] = True
                    messages.append(f"{key}:データ不足のため採点を中止しました")
                else:
                    messages.append(f"{key}:採点待ち({e})")
                continue
            text = header + "\n\n" + ("\n".join(lines) if lines else "この日の予想の提出はありませんでした。")
            target = await self.note_thread(key) or self.find_channel("予報ノート")
            if target:
                await target.send(text[:1990])
            messages.append(f"{key}を採点しました")
        self.notes.prune()
        return "、".join(messages) if messages else "待機中"

    # ----- 投稿間隔の自動切り替え -----
    async def update_mode(self) -> str:
        mode, changed = await self.mode.evaluate()
        if changed:
            await self.announce_mode(mode)
        return f"{mode.name}モード({mode.interval}分ごと)" + (" ※切り替えました" if changed else "")

    async def announce_mode(self, mode: Mode) -> None:
        if mode.name == "通常":
            text = f"🟢 **通常モード**に戻りました。画像は{mode.interval}分ごとに投稿します。"
        else:
            icon = "🔴" if mode.name == "大雨監視" else "🌀"
            text = (f"{icon} **{mode.name}モード**に切り替えました。"
                    f"画像を{mode.interval}分ごとに投稿します。\n理由:{mode.reason}\n"
                    "(出典:気象庁 アメダス・台風情報)")
        targets = {"気象レーダー"}
        if "台風" in mode.name or "台風" in (self.mode.previous_name or ""):
            targets.add("台風")
        for name in targets:
            if channel := self.find_channel(name):
                await channel.send(text)

    # ----- 画像系(ひまわり・レーダー・解析雨量) -----
    async def check_image(self, product: ImageProduct, force: bool = False) -> str:
        async with self.session.get(product.times_url) as r:
            r.raise_for_status()
            entries = await r.json(content_type=None)
        entry = pick_latest(entries, product.element, 1 if force else self.mode.current.interval)
        if entry is None:
            return "対象時刻のデータがまだありません"
        vt = entry["validtime"]
        if not force and (msg := self.is_new(product.key, vt)) is not None:
            return msg
        channel = self.find_channel(product.channel)
        if channel is None:
            return f"#{product.channel} が見つかりません(/setup_weather を実行)"

        images = [await self.composer.render(product, entry, area) for area in product.areas]
        ext = "jpg" if product.kind == "satellite" else "png"
        t = parse_utc(vt)
        embed = discord.Embed(title=product.title, url=product.page_url, color=0x805AD5,
                              description=product.note)
        embed.add_field(name="観測時刻",
                        value=f"{t.astimezone(JST):%Y-%m-%d %H:%M} JST({t:%H:%M} UTC)",
                        inline=False)
        embed.add_field(name="範囲", value=" / ".join(a.name for a in product.areas), inline=True)
        source = "気象庁" if product.kind == "satellite" else "気象庁(降水)・地理院タイル(背景地図)"
        embed.add_field(name="出典", value=source, inline=True)
        embed.set_footer(text=f"検知 {now_jst_text()}")
        embed.set_image(url=f"attachment://{product.key}_1.{ext}")
        files = [discord.File(io.BytesIO(img), filename=f"{product.key}_{i + 1}.{ext}")
                 for i, img in enumerate(images)]
        await channel.send(embed=embed, files=files)
        if not force:
            self.state[product.key] = vt
        return "✅ 投稿しました"


bot = WeatherBot()


@bot.tree.command(name="setup_weather", description="気象Bot用のカテゴリーとチャンネルを作成します")
@app_commands.default_permissions(administrator=True)
async def setup_weather(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    created = []
    for category_name, channel_names in CHANNEL_LAYOUT:
        category = (discord.utils.get(guild.categories, name=category_name)
                    or await guild.create_category(category_name))
        for name in channel_names:
            if discord.utils.get(guild.text_channels, name=name) is None:
                await guild.create_text_channel(name, category=category)
                created.append(f"#{name}")
    await interaction.followup.send(
        "作成しました: " + ", ".join(created) if created else "すべて作成済みです。",
        ephemeral=True)


@bot.tree.command(name="check_now", description="今すぐ新着を確認します")
@app_commands.default_permissions(administrator=True)
async def check_now(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    results = await bot.check_all()
    await interaction.followup.send("\n".join(results), ephemeral=True)


@bot.tree.command(name="latest_images", description="衛星・レーダー・解析雨量の最新画像を今すぐ投稿します")
@app_commands.default_permissions(administrator=True)
async def latest_images(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    results = [await bot.safe(p.title, bot.check_image(p, force=True)) for p in IMAGE_PRODUCTS]
    await interaction.followup.send("\n".join(results), ephemeral=True)


@bot.tree.command(name="mode", description="現在の投稿モードと、その理由を表示します")
async def show_mode(interaction: discord.Interaction) -> None:
    m = bot.mode.current
    reason = f"\n理由:{m.reason}" if m.reason else ""
    await interaction.response.send_message(
        f"現在は **{m.name}モード**(画像は{m.interval}分ごと)です。{reason}", ephemeral=True)


@bot.tree.command(name="amedas", description="最新のアメダス観測値の一覧表を表示します")
async def amedas_now(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    t = await bot.amedas.latest_time()
    image, text, missing = await bot.amedas.build(t)
    await interaction.followup.send(**bot.amedas_message(t, image, text, missing))


@bot.tree.command(name="yoso", description="今日の予報ノートに予想を提出します")
@app_commands.rename(max_temp="最高気温", rain="降水", memo="メモ")
@app_commands.describe(max_temp="今日の最高気温の予想(℃)", rain="1mm以上の降水があるか",
                       memo="予想の根拠など(任意)")
@app_commands.choices(rain=[app_commands.Choice(name="あり", value=1),
                            app_commands.Choice(name="なし", value=0)])
async def yoso(interaction: discord.Interaction, max_temp: float,
               rain: app_commands.Choice[int], memo: str = "") -> None:
    reply = bot.notes.submit(interaction.user.id, interaction.user.display_name,
                             max_temp, bool(rain.value), memo)
    save_state(bot.state)
    await interaction.response.send_message(reply, ephemeral=True)
    if reply.startswith("受け付けました"):
        thread = await bot.note_thread(bot.notes.now().date().isoformat())
        if thread:
            await thread.send(f"✏️ {interaction.user.display_name}さんが予想を提出しました(内容は採点時に公開)")


@bot.tree.command(name="score", description="予報ノートの自分の成績を表示します")
async def show_score(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(bot.notes.summary(str(interaction.user.id)),
                                            ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
