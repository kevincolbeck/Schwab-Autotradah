"""Generate app icon for the Schwab Live Trader."""

from PIL import Image, ImageDraw, ImageFont
import struct
import io


def create_icon():
    """Create a professional trading bot icon with a chart + lightning bolt motif."""
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # --- Background: rounded dark blue-black circle ---
    padding = 8
    bg_color = (15, 23, 42)  # dark navy
    draw.ellipse(
        [padding, padding, size - padding, size - padding],
        fill=bg_color,
    )

    # --- Outer ring: gradient-like teal accent ---
    ring_color = (6, 182, 212)  # cyan/teal
    draw.ellipse(
        [padding, padding, size - padding, size - padding],
        outline=ring_color,
        width=4,
    )

    # --- Inner glow ring ---
    inner_pad = 16
    draw.ellipse(
        [inner_pad, inner_pad, size - inner_pad, size - inner_pad],
        outline=(6, 182, 212, 80),
        width=2,
    )

    # --- Candlestick / chart bars ---
    cx, cy = size // 2, size // 2
    bar_width = 12
    bar_gap = 22
    bars = [
        # (height, color, has_wick_up, has_wick_down) - represents price bars
        (50, (239, 68, 68), True, True),     # red bar (down)
        (35, (239, 68, 68), True, False),     # red bar
        (60, (34, 197, 94), True, True),      # green bar (up)
        (80, (34, 197, 94), True, True),      # big green bar (up)
        (45, (34, 197, 94), False, True),     # green bar
    ]

    start_x = cx - (len(bars) * bar_gap) // 2
    base_y = cy + 40

    for i, (h, color, wick_up, wick_down) in enumerate(bars):
        x = start_x + i * bar_gap
        top = base_y - h

        # Draw candle body
        draw.rectangle(
            [x - bar_width // 2, top, x + bar_width // 2, base_y],
            fill=color,
        )

        # Draw wicks
        wick_color = (*color[:3], 180)
        if wick_up:
            draw.line(
                [(x, top - 12), (x, top)],
                fill=wick_color,
                width=2,
            )
        if wick_down:
            draw.line(
                [(x, base_y), (x, base_y + 8)],
                fill=wick_color,
                width=2,
            )

    # --- Lightning bolt (representing automation / speed) ---
    bolt_color = (250, 204, 21)  # gold/yellow
    bolt_x = cx + 30
    bolt_y = cy - 55
    bolt_points = [
        (bolt_x, bolt_y),
        (bolt_x - 14, bolt_y + 22),
        (bolt_x - 4, bolt_y + 22),
        (bolt_x - 18, bolt_y + 48),
        (bolt_x + 2, bolt_y + 26),
        (bolt_x - 8, bolt_y + 26),
        (bolt_x, bolt_y),
    ]
    draw.polygon(bolt_points, fill=bolt_color)

    # --- "$" text at top ---
    dollar_color = (148, 163, 184)  # slate gray
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except OSError:
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 28)
        except OSError:
            font = ImageFont.load_default()
    draw.text(
        (cx - 40, cy - 70),
        "$",
        fill=dollar_color,
        font=font,
    )

    # --- "LIVE" badge at bottom ---
    badge_w, badge_h = 52, 20
    badge_x = cx - badge_w // 2
    badge_y = base_y + 18
    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=4,
        fill=(220, 38, 38),  # red
    )
    try:
        badge_font = ImageFont.truetype("arial.ttf", 13)
    except OSError:
        try:
            badge_font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 13)
        except OSError:
            badge_font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), "LIVE", font=badge_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        (badge_x + (badge_w - tw) // 2, badge_y + (badge_h - th) // 2 - 1),
        "LIVE",
        fill=(255, 255, 255),
        font=badge_font,
    )

    # Save as PNG
    img.save("assets/icon.png", "PNG")
    print("Created assets/icon.png")

    # Create ICO with multiple sizes
    sizes = [16, 32, 48, 64, 128, 256]
    icons = []
    for s in sizes:
        resized = img.resize((s, s), Image.LANCZOS)
        icons.append(resized)

    icons[0].save(
        "assets/icon.ico",
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=icons[1:],
    )
    print("Created assets/icon.ico")


if __name__ == "__main__":
    import os
    os.makedirs("assets", exist_ok=True)
    create_icon()
