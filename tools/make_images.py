"""Draw the touch icon and the share card (what LinkedIn, Slack or iMessage show for a link to the dashboard).

    .venv/bin/python tools/make_images.py      # after a build, so the numbers are current

Writes covenant_lens/site_assets/{apple-touch-icon.png, og.png}; `site` copies them next to index.html.
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from covenant_lens.site import read_cost  # noqa: E402

OUT = ROOT / "covenant_lens" / "site_assets"
BG, INK, INK2, MUTED, LINE = "#10161b", "#eef1f3", "#b9c1c8", "#87909a", "#2c343b"
ACCENT, CRIT, TRACK = "#1d4e6e", "#e0605c", "#1f262c"
SANS, MONO = "/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/SFNSMono.ttf"


def font(path: str, size: int, weight: str | None = None) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(path, size)
    if weight:
        try:
            f.set_variation_by_name(weight)
        except Exception:
            pass
    return f


def ruler(d: ImageDraw.ImageDraw, x0: int, y: int, w: int, value: float, limit: float, h: int = 18) -> None:
    top = max(limit * 1.3, value * 1.12)
    x = lambda v: x0 + min(v, top) / top * w  # noqa: E731
    d.rounded_rectangle([x0, y, x0 + w, y + h], radius=h // 2, fill=TRACK)
    lx = x(limit)
    for k in range(int(lx) - h, x0 + w, 9):  # hatched zone past the limit
        d.line([(max(k, lx), y + h if k < lx else y + h), (min(k + h, x0 + w), y)], fill="#5a2a2a", width=3)
    d.rounded_rectangle([x0, y, x(value), y + h], radius=h // 2, fill=INK2)
    d.line([(lx, y - 14), (lx, y + h + 4)], fill=CRIT, width=4)
    d.text((lx - 8, y - 46), f"Limit {limit:.2f}x", font=font(SANS, 24, "Semibold"), fill=CRIT, anchor="ra")
    d.text((x(value), y - 46), f"{value:.2f}x", font=font(MONO, 26, "Semibold"), fill=INK, anchor="ma")
    d.polygon([(x(value) - 9, y - 14), (x(value) + 9, y - 14), (x(value), y - 3)], fill=INK)


def share_card(summary: dict, cost: dict | None, example: dict | None) -> Image.Image:
    img = Image.new("RGB", (1200, 630), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1200, 8], fill=ACCENT)
    d.text((72, 70), "Covenant Lens", font=font(SANS, 76, "Bold"), fill=INK)
    sub = font(SANS, 32)
    d.text((72, 168), f"How close {summary['borrowers']} public companies are to the limits in their loan agreements,", font=sub, fill=INK2)
    d.text((72, 210), "graded against what each company reports.", font=sub, fill=INK2)
    if example:
        d.text((72, 292), f"{example['name']}, {example['period']}", font=font(SANS, 24, "Semibold"), fill=MUTED)
        ruler(d, 72, 380, 1056, example["value"], example["limit"])
    stats = [(f"{summary['limits_matched']}/{summary['limits_checked']}", "limits read correctly"),
             (f"{summary['paired_adjusted_median'] * 100:.1f}%", "median ratio error"),
             (f"${cost['read']:.2f}" if cost else "", f"to read {cost['agreements']} contracts" if cost else "")]
    d.line([(72, 462), (1128, 462)], fill=LINE, width=2)
    for i, (num, cap) in enumerate(stats):
        x = 72 + i * 360
        d.text((x, 488), num, font=font(MONO, 44, "Semibold"), fill=INK)
        d.text((x, 546), cap, font=font(SANS, 24), fill=MUTED)
    return img


def touch_icon() -> Image.Image:
    s = 180
    img = Image.new("RGB", (s, s), ACCENT)
    d = ImageDraw.Draw(img)
    k = s / 32
    for x0 in range(int(6 * k), int(26 * k), int(5.8 * k)):  # dashed limit line
        d.line([(x0, 10.5 * k), (min(x0 + 3.4 * k, 26 * k), 10.5 * k)], fill="#f3a59d", width=int(2.4 * k))
    pts = [(6 * k, 24 * k), (12 * k, 18.5 * k), (17 * k, 20.5 * k), (26 * k, 14.5 * k)]
    d.line(pts, fill="#ffffff", width=int(2.6 * k), joint="curve")
    return img


if __name__ == "__main__":
    summary = json.loads((ROOT / "data" / "universe" / "summary.json").read_text())
    casella = json.loads((ROOT / "data" / "universe" / "911177" / "result.json").read_text())["tightest"]
    from datetime import date
    example = {"name": "Casella Waste Systems", "value": casella["value"], "limit": casella["limit"],
               "period": date.fromisoformat(casella["period_end"]).strftime("%b %-d, %Y")} if casella else None
    OUT.mkdir(exist_ok=True)
    share_card(summary, read_cost(), example).save(OUT / "og.png", optimize=True)
    touch_icon().save(OUT / "apple-touch-icon.png", optimize=True)
    print("wrote", OUT / "og.png", OUT / "apple-touch-icon.png")
