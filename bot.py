"""気象情報Discord Bot:一次情報源(気象庁など)の新着を検知して、種類ごとのチャンネルへデータを送る。

解説や予想の練習の機能は持たず、データ(図・観測値)をそのまま届けることに徹する。
"""
import asyncio
import hashlib
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp
import discord
import numpy as np
import pymupdf
from discord import app_commands
from discord.ext import tasks

import amedas_map
import extra_sources
import panels
import past
import ssi
from charts import ensure_font
import himawari_hsd as hsd
import typhoon as tcmod
import windprofiler as wpr
from archive import ARCHIVE_CHANNEL, TYPHOON_CHANNEL, Events
from images import IMAGE_PRODUCTS, JAPAN, MASK, ImageProduct, TileComposer, draw_grid, parse_utc, pick_latest
from mode import Mode, ModeWatcher
from sources import CHANNEL_LAYOUT, CHANNEL_RENAMES, PDF_SOURCES, USER_AGENT, WEATHER_MAPS, AsasSource, PdfSource

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("weather-bot")

JST = timezone(timedelta(hours=9))
TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = int(os.environ.get("GUILD_ID", "0"))
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "10"))
POST_ON_FIRST_RUN = os.getenv("POST_ON_FIRST_RUN", "0") == "1"
HISTORY_LIMIT = 30  # 再送防止のために確認する最近の投稿数
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
STATE_PATH = DATA_DIR / "state.json"
RENDER_ZOOM = float(os.getenv("RENDER_ZOOM", "3.0"))  # PDF→画像の拡大率(2.0で約144dpi、3.0で約216dpi)
MAX_FILE_BYTES = 9 * 1024 * 1024  # Discordの添付上限(10MB)より少し小さく
MAX_FILES_PER_MESSAGE = 10        # Discordの1メッセージあたりの添付数上限
WPR_CHANNEL = "ウィンドプロファイラ"


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
    """ファイル名に含まれる日時(12〜14桁の数字)が最も新しいものを選ぶ。"""
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


def time_text(t: datetime) -> str:
    return f"{t.astimezone(JST):%Y-%m-%d %H:%M} JST({t.astimezone(timezone.utc):%H:%M} UTC)"


