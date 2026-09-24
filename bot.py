"""気象情報Discord Bot:一次情報源の新着を検知して分類ごとのチャンネルへ投稿する。"""
import asyncio
import hashlib
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp
import discord
import pymupdf
from discord import app_commands
from discord.ext import tasks

from mode import Mode, ModeWatcher
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
        self.lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=90),
        )
        self.composer = TileComposer(self.session)
        self.mode = ModeWatcher(self.session)
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
        images = await asyncio.to_thread(render_pdf, pdf, src.pages)

        description = src.note
        if len(images) > 1:
            description += f"\n全{len(images)}ページ"
        embed = discord.Embed(title=src.title, url=src.page_url, description=description,
                              color=0x2B6CB0)
        embed.add_field(name="出典", value="気象庁", inline=True)
        embed.add_field(name="公開ファイル更新", value=to_jst_text(last_modified), inline=True)
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
        filename = files[-1]
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
        embed.set_footer(text=f"検知 {now_jst_text()}|図中の時刻はUTC(JST=UTC+9)")
        embed.set_image(url=f"attachment://{src.key}.png")
        await channel.send(embed=embed,
                           file=discord.File(io.BytesIO(png), filename=f"{src.key}.png"))
        self.state[src.key] = filename
        return "✅ 投稿しました"

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


if __name__ == "__main__":
    bot.run(TOKEN)
