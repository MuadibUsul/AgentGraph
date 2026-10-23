"""Contrast gate for the Veynod console's core semantic pairs."""

from __future__ import annotations

PAIRS = [
    ("text-primary/page", "#e8eaf0", "#0a0b0f", 4.5),
    ("text-secondary/page", "#adb2c0", "#0a0b0f", 4.5),
    ("text-muted/page", "#8a90a0", "#0a0b0f", 4.5),
    ("text-muted/panel", "#8a90a0", "#12151c", 4.5),
    ("text-secondary/panel", "#adb2c0", "#12151c", 4.5),
    ("primary-button", "#ffffff", "#6550f0", 4.5),
    ("accent/page", "#a99eff", "#0a0b0f", 4.5),
    ("accent-strong/page", "#d2ccff", "#0a0b0f", 4.5),
    ("success/page", "#34d399", "#0a0b0f", 4.5),
    ("danger/page", "#fda4af", "#0a0b0f", 4.5),
    ("warning/page", "#fcd34d", "#0a0b0f", 4.5),
    ("info/page", "#93c5fd", "#0a0b0f", 4.5),
    ("proposal/page", "#c4b5fd", "#0a0b0f", 4.5),
    ("control-border/panel", "#5c6479", "#12151c", 3.0),
    ("focus-ring/panel", "#a99eff", "#12151c", 3.0),
]


def luminance(value: str) -> float:
    value = value.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]

    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def ratio(foreground: str, background: str) -> float:
    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def main() -> int:
    failed = False
    for name, foreground, background, minimum in PAIRS:
        measured = ratio(foreground, background)
        status = "PASS" if measured >= minimum else "FAIL"
        failed = failed or measured < minimum
        print(f"{status:4} {name:24} {measured:5.2f}:1 (min {minimum}:1)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
