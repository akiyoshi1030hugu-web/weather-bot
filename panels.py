"""1ページに何面も並んだ天気図(上下・左右)を、1面ずつ切り出す。

図と図の境目は、ページのまん中あたりにある「縦(横)に通った白い帯」か「縦(横)に通った枠線」で見つける。
見つからなければ None を返し、呼び出し側はページ全体をそのまま送る(間違った切り出しを送らないため)。
"""
import io

import numpy as np
from PIL import Image


def _groups(idx: list[int]) -> list[list[int]]:
    groups = []
    for i in idx:
        if groups and i - groups[-1][-1] <= 2:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def _split(profile: np.ndarray, length: int) -> int | None:
    """まん中(35〜65%)にある面の境目の位置。

    境目とみなすのは「枠線(ほぼ全部インクの列)のすぐ隣に白い帯がある」か「枠線が2本並んでいる」所だけ。
    1枚の図の中の経線や海の空白を、境目と見間違えないようにするため。
    """
    lo, hi = int(length * 0.35), int(length * 0.65)
    lines = _groups([i for i in range(lo, hi) if profile[i] > 0.85])
    blanks = [g for g in _groups([i for i in range(lo, hi) if profile[i] < 0.004]) if len(g) >= 4]
    found = []
    for ln in lines:
        c = (ln[0] + ln[-1]) / 2
        if any(min(abs(b[0] - c), abs(b[-1] - c)) <= 30 for b in blanks):
            found.append(c)
        elif any(o is not ln and 3 < abs((o[0] + o[-1]) / 2 - c) <= 40 for o in lines):
            found.append(c)
    if not found:
        return None
    mid = length / 2
    return int(min(found, key=lambda c: abs(c - mid)))


def _trim(img: Image.Image) -> Image.Image:
    """まわりの白い余白を少しだけ残して切り落とす。"""
    a = np.asarray(img.convert("L")) < 200
    ys, xs = np.where(a)
    if len(xs) == 0:
        return img
    pad = 6
    return img.crop((max(int(xs.min()) - pad, 0), max(int(ys.min()) - pad, 0),
                     min(int(xs.max()) + pad + 1, img.width), min(int(ys.max()) + pad + 1, img.height)))


def split(png: bytes, rows: int, cols: int) -> dict[tuple[int, int], bytes] | None:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    content = _trim(img)
    ink = np.asarray(content.convert("L")) < 160
    h, w = ink.shape
    xs, ys = [0], [0]
    if cols == 2:
        x = _split(ink.mean(axis=0), w)
        if x is None:
            return None
        xs.append(x)
    if rows == 2:
        y = _split(ink.mean(axis=1), h)
        if y is None:
            return None
        ys.append(y)
    xs.append(w)
    ys.append(h)
    out = {}
    for r in range(rows):
        for c in range(cols):
            piece = _trim(content.crop((xs[c], ys[r], xs[c + 1], ys[r + 1])))
            if piece.width < w / cols * 0.6 or piece.height < h / rows * 0.6:
                return None                       # 切り出した面が小さすぎる=境目の見つけ違い
            buf = io.BytesIO()
            piece.save(buf, "PNG", optimize=True)
            out[(r, c)] = buf.getvalue()
    return out