# ---------- Bot本体 ----------
class WeatherBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.state = load_state()
        self.session: aiohttp.ClientSession | None = None
        self.lock = asyncio.Lock()
        self.fetch_all_mode = False   # /fetch_all の間は、初めて扱う図も投稿する
        self.events = Events(self.state)
        self.tc_info: dict[str, dict] = {}          # 台風ごとの最新の情報(進路図用)

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=90),
        )
        self.composer = TileComposer(self.session)
        self.mode = ModeWatcher(self.session)
        self.profiler = wpr.WindProfiler(self.session)
        self.typhoons = tcmod.TyphoonSource(self.session)
        self.amedas_map = amedas_map.AmedasMap(self.session)
        for i in range(len(past.CATEGORIES)):          # 過去データの選択画面(再起動後も押せるように)
            self.add_view(past.CategoryView(self, i))
        self.add_view(past.CaseView(self))
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

    async def font(self):
        return await ensure_font(self.session)

    async def thread_of(self, thread_id: int):
        try:
            return self.get_channel(thread_id) or await self.fetch_channel(thread_id)
        except discord.HTTPException:
            return None

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
                results.append(await self.safe(src.title + f"({src.note.splitlines()[-1]})", self.check_pdf(src)))
                await asyncio.sleep(2)  # 連続アクセスを避ける
            for wm in WEATHER_MAPS:
                results.append(await self.safe(wm.title, self.check_weather_map(wm)))
            results.append(await self.safe("投稿間隔の自動判定", self.update_mode()))
            results.append(await self.safe("台風", self.check_typhoons()))
            for product in IMAGE_PRODUCTS:
                results.append(await self.safe(product.title, self.check_image(product)))
                await asyncio.sleep(2)
            results.append(await self.safe("ひまわり B07・差分画像", self.check_hsd()))
            results.append(await self.safe("ウィンドプロファイラ", self.check_windprofiler()))
            results.append(await self.safe("アメダス天気図", self.check_amedas_map()))
            results.append(await self.safe("SSI分布", self.check_ssi()))
            results.append(await self.safe("月平均天気図", self.check_monthly()))
            results.append(await self.safe("沿岸波浪実況図", self.check_wave()))
            save_state(self.state)
            return results

    async def safe(self, label: str, coro) -> str:
        try:
            return f"{label}: {await coro}"
        except Exception as e:  # 1つ失敗しても他は続ける
            log.exception("取得失敗: %s", label)
            return f"{label}: ⚠️ エラー ({e})"

    @staticmethod
    def marker(key: str, value: str) -> str:
        """投稿を識別する印。添付ファイル名の先頭に付ける。"""
        return f"{key}-{hashlib.sha1(value.encode()).hexdigest()[:10]}"

    async def already_posted(self, channel, key: str, value: str) -> bool:
        """チャンネルの最近の投稿に、同じ内容(同じ印の添付)があるか。"""
        mark = self.marker(key, value)
        async for message in channel.history(limit=HISTORY_LIMIT):
            if message.author.id != self.user.id:
                continue
            if any(a.filename.startswith(mark) for a in message.attachments):
                return True
        return False

    async def skip_if_posted(self, channel, key: str, value: str) -> str | None:
        """すでに投稿済みなら記録して理由を返す。未投稿ならNone。"""
        if await self.already_posted(channel, key, value):
            self.state[key] = value
            return "投稿済み(チャンネルで確認したので再送しません)"
        return None

    async def archive_post(self, message, label: str) -> None:
        """記録中の事例(台風・大雨など)のスレッドに、投稿へのリンクを追加する。"""
        if message is None:
            return
        for ev in self.events.targets(getattr(message.channel, "name", "")):
            thread = await self.thread_of(ev["thread_id"])
            if thread is None:
                continue
            try:
                await thread.send(f"🔗 {label}({now_jst_text()}) {message.jump_url}")
                ev["count"] += 1
            except discord.HTTPException:
                log.exception("事例スレッドへの追加に失敗しました")

    def is_new(self, key: str, value: str) -> str | None:
        """新着ならNone、そうでなければ結果メッセージを返す。"""
        prev = self.state.get(key)
        if prev == value:
            return "更新なし"
        if prev is None and not (POST_ON_FIRST_RUN or self.fetch_all_mode):
            self.state[key] = value
            return "初回のため記録のみ(次の更新から投稿)"
        return None

    # ----- PDF系(解説資料・高層天気図・数値予報天気図) -----
    async def send_files(self, channel, embed, items: list[tuple[str, bytes]], title: str):
        """添付を分けて送る(10個・9MBまで)。最初のメッセージを返す。"""
        first = None
        batches = make_batches(items)
        for n, batch in enumerate(batches):
            kwargs = {"embed": embed} if n == 0 else {"content": f"{title}(続き {n + 1}/{len(batches)})"}
            try:
                sent = await channel.send(files=[discord.File(io.BytesIO(d), filename=nm) for nm, d in batch],
                                          **kwargs)
                first = first or sent
            except discord.HTTPException as e:
                if e.status != 413 or len(batch) == 1:
                    raise
                for i, (nm, d) in enumerate(batch):     # まとめて送れない場合は1枚ずつ送る
                    sent = await channel.send(file=discord.File(io.BytesIO(d), filename=nm),
                                              **(kwargs if i == 0 else {}))
                    first = first or sent
        return first

    def chart_embed(self, title: str, src: PdfSource, last_modified, image_name: str, extra: str = ""):
        embed = discord.Embed(title=title, url=src.page_url, description=(src.note + extra).strip(), color=0x2B6CB0)
        embed.add_field(name="出典", value="気象庁", inline=True)
        embed.add_field(name="公開ファイル更新", value=to_jst_text(last_modified), inline=True)
        embed.set_footer(text=f"検知 {now_jst_text()}|図中の時刻はUTC(JST=UTC+9)")
        embed.set_image(url=f"attachment://{image_name}")
        return embed

    async def check_pdf(self, src: PdfSource) -> str:
        fp, last_modified = await self.fingerprint(src.url)
        if (msg := self.is_new(src.key, fp)) is not None:
            return msg
        first_channel = src.panels[0].channel if src.panels else src.channel
        channel = self.find_channel(first_channel)
        if channel is None:
            return f"#{first_channel} が見つかりません(/setup_weather を実行)"
        if (msg := await self.skip_if_posted(channel, src.key, fp)) is not None:
            return msg
        mark = self.marker(src.key, fp)

        data = await self.get_bytes(src.url)
        digest = hashlib.sha256(data).hexdigest()
        if self.state.get(f"{src.key}:sha256") == digest:
            self.state[src.key] = fp
            return "更新なし(内容が同じ)"
        if src.is_png:
            ext = src.url.rsplit(".", 1)[-1].lower()
            images = [data]
        else:
            ext = "png"
            images = await asyncio.to_thread(render_pdf, data, src.pages)

        result = "✅ 投稿しました"
        if src.panels and src.grid:
            pieces = await asyncio.to_thread(panels.split, images[0], *src.grid)
            if pieces is not None:
                for i, p in enumerate(src.panels):
                    ch = self.find_channel(p.channel)
                    if ch is None:
                        continue
                    name = f"{mark}-{p.row}{p.col}.png" if i == 0 else f"{src.key}-{p.row}{p.col}-{digest[:8]}.png"
                    items = [(name, pieces[(p.row, p.col)])]
                    for x in src.extras:
                        items.append((f"{mark}-x{x.row}{x.col}.png", pieces[(x.row, x.col)]))
                    extra = ("\n一緒に添付:" + "、".join(x.title for x in src.extras)) if src.extras else ""
                    embed = self.chart_embed(p.title, src, last_modified, name, extra)
                    embed.add_field(name="元の図", value=f"[{src.title}(全体)]({src.url})", inline=False)
                    sent = await self.send_files(ch, embed, items, p.title)
                    await self.archive_post(sent, f"{p.title}({src.note.splitlines()[-1]})")
                self.state[src.key] = fp
                self.state[f"{src.key}:sha256"] = digest
                return result
            result = "✅ 投稿しました(面を切り出せなかったので全体を投稿)"
            channel = self.find_channel(src.channel) or channel

        extra = f"\n全{len(images)}ページ" if len(images) > 1 else ""
        if src.panels and src.grid:
            extra += "\n(面ごとに切り出せなかったため、図の全体を投稿しています)"
        items = [(f"{mark}-p{i + 1}.{ext}", img) for i, img in enumerate(images)]
        embed = self.chart_embed(src.title, src, last_modified, items[0][0], extra)
        if src.attach_pdf and not src.is_png and len(data) <= MAX_FILE_BYTES:
            items.append((f"{mark}.pdf", data))
        first = await self.send_files(channel, embed, items, src.title)
        self.state[src.key] = fp
        self.state[f"{src.key}:sha256"] = digest
        await self.archive_post(first, f"{src.title}({src.note.splitlines()[-1]})")
        return result

    # ----- 地上天気図 -----
    async def check_weather_map(self, src: AsasSource) -> str:
        async with self.session.get(src.list_url) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        files = data.get(src.region, {}).get(src.list_key)
        if not files:
            keys = ", ".join(f"{r}.{k}" for r in data for k in (data[r] if isinstance(data[r], dict) else {}))
            return f"天気図一覧に '{src.list_key}' がありません(ある種類: {keys})"
        filename = latest_file(files)
        if (msg := self.is_new(src.key, filename)) is not None:
            return msg
        channel = self.find_channel(src.channel)
        if channel is None:
            return f"#{src.channel} が見つかりません(/setup_weather を実行)"
        if (msg := await self.skip_if_posted(channel, src.key, filename)) is not None:
            return msg
        mark = self.marker(src.key, filename)
        png = await self.get_bytes(src.image_base + filename)
        embed = discord.Embed(title=src.title, url=src.page_url, color=0x2F855A, description=src.note or None)
        embed.add_field(name="出典", value="気象庁", inline=True)
        embed.add_field(name="ファイル", value=filename, inline=True)
        embed.set_footer(text=f"検知 {now_jst_text()}|図中の時刻はUTC(JST=UTC+9)")
        embed.set_image(url=f"attachment://{mark}.png")
        sent = await channel.send(embed=embed, file=discord.File(io.BytesIO(png), filename=f"{mark}.png"))
        await self.archive_post(sent, src.title)
        self.state[src.key] = filename
        return "✅ 投稿しました"

    # ----- 投稿間隔の自動切り替えと、大雨の記録 -----
    async def update_mode(self) -> str:
        mode, changed = await self.mode.evaluate()
        if changed:
            await self.announce_mode(mode)
        await self.rain_event(mode)
        return f"{mode.name}モード({mode.interval}分ごと)" + (" ※切り替えました" if changed else "")

    async def announce_mode(self, mode: Mode) -> None:
        if mode.name == "通常":
            text = f"🟢 **通常モード**に戻りました。画像は{mode.interval}分ごとに投稿します。"
        else:
            icon = "🔴" if mode.name == "大雨監視" else "🌀"
            text = (f"{icon} **{mode.name}モード**に切り替えました。画像を{mode.interval}分ごとに投稿します。\n"
                    f"理由:{mode.reason}\n(出典:気象庁 アメダス・台風情報)")
        if channel := self.find_channel("気象レーダー"):
            await channel.send(text)

    async def rain_event(self, mode: Mode) -> None:
        now = datetime.now(JST)
        ev = self.events.get("rain")
        if mode.name == "大雨監視":
            if ev is None:
                await self.event_begin("rain", "rain", "大雨", f"🔴 {mode.reason}", now, ARCHIVE_CHANNEL)
            elif self.events.add_reason("rain", mode.reason):
                if thread := await self.thread_of(ev["thread_id"]):
                    await thread.send(f"📌 {mode.reason}"[:1990])
        elif ev is not None:
            await self.event_finish("rain", now)

    async def event_begin(self, key: str, kind: str, title: str, text: str, now: datetime,
                          channel_name: str) -> str:
        channel = self.find_channel(channel_name)
        if channel is None:
            return f"#{channel_name} が見つかりません(/setup_weather を実行)"
        head = await channel.send(f"**{now:%Y/%m/%d %H:%M} {title}** 記録中\n{text}".strip()[:1990])
        thread = await head.create_thread(name=f"{now:%Y-%m-%d} {title}"[:90], auto_archive_duration=10080)
        self.events.start(key, kind, title, thread.id, head.id, now, text)
        return "記録を開始しました"

    async def event_finish(self, key: str, now: datetime, note: str = "") -> str:
        ev = self.events.get(key)
        if ev is None:
            return "記録中の事例はありません"
        thread = await self.thread_of(ev["thread_id"])
        self.events.end(key, now)
        start = datetime.fromisoformat(ev["start"])
        hours = (now - start).total_seconds() / 3600
        text = (f"✅ **記録終了** {note}\n期間:{start:%m/%d %H:%M}〜{now:%m/%d %H:%M}(約{hours:.0f}時間)"
                f"/集めた投稿:{ev['count']}件")
        if ev["reasons"] and ev["kind"] == "rain":
            text += "\n**記録された観測**\n" + "\n".join(f"・{r}" for r in ev["reasons"][-10:])
        if thread:
            await thread.send(text[:1990])
            parent = getattr(thread, "parent", None)
            if parent and ev.get("head_id"):
                try:
                    head = await parent.fetch_message(ev["head_id"])
                    await head.edit(content=head.content.replace(
                        "記録中", f"({start:%m/%d %H:%M}〜{now:%m/%d %H:%M})")[:1990])
                except discord.HTTPException:
                    pass
        return "記録を終了しました"

    # ----- 台風(1つずつスレッドに記録) -----
    def tc_state(self, tc_id: str) -> dict:
        return self.state.setdefault("tc", {}).setdefault(tc_id, {"issue": None, "history": []})

    async def check_typhoons(self) -> str:
        active = await self.typhoons.active()
        now = datetime.now(JST)
        messages = []
        for tc_id in active:
            specs = await self.typhoons.specifications(tc_id)
            info = tcmod.parse(specs)
            self.tc_info[tc_id] = info
            st = self.tc_state(tc_id)
            issue = info["issue"].isoformat() if info["issue"] else hashlib.sha1(
                json.dumps(specs, sort_keys=True).encode()).hexdigest()
            for p in info["points"]:                 # 実況の位置を経路として記録する
                if "実況" in p["part"] and p["time"]:
                    t = p["time"].isoformat()
                    if not st["history"] or st["history"][-1]["t"] < t:
                        st["history"].append({"t": t, "lat": p["lat"], "lon": p["lon"]})
            key = f"tc:{tc_id}"
            name = tcmod.label(tc_id, info)
            if self.events.get(key) is None:
                r = await self.event_begin(key, "typhoon", name, f"気象庁が {name} の情報を発表中", now,
                                           TYPHOON_CHANNEL)
                if not r.startswith("記録を開始"):
                    messages.append(f"{tc_id}:{r}")
                    continue
            ev = self.events.get(key)
            ev["title"] = name
            if st["issue"] == issue:
                messages.append(f"{name}:更新なし")
                continue
            thread = await self.thread_of(ev["thread_id"])
            if thread is None:
                messages.append(f"{name}:スレッドが見つかりません")
                continue
            png = await tcmod.track_map(self.composer, info, st["history"])
            mark = self.marker(f"tc-{tc_id}", issue)
            await thread.send(tcmod.summary_text(tc_id, info)[:1990],
                              file=discord.File(io.BytesIO(png), filename=f"{mark}.png"))
            st["issue"] = issue
            messages.append(f"{name}:✅ 投稿しました")
        for key in [k for k in self.events.active if k.startswith("tc:")]:   # 情報が終わった台風
            tc_id = key[3:]
            if tc_id not in active:
                await self.event_finish(key, now, "気象庁の情報の発表が終わりました")
                messages.append(f"{tc_id}:記録を終了しました")
        return "、".join(messages) if messages else "発表中の台風はありません"

    # ----- 画像系(ひまわり・レーダー・解析雨量・雷・竜巻) -----
    async def target_times(self, product: ImageProduct) -> list[dict]:
        urls = [product.times_url] if isinstance(product.times_url, str) else list(product.times_url)
        entries, last_error = [], None
        for u in urls:
            try:
                async with self.session.get(u) as r:
                    r.raise_for_status()
                    entries += await r.json(content_type=None)
            except (aiohttp.ClientError, ValueError) as e:
                last_error = e
        if not entries and last_error:
            raise last_error
        return entries

    async def check_image(self, product: ImageProduct, force: bool = False) -> str:
        entries = await self.target_times(product)
        entry = pick_latest(entries, product.element, 1 if force else self.mode.current.interval)
        if entry is None:
            return "対象時刻のデータがまだありません"
        vt = entry["validtime"]
        if product.daytime_only and not 7 <= parse_utc(vt).astimezone(JST).hour <= 16:
            return "夜間のため投稿しません(可視画像)"
        if not force and (msg := self.is_new(product.key, vt)) is not None:
            return msg
        channel = self.find_channel(product.channel)
        if channel is None:
            return f"#{product.channel} が見つかりません(/setup_weather を実行)"
        if not force and (msg := await self.skip_if_posted(channel, product.key, vt)) is not None:
            return msg
        mark = self.marker(product.key, vt)

        composed = [await self.composer.compose(product, entry, area) for area in product.areas]
        if product.skip_empty and not any(has for _, has in composed):
            self.state[product.key] = vt
            return "該当なし(何も解析されていないので投稿しません)"
        from images import encode
        images = [await asyncio.to_thread(encode, img, product.kind) for img, _ in composed]
        t = parse_utc(vt)
        embed = discord.Embed(title=product.title, url=product.page_url, color=0x805AD5, description=product.note)
        embed.add_field(name="観測時刻", value=time_text(t), inline=False)
        embed.add_field(name="範囲", value=" / ".join(a.name for a in product.areas), inline=True)
        source = "気象庁" if product.kind == "satellite" else "気象庁・地理院タイル(背景地図)"
        embed.add_field(name="出典", value=source, inline=True)
        embed.set_footer(text=f"検知 {now_jst_text()}")
        embed.set_image(url=f"attachment://{mark}-1.{images[0][1]}")
        files = [discord.File(io.BytesIO(data), filename=f"{mark}-{i + 1}.{ext}")
                 for i, (data, ext) in enumerate(images)]
        sent = await channel.send(embed=embed, files=files)
        await self.archive_post(sent, f"{product.title} {t.astimezone(JST):%H:%M}")
        if not force:
            self.state[product.key] = vt
        return "✅ 投稿しました"

    # ----- ひまわり B07(3.9μm)・差分画像 -----
    async def check_hsd(self, force: bool = False) -> str:
        imgs = hsd.active_images()
        if not imgs:
            return "無効(HSD_IMAGES が空)"
        interval = 10 if force else max(self.mode.current.interval, hsd.MIN_INTERVAL)
        t = await hsd.find_latest(self.session, datetime.now(timezone.utc), interval)
        if t is None:
            return "対象時刻のデータがまだありません"
        value = t.strftime("%Y%m%d%H%M")
        if not force and (msg := self.is_new("hsd", value)) is not None:
            return msg
        channels = {}
        for im in imgs:
            ch = self.find_channel(im.channel)
            if ch is None:
                return f"#{im.channel} が見つかりません(/setup_weather を実行)"
            channels.setdefault(im.channel, (ch, []))[1].append(im)
        first_ch = next(iter(channels.values()))[0]
        if not force and (msg := await self.skip_if_posted(first_ch, "hsd", value)) is not None:
            return msg
        arrays = await hsd.build(self.session, t, JAPAN.bbox, JAPAN.zoom)
        mask = await self.composer.mosaic(MASK, JAPAN, static=True)
        font = await self.font()
        mark = self.marker("hsd", value)
        from PIL import Image
        for ch, group in channels.values():
            files = []
            for im in group:
                rgba = Image.fromarray(hsd.colorize(hsd.compute(arrays, im.expr), im), "RGBA")
                m = mask if mask.size == rgba.size else mask.resize(rgba.size)
                rgba = Image.alpha_composite(rgba, m)
                draw_grid(rgba, JAPAN, light=True)
                jpg = await asyncio.to_thread(hsd.finish, np.array(rgba), im, t, font)
                files.append((f"{mark}-{im.key}.jpg", jpg, im))
            embed = discord.Embed(title=" / ".join(im.title.replace("ひまわり ", "") for _, _, im in files),
                                  color=0x553C9A, url="https://www.data.jma.go.jp/mscweb/ja/index.html",
                                  description="\n".join(f"・{im.title}:{im.note}" for _, _, im in files))
            embed.add_field(name="観測時刻", value=time_text(t), inline=False)
            embed.add_field(name="範囲", value=JAPAN.name, inline=True)
            embed.add_field(name="出典", value="気象庁 ひまわり9号 標準データ(NOAA Open Data で公開)", inline=True)
            embed.set_image(url=f"attachment://{files[0][0]}")
            sent = await ch.send(embed=embed, files=[discord.File(io.BytesIO(d), filename=n) for n, d, _ in files])
            await self.archive_post(sent, f"{group[0].title} {t.astimezone(JST):%H:%M}")
        if not force:
            self.state["hsd"] = value
        return "✅ 投稿しました"

    # ----- ウィンドプロファイラ -----
    async def windprofiler_message(self, name: str, sid: str, hours: int, mark: str) -> dict:
        series = await self.profiler.series(sid)
        if not series:
            raise RuntimeError(f"{name}の観測値がありません")
        end = series[-1][0]
        start = max(series[0][0], end - timedelta(hours=hours))
        title = (f"ウィンドプロファイラ {name}({sid})  "
                 f"{start.astimezone(JST):%m/%d %H:%M}〜{end.astimezone(JST):%H:%M} JST")
        png = await asyncio.to_thread(wpr.render, title, series, hours, await self.font())
        embed = discord.Embed(title=f"ウィンドプロファイラ {name}", url=wpr.PAGE_URL, color=0x2C7A7B,
                              description="時間高度断面図(水平風の矢羽と鉛直流)。10分ごとの観測値")
        embed.add_field(name="最新の観測", value=time_text(end), inline=True)
        embed.add_field(name="出典", value="気象庁 ウィンドプロファイラ(WINDAS)", inline=True)
        embed.set_image(url=f"attachment://{mark}.png")
        return {"embed": embed, "file": discord.File(io.BytesIO(png), filename=f"{mark}.png")}

    async def check_windprofiler(self) -> str:
        if not wpr.AUTO_STATIONS:
            return "自動投稿なし(WPR_STATIONS が空)"
        latest = await self.profiler.latest_time()
        if (latest.hour * 60 + latest.minute) % wpr.AUTO_INTERVAL:
            latest = latest - timedelta(minutes=(latest.hour * 60 + latest.minute) % wpr.AUTO_INTERVAL)
        value = latest.strftime("%Y%m%d%H%M")
        if (msg := self.is_new("wpr", value)) is not None:
            return msg
        channel = self.find_channel(WPR_CHANNEL)
        if channel is None:
            return f"#{WPR_CHANNEL} が見つかりません(/setup_weather を実行)"
        if (msg := await self.skip_if_posted(channel, "wpr", value)) is not None:
            return msg
        done = []
        for i, st in enumerate(wpr.AUTO_STATIONS):
            found = wpr.resolve(st)
            if found is None:
                done.append(f"{st}:地点が見つかりません")
                continue
            name, sid = found
            mark = self.marker("wpr", value) + f"-{sid}" if i == 0 else f"wpr-{value}-{sid}"
            try:
                sent = await channel.send(**await self.windprofiler_message(name, sid, wpr.DEFAULT_HOURS, mark))
                await self.archive_post(sent, f"ウィンドプロファイラ {name}")
                done.append(f"{name}:✅")
            except Exception as e:
                log.exception("ウィンドプロファイラ %s", name)
                done.append(f"{name}:⚠️ {e}")
            await asyncio.sleep(2)
        self.state["wpr"] = value
        return "、".join(done)

    # ----- アメダス天気図 -----
    async def check_amedas_map(self) -> str:
        latest = await self.amedas_map.latest_time()
        t = amedas_map.target_time(latest)
        if t is None:
            return "投稿対象の時刻がありません(AMEDAS_MAP_HOURS を確認)"
        value = t.isoformat()
        if (msg := self.is_new("amedas_map", value)) is not None:
            return msg
        channel = self.find_channel("アメダス天気図")
        if channel is None:
            return "#アメダス天気図 が見つかりません(/setup_weather を実行)"
        if (msg := await self.skip_if_posted(channel, "amedas_map", value)) is not None:
            return msg
        pts = await self.amedas_map.load(t)
        font = await self.font()
        mark = self.marker("amedas_map", value)
        items = []
        for i, (name, bbox, cell) in enumerate(amedas_map.AREAS):
            png = await asyncio.to_thread(amedas_map.render, t, pts, name, bbox, cell, font)
            items.append((f"{mark}-{i + 1}.png", png))
        embed = discord.Embed(title="アメダス天気図", url=amedas_map.PAGE_URL, color=0xDD6B20,
                              description="気温・風・1時間降水量(アメダスの速報値)")
        embed.add_field(name="観測時刻", value=time_text(t), inline=False)
        embed.add_field(name="出典", value="気象庁 アメダス", inline=True)
        embed.set_image(url=f"attachment://{items[0][0]}")
        sent = await self.send_files(channel, embed, items, "アメダス天気図")
        await self.archive_post(sent, f"アメダス天気図 {t:%H時}")
        self.state["amedas_map"] = value
        return "✅ 投稿しました"

    # ----- SSI分布(MSMから計算) -----
    async def check_ssi(self) -> str:
        if not ssi.ENABLED:
            return "無効(SSI_ENABLED=0)"
        found = await ssi.find_latest(self.session, datetime.now(timezone.utc))
        if found is None:
            return "MSMのデータが見つかりません(京都大学のアーカイブに、まだ届いていない可能性があります)"
        init, url = found
        value = init.strftime("%Y%m%d%H")
        if (msg := self.is_new("ssi", value)) is not None:
            return msg
        channel = self.find_channel("ssi分布")
        if channel is None:
            return "#ssi分布 が見つかりません(/setup_weather を実行)"
        if (msg := await self.skip_if_posted(channel, "ssi", value)) is not None:
            return msg
        data = await ssi.fetch(self.session, url)
        if not data["fields"]:
            return "MSMのデータから850hPa・500hPaの値を読めませんでした"
        lats, lons = data["grid"]
        font = await self.font()
        mark = self.marker("ssi", value)
        items = []
        for step in sorted(data["fields"]):
            t850, rh850, t500 = data["fields"][step]
            value_k = await asyncio.to_thread(ssi.ssi, t850, rh850, t500)
            png = await asyncio.to_thread(ssi.render, init, step, lats, lons, value_k, font)
            items.append((f"{mark}-ft{step:02d}.png", png))
        jst = init.astimezone(JST)
        embed = discord.Embed(title="SSI分布(ショワルター安定指数)", url=ssi.PAGE_URL, color=0xC53030,
                              description="SSI = 500hPaの気温 − 850hPaの空気塊を500hPaまで持ち上げたときの温度。"
                                          "小さい(負)ほど不安定\n気象庁MSMの気圧面データから格子ごとに計算した値です")
        embed.add_field(name="初期時刻", value=f"{jst:%Y-%m-%d %H:%M} JST({init:%H} UTC)", inline=True)
        embed.add_field(name="図", value="、".join("初期値" if s == 0 else f"{s}時間予想" for s in sorted(data["fields"])),
                        inline=True)
        embed.add_field(name="出典", value="気象庁 MSM(京都大学生存圏研究所 気象庁データアーカイブ)", inline=False)
        embed.set_image(url=f"attachment://{items[0][0]}")
        sent = await self.send_files(channel, embed, items, "SSI分布")
        await self.archive_post(sent, f"SSI分布 {jst:%m/%d %H時}初期値")
        self.state["ssi"] = value
        return "✅ 投稿しました"

    # ----- 月平均天気図(気候系監視速報) -----
    async def check_monthly(self) -> str:
        ym = extra_sources.last_month(datetime.now(JST))
        if self.state.get("monthly") == ym:
            return f"更新なし({ym[:4]}年{int(ym[4:])}月分は投稿済み)"
        checked = self.state.get("monthly_checked")
        if checked and datetime.fromisoformat(checked) > datetime.now(JST) - timedelta(hours=6):
            return f"{ym[:4]}年{int(ym[4:])}月分の公開待ち"
        self.state["monthly_checked"] = datetime.now(JST).isoformat()
        channel = self.find_channel("月平均天気図")
        if channel is None:
            return "#月平均天気図 が見つかりません(/setup_weather を実行)"
        items, titles = [], []
        mark = self.marker("monthly", ym)
        for i, (url, title) in enumerate(extra_sources.monthly_urls(ym)):
            async with self.session.get(url) as r:
                if r.status != 200:
                    continue
                items.append((f"{mark}-{i + 1}.{url.rsplit('.', 1)[-1]}", await r.read()))
                titles.append(title)
        if not items:
            return f"{ym[:4]}年{int(ym[4:])}月分はまだ公開されていません"
        if (msg := await self.skip_if_posted(channel, "monthly", ym)) is not None:
            return msg
        embed = discord.Embed(title=f"月平均天気図・平年偏差図 {ym[:4]}年{int(ym[4:])}月", color=0x2F855A,
                              url=f"{extra_sources.SOKUHO}/sokuho{ym}.html",
                              description="\n".join(f"・{t}" for t in titles) + "\n(等値線が月平均、色が平年偏差)")
        embed.add_field(name="出典", value="気象庁 気候系監視速報", inline=True)
        embed.set_image(url=f"attachment://{items[0][0]}")
        await self.send_files(channel, embed, items, "月平均天気図")
        self.state["monthly"] = ym
        return "✅ 投稿しました"

    # ----- 沿岸波浪実況図 -----
    async def check_wave(self) -> str:
        found = await extra_sources.wave_image(self.session)
        if found is None:
            return ("画像の場所が見つかりません(気象庁のページの作りが想定と違います。"
                    "WAVE_IMAGE_URL に画像のURLを設定してください)")
        url, data = found
        value = hashlib.sha1(data).hexdigest()
        if (msg := self.is_new("wave", value)) is not None:
            return msg
        channel = self.find_channel("沿岸波浪実況図")
        if channel is None:
            return "#沿岸波浪実況図 が見つかりません(/setup_weather を実行)"
        if (msg := await self.skip_if_posted(channel, "wave", value)) is not None:
            return msg
        mark = self.marker("wave", value)
        ext = url.rsplit(".", 1)[-1].lower()
        embed = discord.Embed(title="沿岸波浪実況図", url=extra_sources.WAVE_PAGE, color=0x2B6CB0,
                              description="日本沿岸の波の高さ・周期・向きの実況")
        embed.add_field(name="出典", value="気象庁", inline=True)
        embed.set_footer(text=f"検知 {now_jst_text()}|図中の時刻はUTC(JST=UTC+9)")
        embed.set_image(url=f"attachment://{mark}.{ext}")
        sent = await channel.send(embed=embed, file=discord.File(io.BytesIO(data), filename=f"{mark}.{ext}"))
        await self.archive_post(sent, "沿岸波浪実況図")
        self.state["wave"] = value
        return "✅ 投稿しました"

    # ----- 過去データ -----
    async def answer_past(self, interaction: discord.Interaction, item, when: datetime) -> None:
        notes = []
        channel = self.find_channel(item.channel)
        if channel is not None:
            found = await past.search_channel(channel, self.user.id, when, item.tolerance_h)
            if found:
                await interaction.followup.send(
                    content=f"**{item.label}** {when:%Y-%m-%d %H:%M} JST に近い投稿", ephemeral=True,
                    embeds=past.result_embeds(item, when, found))
                return
            notes.append(f"#{item.channel} には、その時刻の投稿がありませんでした。")
        if item.hsd:                                    # 衛星:ひまわりの観測データから作る
            t = when.astimezone(timezone.utc).replace(second=0, microsecond=0)
            t -= timedelta(minutes=t.minute % 10)
            if t < datetime(2015, 7, 7, 2, tzinfo=timezone.utc) or t > datetime.now(timezone.utc):
                notes.append("ひまわりの観測データは2015年7月7日以降だけです。")
            else:
                try:
                    bands = item.hsd.split("-")
                    arrays = await hsd.build(self.session, t, JAPAN.bbox, JAPAN.zoom, bands)
                    mask = await self.composer.mosaic(MASK, JAPAN, static=True)
                    jpg = await asyncio.to_thread(self._past_sat, arrays, item, t, mask, await self.font())
                    embed = discord.Embed(title=f"{item.label}(過去の観測データから作成)", color=0x553C9A,
                                          description=f"{time_text(t)}\n出典:気象庁 ひまわり標準データ(NOAA Open Data)")
                    embed.set_image(url="attachment://past.jpg")
                    await interaction.followup.send(embed=embed, file=discord.File(io.BytesIO(jpg), "past.jpg"),
                                                    ephemeral=True)
                    return
                except Exception as e:
                    log.exception("過去の衛星画像")
                    notes.append(f"ひまわりの観測データから作れませんでした({e})。")
        if item.key in ("sfc", "pac"):
            ym = when.strftime("%Y_%m")
            notes.append(f"気象庁の保存用天気図(月ごと):https://www.data.jma.go.jp/yoho/data/wxchart/archive/{ym}/index.html\n"
                         "最近の数か月分:https://www.data.jma.go.jp/yoho/wxchart/quickmonthly.html")
        elif not item.hsd:
            notes.append("気象庁のホームページでは、この図の過去の分は公開されていません"
                         "(このBotが投稿を始める前の分は見られません)。")
        await interaction.followup.send("\n".join(notes), ephemeral=True)

    def _past_sat(self, arrays, item, t, mask, font) -> bytes:
        import numpy as np
        from PIL import Image
        vmin, vmax, cmap, center = past.HSD_STYLE[item.hsd]
        im = hsd.HsdImage("past", item.label, "", item.hsd, vmin, vmax, cmap, center, f"{item.hsd} (K)", "")
        rgba = Image.fromarray(hsd.colorize(hsd.compute(arrays, item.hsd), im), "RGBA")
        m = mask if mask.size == rgba.size else mask.resize(rgba.size)
        rgba = Image.alpha_composite(rgba, m)
        draw_grid(rgba, JAPAN, light=True)
        return hsd.finish(np.array(rgba), im, t, font)

    def case_list(self) -> dict:
        guild_id = GUILD_ID
        lines = []
        for ev in self.events.active.values():
            lines.append(f"🔴 記録中 {ev.get('title') or ev['kind']}({ev['start'][:16].replace('T', ' ')}〜)"
                         f" https://discord.com/channels/{guild_id}/{ev['thread_id']}")
        for ev in reversed(self.state.get("case_history", [])[-30:]):
            title = ev.get("title") or {"rain": "大雨", "manual": "手動", "typhoon": "台風"}.get(ev.get("kind"), "")
            lines.append(f"・{title}({(ev.get('start') or '')[:10]}〜{(ev.get('end') or '')[:10]})"
                         f" https://discord.com/channels/{guild_id}/{ev['thread_id']}")
        text = "\n".join(lines) if lines else "記録した事例はまだありません。"
        return {"embed": discord.Embed(title="🌀 記録した事例", description=text[:4000], color=0x2C5282)}

    async def post_panel(self, channel) -> int:
        n = 0
        for msg in past.panel_messages(self):
            await channel.send(**msg)
            n += 1
        return n


