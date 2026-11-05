"""図を描くときの共通の処理(日本語フォント・海岸線)。"""
import io
import json
import os
from functools import lru_cache
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

FONT_DIR = Path(os.getenv("DATA_DIR", "./data")) / "fonts"
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/bizudgothic/BIZUDGothic-Regular.ttf"
COAST_PATH = Path(__file__).with_name("coastline_eastasia.json")  # Natural Earth 1:50m 海岸線(東アジアを切り出し)


def use_font(font_path: str | Path | None) -> None:
    """日本語フォント(BIZ UDゴシック)を使う。無ければ既定のフォントのまま。"""
    if not font_path:
        return
    try:
        font_manager.fontManager.addfont(str(font_path))
        name = font_manager.FontProperties(fname=str(font_path)).get_name()
        plt.rcParams["font.family"] = name
    except (OSError, RuntimeError, ValueError):
        pass


@lru_cache(maxsize=1)
def coastlines() -> list:
    try:
        return json.loads(COAST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def draw_coast(ax, color="#555555", lw=0.6) -> None:
    for line in coastlines():
        xs, ys = zip(*line)
        ax.plot(xs, ys, color=color, lw=lw, zorder=1)


def to_png(fig, dpi=110) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


async def ensure_font(session) -> Path | None:
    """図の日本語フォント(BIZ UDゴシック、SIL Open Font License)。初回だけダウンロードして保存する。"""
    path = FONT_DIR / "BIZUDGothic-Regular.ttf"
    if path.exists():
        return path
    try:
        async with session.get(FONT_URL) as r:
            r.raise_for_status()
            data = await r.read()
    except Exception:
        return None
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
