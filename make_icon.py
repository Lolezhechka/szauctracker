"""Генератор иконки SZAUCTRACKER: чёткая на 16–256 px."""
from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE = Path(__file__).resolve().parent
ASSETS = BASE / "assets"
FONT = "C:/Windows/Fonts/seguibl.ttf"
LIME_TOP = (210, 255, 138)
LIME_BOTTOM = (137, 216, 60)
LIME = (168, 224, 95)
DARK_TOP = (28, 36, 20)
DARK_BOTTOM = (8, 11, 6)


def lerp(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def vertical_gradient(size, top, bottom):
    width, height = size
    image = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        draw.line([(0, y), (width, y)], fill=lerp(top, bottom, y / max(1, height - 1)))
    return image


def rounded_mask(size, radius, inset=0):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (inset, inset, size[0] - 1 - inset, size[1] - 1 - inset),
        radius=radius, fill=255,
    )
    return mask


def text_mask(size, font_path, font_size, text, center_x, top_y, tracking=0):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    font = ImageFont.truetype(font_path, font_size)
    # рисуем по буквам, чтобы управлять трекингом
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = center_x - total / 2
    bbox = font.getbbox(text)
    offset_y = top_y - bbox[1]
    for ch, width in zip(text, widths):
        draw.text((x, offset_y), ch, font=font, fill=255)
        x += width + tracking
    return mask


def draw_arrow(layer, scale):
    """Восходящая линия рынка со стрелкой. Координаты в долях 1024."""
    draw = ImageDraw.Draw(layer)
    width = max(2, int(58 * scale))
    points = [
        (232, 908), (392, 806), (520, 856), (724, 706),
    ]
    points = [(int(x * scale), int(y * scale)) for x, y in points]
    draw.line(points, fill=LIME, width=width, joint="curve")
    # скругления на изломах
    r = width // 2
    for x, y in points[1:-1]:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=LIME)
    # наконечник
    tip = (int(804 * scale), int(646 * scale))
    base_center = (int(742 * scale), int(706 * scale))
    dx, dy = tip[0] - base_center[0], tip[1] - base_center[1]
    length = max(1.0, (dx * dx + dy * dy) ** 0.5)
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    half = int(58 * scale)
    base1 = (int(base_center[0] + px * half), int(base_center[1] + py * half))
    base2 = (int(base_center[0] - px * half), int(base_center[1] - py * half))
    draw.polygon([tip, base1, base2], fill=LIME)


def render(size: int, full: bool) -> Image.Image:
    """Рендер в 4x и аккуратный даунсемпл."""
    s = size * 4
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    inset = max(1, int(18 * s / 1024))
    radius = int(220 * s / 1024)
    mask = rounded_mask((s, s), radius, inset)
    bg = vertical_gradient((s, s), DARK_TOP, DARK_BOTTOM).convert("RGBA")
    image.paste(bg, (0, 0), mask)

    # мягкое свечение сверху
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(
        (int(-260 * s / 1024), int(-360 * s / 1024),
         int(760 * s / 1024), int(560 * s / 1024)),
        fill=(168, 224, 95, 38),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(int(110 * s / 1024)))
    glow.putalpha(Image.composite(glow.getchannel("A"), Image.new("L", (s, s), 0), mask))
    image = Image.alpha_composite(image, glow)

    # тонкая рамка
    border = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bd = ImageDraw.Draw(border)
    bw = max(2, int(16 * s / 1024))
    bd.rounded_rectangle(
        (inset, inset, s - 1 - inset, s - 1 - inset),
        radius=radius, outline=LIME + (255,), width=bw,
    )
    inner = inset + bw + max(1, int(4 * s / 1024))
    bd.rounded_rectangle(
        (inner, inner, s - 1 - inner, s - 1 - inner),
        radius=max(1, radius - bw), outline=(230, 255, 190, 70), width=max(1, int(3 * s / 1024)),
    )
    image = Image.alpha_composite(image, border)

    # монограмма
    font_size = int((520 if full else 690) * s / 1024)
    top = int((200 if full else 145) * s / 1024)
    tmask = text_mask(
        (s, s), FONT, font_size, "SZ", s / 2, top,
        tracking=int((-18 if full else -10) * s / 1024),
    )
    bbox = tmask.getbbox()
    if bbox:
        # тень
        shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        sh = tmask.filter(ImageFilter.GaussianBlur(max(1, int(18 * s / 1024))))
        shadow.paste((0, 0, 0, 120), (0, 0), sh)
        shadow = shadow.transform(
            shadow.size, Image.AFFINE,
            (1, 0, 0, 0, 1, max(1, int(14 * s / 1024))),
            resample=Image.BICUBIC,
        )
        image = Image.alpha_composite(image, shadow)
        # градиентная заливка букв
        grad = vertical_gradient((s, s), LIME_TOP, LIME_BOTTOM).convert("RGBA")
        grad.putalpha(tmask)
        image = Image.alpha_composite(image, grad)

    # линия рынка только на крупных размерах
    if full and size >= 48:
        arrow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw_arrow(arrow, s / 1024)
        image = Image.alpha_composite(image, arrow)

    out = image.resize((size, size), Image.LANCZOS)
    # лёгкая резкость для мелких размеров
    if size <= 32:
        out = out.filter(ImageFilter.UnsharpMask(radius=0.8, percent=55, threshold=1))
    return out


def _bmp_frame(image: Image.Image) -> bytes:
    """32-битный BMP-кадр ICO: совместим со старым shell и .NET."""
    image = image.convert("RGBA")
    width, height = image.size
    pixels = image.load()
    rows = []
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for x in range(width):
            r, g, b, a = pixels[x, y]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    xor = b"".join(rows)
    mask_row = ((width + 31) // 32) * 4
    and_mask = bytes(mask_row * height)
    header = struct.pack(
        "<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0,
        width * height * 4, 0, 0, 0, 0,
    )
    return header + xor + and_mask


def save_ico(path: Path, images: dict[int, Image.Image]) -> None:
    """ICO с несжатыми BMP-кадрами: работает и в Explorer, и в .NET, и в PyInstaller."""
    entries = []
    payloads = []
    offset = 6 + 16 * len(images)
    for size in sorted(images):
        data = _bmp_frame(images[size])
        w = 0 if size >= 256 else size
        entries.append(struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset))
        payloads.append(data)
        offset += len(data)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, len(images)))
        for entry in entries:
            fh.write(entry)
        for data in payloads:
            fh.write(data)


def main():
    ASSETS.mkdir(exist_ok=True)
    sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
    images = {size: render(size, full=size >= 40) for size in sizes}
    images[256].save(ASSETS / "icon.png")
    save_ico(ASSETS / "icon.ico", images)
    print("icon.ico", (ASSETS / "icon.ico").stat().st_size, "bytes;", len(images), "sizes")


if __name__ == "__main__":
    main()
