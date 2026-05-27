#!/usr/bin/env python3
from __future__ import annotations

from html import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "assets" / "mmux-demo.svg"


LINES = [
    "$ mmux task add \"Change a small Python value\" --resource src",
    "task added: #1 resource=src",
    "",
    "$ mmux run . --minutes 10 --execute-agents",
    "supervisor: deterministic role leases online",
    "driver   codex  -> task #1 awaiting_review",
    "reviewer claude -> task #1 awaiting_test review=approve",
    "tester   claude -> task #1 completed",
    "",
    "$ mmux tasks .",
    "#1 completed resource=src Change a small Python value",
    "",
    "$ git diff -- src/app.py",
    "-value = 1",
    "+value = 7",
]


def visible_text() -> str:
    width = 1120
    height = 650
    line_height = 31
    start_x = 70
    start_y = 130
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '  <title id="title">mmux alpha demo recording</title>',
        '  <desc id="desc">Animated terminal recording of the mmux driver, reviewer, and tester state flow.</desc>',
        "  <defs>",
        "    <style>",
        "      .bg { fill: #101511; }",
        "      .chrome { fill: #202820; stroke: #415044; stroke-width: 1.5; }",
        "      .bar { fill: #17211b; }",
        "      .dot-red { fill: #e36d64; }",
        "      .dot-yellow { fill: #e6c45c; }",
        "      .dot-green { fill: #74c47c; }",
        "      .text { font: 18px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; fill: #dfe8dc; }",
        "      .prompt { fill: #91d18b; }",
        "      .state { fill: #9fd0ff; }",
        "      .ok { fill: #b7ecb0; }",
        "      .diff-add { fill: #b7ecb0; }",
        "      .diff-del { fill: #f0a4a4; }",
        "      .muted { fill: #8fa096; }",
        "      .cursor { fill: #dfe8dc; }",
        "    </style>",
        "  </defs>",
        f'  <rect class="bg" width="{width}" height="{height}"/>',
        '  <rect class="chrome" x="36" y="34" width="1048" height="582" rx="14"/>',
        '  <rect class="bar" x="36" y="34" width="1048" height="54" rx="14"/>',
        '  <circle class="dot-red" cx="66" cy="61" r="8"/>',
        '  <circle class="dot-yellow" cx="92" cy="61" r="8"/>',
        '  <circle class="dot-green" cx="118" cy="61" r="8"/>',
        '  <text class="text muted" x="151" y="67">mmux alpha demo - deterministic agent loop</text>',
    ]

    for index, line in enumerate(LINES):
        y = start_y + index * line_height
        cls = "text"
        if line.startswith("$"):
            cls = "text prompt"
        elif "awaiting_" in line:
            cls = "text state"
        elif "completed" in line or "review=approve" in line:
            cls = "text ok"
        elif line.startswith("+"):
            cls = "text diff-add"
        elif line.startswith("-"):
            cls = "text diff-del"
        elif not line:
            cls = "text muted"
        begin = 0.28 + index * 0.46
        parts.extend(
            [
                f'  <text class="{cls}" x="{start_x}" y="{y}" opacity="0">{escape(line)}',
                f'    <animate attributeName="opacity" from="0" to="1" begin="{begin:.2f}s" dur="0.18s" fill="freeze"/>',
                "  </text>",
            ]
        )

    cursor_y = start_y + len(LINES) * line_height
    parts.extend(
        [
            f'  <rect class="cursor" x="{start_x}" y="{cursor_y - 18}" width="10" height="22" opacity="0">',
            f'    <animate attributeName="opacity" values="0;1;0;1;0;1;0" begin="{0.28 + len(LINES) * 0.46:.2f}s" dur="2.4s" repeatCount="indefinite"/>',
            "  </rect>",
            '  <text class="text muted" x="70" y="586">LLMs work. Deterministic state decides.</text>',
            "</svg>",
        ]
    )
    return "\n".join(parts) + "\n"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(visible_text(), encoding="utf-8")
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
