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

from sources import ASAS, CHANNEL_LAYOUT, PDF_SOURCES, USER_AGENT, PdfSource

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
MAX_FILE_BYTES = 9 * 1024 * 1024  # Discordの添付上限(10MB)より少し小さく


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
def render_pdf(pdf: bytes, pages: int, zoom: float = 2.0) -> list[bytes]:
    images = []
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for i in range(min(pages, doc.page_count)):
            z = zoom
            while True:
                png = doc[i].get_pixmap(matrix=pymupdf.Matrix(z, z)).tobytes("png")
                if len(png) <= MAX_FILE_BYTES or z <= 0.8:
                    break
                z *= 0.75
            images.append(png)
    return images


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
        self.lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=90),
        )
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
            results.append(await self.safe(ASAS.title, self.check_asas()))
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

        embed = discord.Embed(title=src.title, url=src.page_url, description=src.note,
                              color=0x2B6CB0)
        embed.add_field(name="出典", value="気象庁", inline=True)
        embed.add_field(name="公開ファイル更新", value=to_jst_text(last_modified), inline=True)
        embed.set_footer(text=f"検知 {now_jst_text()}|図中の時刻はUTC(JST=UTC+9)")
        embed.set_image(url="attachment://page1.png")

        files = [discord.File(io.BytesIO(img), filename=f"page{i + 1}.png")
                 for i, img in enumerate(images)]
        if src.attach_pdf and len(pdf) <= MAX_FILE_BYTES:
            files.append(discord.File(io.BytesIO(pdf), filename=f"{src.key}.pdf"))
        try:
            await channel.send(embed=embed, files=files)
        except discord.HTTPException as e:
            if e.status != 413:
                raise
            # 添付が大きすぎる場合は1ページ目だけにする
            await channel.send(embed=embed,
                               file=discord.File(io.BytesIO(images[0]), filename="page1.png"))
        self.state[src.key] = fp
        return "✅ 投稿しました"

    # ----- 地上実況天気図 -----
    async def check_asas(self) -> str:
        async with self.session.get(ASAS.list_url) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        filename = data["near"]["now"][-1]
        if (msg := self.is_new(ASAS.key, filename)) is not None:
            return msg
        channel = self.find_channel(ASAS.channel)
        if channel is None:
            return f"#{ASAS.channel} が見つかりません(/setup_weather を実行)"

        png = await self.get_bytes(ASAS.image_base + filename)
        embed = discord.Embed(title=ASAS.title, url=ASAS.page_url, color=0x2F855A)
        embed.add_field(name="出典", value="気象庁", inline=True)
        embed.add_field(name="ファイル", value=filename, inline=True)
        embed.set_footer(text=f"検知 {now_jst_text()}|図中の時刻はUTC(JST=UTC+9)")
        embed.set_image(url="attachment://asas.png")
        await channel.send(embed=embed, file=discord.File(io.BytesIO(png), filename="asas.png"))
        self.state[ASAS.key] = filename
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


if __name__ == "__main__":
    bot.run(TOKEN)
