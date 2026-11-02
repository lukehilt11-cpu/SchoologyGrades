"""Draws the app icons (needs Pillow and numpy):  python make_icons.py

Writes, for both programs, name.ico / name.icns / name_256.png, plus grades_64.png (the
report's tab icon), the installer's wizard pictures (wizard_large.bmp, wizard_small.bmp)
and the disk image background (dmg_background.png).
Edit the colours or shapes below and run it again to restyle everything.
"""

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(HERE, "icons")
os.makedirs(ICON_DIR, exist_ok=True)
CANVAS = 2048  # drawn at 2x, then shrunk for smooth edges


# ---------------------------------------------------------------- shapes
def squircle_mask(size, n=5.0):
    ys, xs = np.mgrid[0:size, 0:size]
    x = (xs + 0.5) / size * 2 - 1
    y = (ys + 0.5) / size * 2 - 1
    return Image.fromarray(((np.abs(x) ** n + np.abs(y) ** n) <= 1.0).astype("uint8") * 255, "L")


def gradient_rect(w, h, c1, c2):
    """Diagonal two-colour gradient with a soft top-left glow and a darker bottom edge."""
    ys, xs = np.mgrid[0:h, 0:w]
    x = (xs + 0.5) / w * 2 - 1
    y = (ys + 0.5) / h * 2 - 1
    t = np.clip((x + y) / 4 + 0.5, 0, 1)[..., None]
    rgb = np.array(c1, float) * (1 - t) + np.array(c2, float) * t
    glow = np.exp(-((x + 0.55) ** 2 + (y + 0.7) ** 2) / 0.38)[..., None] * 0.20
    shade = np.clip((y - 0.2) / 0.8, 0, 1)[..., None] * 0.16
    rgb = rgb + (255 - rgb) * glow
    rgb = rgb * (1 - shade)
    return Image.fromarray(np.clip(rgb, 0, 255).astype("uint8"), "RGB").convert("RGBA")


def gradient_tile(size, c1, c2):
    return gradient_rect(size, size, c1, c2)


def with_shadow(layer, offset, blur, alpha):
    """layer (RGBA) plus a soft drop shadow underneath."""
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    a = layer.getchannel("A").point(lambda v: int(v * alpha))
    shadow.putalpha(a)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.alpha_composite(shadow, (0, offset))
    out.alpha_composite(layer)
    return out


def circle(draw, cx, cy, r, **kw):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], **kw)


# ---------------------------------------------------------------- artwork
def stroke(draw, p1, p2, width):
    """A straight line with round ends."""
    draw.line([p1, p2], fill=(255, 255, 255, 255), width=int(width))
    for x, y in (p1, p2):
        circle(draw, x, y, width / 2, fill=(255, 255, 255, 255))


def art_grades(T):
    """One bold white 'A+' glyph with a soft shadow, in the style of Apple's app icons."""
    glyph = Image.new("RGBA", (T, T), (0, 0, 0, 0))
    d = ImageDraw.Draw(glyph)
    u = lambda x, y: (T * (x - 0.015), T * y)  # nudge left so the whole glyph sits centred

    # the A: two legs meeting at a rounded top, with a crossbar
    apex, left_foot, right_foot = u(0.370, 0.285), u(0.150, 0.735), u(0.590, 0.735)
    leg = T * 0.108
    stroke(d, apex, left_foot, leg)
    stroke(d, apex, right_foot, leg)
    stroke(d, u(0.226, 0.595), u(0.514, 0.595), T * 0.088)

    # the +, raised like a superscript
    stroke(d, u(0.655, 0.405), u(0.835, 0.405), T * 0.088)
    stroke(d, u(0.745, 0.315), u(0.745, 0.495), T * 0.088)

    # subtle top-to-bottom shading inside the white
    shade = np.linspace(0, 1, T)[:, None, None]
    top, bottom = np.array([255, 255, 255]), np.array([214, 232, 255])
    rgb = (top * (1 - shade) + bottom * shade) * np.ones((1, T, 1))
    colored = Image.fromarray(rgb.astype("uint8"), "RGB").convert("RGBA")
    colored.putalpha(glyph.getchannel("A"))
    return with_shadow(colored, int(T * 0.022), T * 0.03, 0.38)


def art_setup(T):
    """Three settings sliders."""
    layer = Image.new("RGBA", (T, T), (0, 0, 0, 0))
    left, right = T * 0.20, T * 0.80
    thick = T * 0.058
    for yy, pos in ((0.32, 0.30), (0.50, 0.70), (0.68, 0.46)):
        y = T * yy
        kx = left + (right - left) * pos
        track = Image.new("RGBA", (T, T), (0, 0, 0, 0))
        d = ImageDraw.Draw(track)
        d.rounded_rectangle([left, y - thick / 2, right, y + thick / 2], radius=thick / 2, fill=(255, 255, 255, 110))
        d.rounded_rectangle([left, y - thick / 2, kx, y + thick / 2], radius=thick / 2, fill=(255, 255, 255, 255))
        layer.alpha_composite(track)
        knob = Image.new("RGBA", (T, T), (0, 0, 0, 0))
        d = ImageDraw.Draw(knob)
        circle(d, kx, y, T * 0.088, fill=(255, 255, 255, 255))
        circle(d, kx, y, T * 0.034, fill=(244, 63, 94, 255))
        layer.alpha_composite(with_shadow(knob, int(T * 0.012), T * 0.014, 0.40))
    return layer


