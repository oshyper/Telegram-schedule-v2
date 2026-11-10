from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps


def watermark_image(data: bytes, text: str) -> bytes:
    """Draw a semi-transparent text watermark in the bottom-right corner and return JPEG bytes."""
    img = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGBA")
    width, height = img.size
    size = max(14, width // 22)
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        font = ImageFont.load_default()

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = right - left, bottom - top
    pad = max(8, size // 2)
    x = max(pad, width - text_w - pad)
    y = max(pad, height - text_h - pad * 2)
    draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 150))      # soft shadow
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 210))

    out = BytesIO()
    Image.alpha_composite(img, overlay).convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()
