"""エマグラムを描いてPNGに保存する。

Botから別プロセスとして呼び出す(描画ライブラリのメモリを使い終わったら解放するため)。
  python emagram_plot.py 入力.json 出力.png フォント.ttf
入力JSON: {"title": str, "levels": [{"p","z","t","rh","ws","wd"}, ...], "surface": {...}}
出力: PNG画像と、標準出力に指数のJSON
"""
import json
import sys
import warnings

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from metpy import calc as mpcalc  # noqa: E402
from metpy.plots import SkewT  # noqa: E402
from metpy.units import units  # noqa: E402

warnings.filterwarnings("ignore")


def arr(rows, key):
    return np.array([np.nan if r.get(key) is None else r[key] for r in rows], dtype=float)


def safe(func, default=None):
    try:
        return func()
    except Exception:
        return default


def main(src: str, dst: str, font_path: str) -> None:
    data = json.load(open(src, encoding="utf-8"))
    font_manager.fontManager.addfont(font_path)
    plt.rcParams["font.family"] = font_manager.FontProperties(fname=font_path).get_name()

    rows = sorted(data["levels"], key=lambda r: -r["p"])  # 気圧の高い(下層)順
    p = arr(rows, "p") * units.hPa
    t = arr(rows, "t") * units.degC
    rh = arr(rows, "rh") / 100.0
    td = mpcalc.dewpoint_from_relative_humidity(t, rh.clip(0.01, 1.0) * units.dimensionless)
    td = np.where(np.isnan(rh), np.nan, td.m) * units.degC

    fig = plt.figure(figsize=(8, 9), dpi=130)
    skew = SkewT(fig, rotation=0)  # 回転なし=エマグラム(横軸が気温、縦軸が気圧の対数)
    ax = skew.ax
    ax.set_ylim(1050, 100)
    ax.set_xlim(-60, 40)

    # 補助線:乾燥断熱線・湿潤断熱線・等飽和混合比線
    skew.plot_dry_adiabats(colors="#d69e2e", alpha=0.5, linewidths=0.8, linestyles="-")
    skew.plot_moist_adiabats(colors="#38a169", alpha=0.5, linewidths=0.8, linestyles="-")
    skew.plot_mixing_lines(colors="#3182ce", alpha=0.6, linewidths=0.8, linestyles=":")
    ax.axvline(0, color="#718096", linewidth=0.8, linestyle="--")

    ok_t = ~np.isnan(t.m)
    ok_td = ~np.isnan(td.m)
    skew.plot(p[ok_t], t[ok_t], color="#e53e3e", linewidth=2.2, label="気温")
    skew.plot(p[ok_td], td[ok_td], color="#2f855a", linewidth=2.2, label="露点温度")

    indices = {}
    # 地上の空気塊を持ち上げたときの経路と、CAPE・LCL
    if ok_t[0] and ok_td[0]:
        pp, tt, dd = p[ok_t & ok_td], t[ok_t & ok_td], td[ok_t & ok_td]
        prof = safe(lambda: mpcalc.parcel_profile(p[ok_t], t[0], td[0]).to("degC"))
        if prof is not None:
            skew.plot(p[ok_t], prof, color="#1a202c", linewidth=1.4, linestyle="--", label="持ち上げた空気塊")
            cape_cin = safe(lambda: mpcalc.cape_cin(p[ok_t], t[ok_t], np.interp(
                p[ok_t].m[::-1], pp.m[::-1], dd.m[::-1])[::-1] * units.degC, prof))
            if cape_cin:
                indices["CAPE"] = f"{cape_cin[0].m:.0f} J/kg"
                indices["CIN"] = f"{cape_cin[1].m:.0f} J/kg"
        lcl = safe(lambda: mpcalc.lcl(p[0], t[0], td[0]))
        if lcl:
            skew.plot(lcl[0], lcl[1], "ko", markersize=5)
            indices["LCL"] = f"{lcl[0].m:.0f} hPa"

    def at(level, values):
        for pv, v in zip(p.m, values):
            if abs(pv - level) < 0.5 and not np.isnan(v):
                return v
        return None

    t850, t700, t500 = at(850, t.m), at(700, t.m), at(500, t.m)
    td850, td700 = at(850, td.m), at(700, td.m)
    if None not in (t850, td850, t500):
        lifted = safe(lambda: mpcalc.parcel_profile(
            np.array([850, 500]) * units.hPa, t850 * units.degC, td850 * units.degC).to("degC"))
        if lifted is not None:
            indices["SSI"] = f"{t500 - lifted[1].m:+.1f}"
    if None not in (t850, td850, t700, td700, t500):
        indices["K指数"] = f"{(t850 - t500) + td850 - (t700 - td700):.1f}"
    if td850 is not None:
        indices["850hPa湿数"] = f"{t850 - td850:.1f}℃"
    pw = safe(lambda: mpcalc.precipitable_water(p[ok_td], td[ok_td]).to("mm"))
    if pw is not None:
        indices["可降水量"] = f"{pw.m:.0f} mm"

    # 風(矢羽根、ノット)
    ws, wd = arr(rows, "ws"), arr(rows, "wd")
    okw = ~np.isnan(ws) & ~np.isnan(wd)
    if okw.any():
        u, v = mpcalc.wind_components((ws[okw] * units("m/s")).to("knot"), wd[okw] * units.deg)
        skew.plot_barbs(p[okw], u, v, xloc=1.06)

    ax.set_xlabel("気温 (℃)")
    ax.set_ylabel("気圧 (hPa)")
    ax.set_title(data["title"], fontsize=13, loc="left")
    ax.legend(loc="upper left", fontsize=9)
    text = "\n".join(f"{k}: {v}" for k, v in indices.items())
    if text:
        ax.text(0.99, 0.99, text, transform=ax.transAxes, ha="right", va="top", fontsize=9,
                bbox=dict(facecolor="white", alpha=0.85, edgecolor="#cbd5e0"))
    ax.text(0, -0.085, "出典:気象庁 高層気象観測(指定気圧面)   黒破線:持ち上げた空気塊  ●:持ち上げ凝結高度(LCL)\n"
            "補助線  黄:乾燥断熱線  緑:湿潤断熱線  青点線:等飽和混合比線   矢羽根:風(ノット)",
            transform=ax.transAxes, fontsize=7.5, color="#4a5568", va="top")
    fig.savefig(dst, bbox_inches="tight")
    print(json.dumps(indices, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:4])