# ---------------------------------------------------------------- icons
ICONS = {
    "grades": dict(art=art_grades, c1=(94, 92, 230), c2=(18, 188, 240)),   # indigo -> cyan
    "setup": dict(art=art_setup, c1=(251, 146, 60), c2=(225, 29, 72)),     # amber -> rose
}


def render(name, mac):
    spec = ICONS[name]
    T = int(CANVAS * (0.80 if mac else 0.94))  # macOS icons keep a margin around the tile
    tile = gradient_tile(T, spec["c1"], spec["c2"])
    tile.alpha_composite(spec["art"](T))
    tile.putalpha(squircle_mask(T))
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    pos = ((CANVAS - T) // 2, (CANVAS - T) // 2)
    if mac:
        shadow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
        shadow.paste((0, 0, 0, 90), pos, tile.getchannel("A"))
        shadow = shadow.filter(ImageFilter.GaussianBlur(CANVAS * 0.012))
        canvas.alpha_composite(shadow, (0, int(CANVAS * 0.012)))
    canvas.alpha_composite(tile, pos)
    return canvas.resize((1024, 1024), Image.LANCZOS)


def extras(win_icon):
    """Pictures for the installer and the disk image, drawn from the Grades icon."""
    c1, c2 = ICONS["grades"]["c1"], ICONS["grades"]["c2"]

    # installer wizard: tall side picture (164x314) and small corner picture (55x55)
    scale = 4
    w, h = 164 * scale, 314 * scale
    side = gradient_rect(w, h, c1, c2)
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(glow)
    circle(d, w * 0.15, h * 0.92, w * 0.85, fill=(255, 255, 255, 22))
    circle(d, w * 0.95, h * 0.70, w * 0.55, fill=(255, 255, 255, 16))
    side.alpha_composite(glow)
    icon = win_icon.resize((int(w * 0.66), int(w * 0.66)), Image.LANCZOS)
    side.alpha_composite(with_shadow(icon, int(w * 0.02), w * 0.03, 0.40), ((w - icon.width) // 2, int(h * 0.12)))
    side.resize((164, 314), Image.LANCZOS).convert("RGB").save(os.path.join(ICON_DIR, "wizard_large.bmp"), "BMP")

    small = Image.new("RGBA", (55, 55), (255, 255, 255, 255))
    small.alpha_composite(win_icon.resize((47, 47), Image.LANCZOS), (4, 4))
    small.convert("RGB").save(os.path.join(ICON_DIR, "wizard_small.bmp"), "BMP")

    # disk image background (660x420): soft backdrop with an arrow from the apps to Applications
    scale = 2
    w, h = 660 * scale, 420 * scale
    ys = np.linspace(0, 1, h)[:, None, None]
    top, bottom = np.array([249, 250, 253]), np.array([229, 233, 243])
    rgb = np.ones((1, w, 1)) * (top * (1 - ys) + bottom * ys)
    bg = Image.fromarray(rgb.astype("uint8"), "RGB").convert("RGBA")
    arrow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(arrow)
    cy = h * 0.505
    d.rounded_rectangle([w * 0.425, cy - h * 0.026, w * 0.535, cy + h * 0.026], radius=int(h * 0.026), fill=(c1[0], c1[1], c1[2], 120))
    d.polygon([(w * 0.520, cy - h * 0.085), (w * 0.520, cy + h * 0.085), (w * 0.610, cy)], fill=(c1[0], c1[1], c1[2], 120))
    d.line([(w * 0.520, cy - h * 0.085), (w * 0.610, cy), (w * 0.520, cy + h * 0.085)], fill=(c1[0], c1[1], c1[2], 120), width=int(h * 0.012), joint="curve")
    bg.alpha_composite(arrow)
    bg.resize((660, 420), Image.LANCZOS).convert("RGB").save(os.path.join(ICON_DIR, "dmg_background.png"))


def main():
    for name in ICONS:
        win = render(name, mac=False)
        mac = render(name, mac=True)
        win.save(os.path.join(ICON_DIR, f"{name}.ico"),
                 sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        mac.save(os.path.join(ICON_DIR, f"{name}.icns"))
        win.resize((256, 256), Image.LANCZOS).save(os.path.join(ICON_DIR, f"{name}_256.png"))
        if name == "grades":
            win.resize((64, 64), Image.LANCZOS).save(os.path.join(ICON_DIR, "grades_64.png"))
            extras(win)


if __name__ == "__main__":
    main()