bot = WeatherBot()


@bot.tree.command(name="setup_weather", description="気象Bot用のカテゴリーとチャンネルを作成します")
@app_commands.default_permissions(administrator=True)
async def setup_weather(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    renamed, created = [], []
    home = {name: cat for cat, names in CHANNEL_LAYOUT for name in names}
    for old, new in CHANNEL_RENAMES.items():            # 以前のチャンネルは名前を変える(投稿はそのまま残る)
        ch = discord.utils.get(guild.text_channels, name=old)
        if ch is None or discord.utils.get(guild.text_channels, name=new) is not None:
            continue
        category = (discord.utils.get(guild.categories, name=home[new])
                    or await guild.create_category(home[new]))
        await ch.edit(name=new, category=category)
        renamed.append(f"#{old}→#{new}")
    for category_name, channel_names in CHANNEL_LAYOUT:
        category = (discord.utils.get(guild.categories, name=category_name)
                    or await guild.create_category(category_name))
        for name in channel_names:
            ch = discord.utils.get(guild.text_channels, name=name)
            if ch is None:
                await guild.create_text_channel(name, category=category)
                created.append(f"#{name}")
            elif ch.category != category:
                await ch.edit(category=category)
    panel = discord.utils.get(guild.text_channels, name="過去データ")
    placed = False
    if panel is not None:
        has_panel = False
        async for m in panel.history(limit=20):
            if m.author.id == bot.user.id:
                has_panel = True
                break
        if not has_panel:
            await bot.post_panel(panel)
            placed = True
    lines = []
    if renamed:
        lines.append("名前を変えました(過去の投稿はそのまま):" + "、".join(renamed))
    if created:
        lines.append("作成しました:" + "、".join(created))
    if placed:
        lines.append("#過去データ に選択画面を置きました")
    text = "\n".join(lines) or "すべて作成済みです。"
    for i in range(0, len(text), 1900):
        await interaction.followup.send(text[i:i + 1900], ephemeral=True)


@bot.tree.command(name="check_now", description="今すぐ新着を確認します")
@app_commands.default_permissions(administrator=True)
async def check_now(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    results = await bot.check_all()
    text = "\n".join(results)
    for i in range(0, len(text), 1900):
        await interaction.followup.send(text[i:i + 1900], ephemeral=True)


@bot.tree.command(name="fetch_all", description="今手に入るデータを一度すべて取得します(投稿済みのものは飛ばします)")
@app_commands.default_permissions(administrator=True)
async def fetch_all(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    bot.fetch_all_mode = True
    try:
        results = await bot.check_all()
    finally:
        bot.fetch_all_mode = False
    posted = [r for r in results if "✅" in r]
    skipped = [r for r in results if "更新なし" in r or "投稿済み" in r]
    other = [r for r in results if r not in posted and r not in skipped]
    text = (f"**投稿 {len(posted)}件**/投稿済みなので飛ばした {len(skipped)}件/その他 {len(other)}件\n\n"
            + "\n".join(posted + other))
    for i in range(0, len(text), 1900):
        await interaction.followup.send(text[i:i + 1900], ephemeral=True)


@bot.tree.command(name="latest_images", description="衛星・レーダーなどの最新画像を今すぐ投稿します")
@app_commands.default_permissions(administrator=True)
async def latest_images(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    results = [await bot.safe(p.title, bot.check_image(p, force=True)) for p in IMAGE_PRODUCTS]
    results.append(await bot.safe("ひまわり B07・差分画像", bot.check_hsd(force=True)))
    await interaction.followup.send("\n".join(results)[:1990], ephemeral=True)


@bot.tree.command(name="mode", description="現在の投稿モードと、その理由を表示します")
async def show_mode(interaction: discord.Interaction) -> None:
    m = bot.mode.current
    reason = f"\n理由:{m.reason}" if m.reason else ""
    await interaction.response.send_message(
        f"現在は **{m.name}モード**(画像は{m.interval}分ごと)です。{reason}", ephemeral=True)


async def station_autocomplete(interaction: discord.Interaction, current: str):
    names = [n for n, _ in wpr.STATIONS if current in n] or [n for n, _ in wpr.STATIONS]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


@bot.tree.command(name="windprofiler", description="指定した地点のウィンドプロファイラ(上空の風)を表示します")
@app_commands.rename(station="地点", hours="時間")
@app_commands.describe(station="観測地点(気象庁のウィンドプロファイラ33地点)", hours="何時間分を表示するか")
@app_commands.autocomplete(station=station_autocomplete)
async def windprofiler_cmd(interaction: discord.Interaction, station: str,
                           hours: app_commands.Range[int, 1, 12] = wpr.DEFAULT_HOURS) -> None:
    found = wpr.resolve(station)
    if found is None:
        await interaction.response.send_message(
            f"「{station}」は観測地点にありません。地点:" + "、".join(n for n, _ in wpr.STATIONS), ephemeral=True)
        return
    await interaction.response.defer()
    name, sid = found
    try:
        await interaction.followup.send(**await bot.windprofiler_message(name, sid, hours, f"wpr-manual-{sid}"))
    except Exception as e:
        log.exception("ウィンドプロファイラ")
        await interaction.followup.send(f"{name}のデータを取得できませんでした:{e}")


@bot.tree.command(name="past_panel", description="#過去データ に過去データの選択画面を置きます")
@app_commands.default_permissions(administrator=True)
async def past_panel(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    channel = bot.find_channel("過去データ")
    if channel is None:
        await interaction.followup.send("#過去データ が見つかりません(/setup_weather を実行)", ephemeral=True)
        return
    n = await bot.post_panel(channel)
    await interaction.followup.send(f"選択画面を置きました({n}件)", ephemeral=True)


@bot.tree.command(name="case_start", description="事例の記録を手動で開始します(前線の通過など)")
@app_commands.describe(title="事例の名前(例:寒冷前線の通過)")
async def case_start(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer(ephemeral=True)
    if bot.events.get("manual"):
        await interaction.followup.send("すでに手動の記録中の事例があります。`/case_end` で終了してください。",
                                        ephemeral=True)
        return
    result = await bot.event_begin("manual", "manual", title, "📁 手動で記録", datetime.now(JST), ARCHIVE_CHANNEL)
    save_state(bot.state)
    await interaction.followup.send(result, ephemeral=True)


@bot.tree.command(name="case_end", description="手動で始めた事例の記録を終了します")
async def case_end(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    result = await bot.event_finish("manual", datetime.now(JST))
    save_state(bot.state)
    await interaction.followup.send(result, ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
