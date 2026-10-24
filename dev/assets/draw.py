#!/usr/bin/env python3
"""Draw the README pictures (assets/*.svg), Chinese and English, from one description: the banner, one run,
the architecture and the three safeguards.

Every text is placed in a box of known width, and its width is estimated before it is written: a text that would
take more than FILL of its box stops the drawing with an error. The estimate is deliberately wide (a CJK character
is one em, a Latin one 0.62 em in bold, more than common sans-serif fonts need), so a picture that passes here keeps
its layout in the fonts GitHub's viewers fall back to (PingFang, Microsoft YaHei, Noto Sans CJK, Segoe UI, Arial).

Usage: python3 dev/assets/draw.py [OUT_DIR]   (default: assets/ of the repository)
"""
import pathlib
import sys
import unicodedata

FILL = 0.86
FONT = ("-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', "
        "'Noto Sans CJK SC', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif")
MONO = "'SF Mono', Menlo, Consolas, 'Liberation Mono', 'PingFang SC', 'Microsoft YaHei', monospace"
# graphite and orange; no blue or violet
C = {"bg0": "#131417", "bg1": "#1F2126", "panel": "#1A1C21", "line": "#34373F", "text": "#F4F1EC",
     "dim": "#ABA59C", "orange": "#FF8A3D", "green": "#3DD68C", "amber": "#F5C451", "red": "#FF6B6B",
     "gray": "#8C9099"}


def em_width(s: str, bold: bool, mono: bool = False) -> float:
    w = 0.0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 1.0
        elif mono:
            w += 0.62
        elif ch == " ":
            w += 0.32
        elif ch.isupper() or ch.isdigit():
            w += 0.70 if bold else 0.66
        else:
            w += 0.62 if bold else 0.58
    return w


class Svg:
    def __init__(self, w, h, title, desc):
        self.w, self.h, self.parts = w, h, []
        self.head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
                     f'role="img" aria-labelledby="t d">\n<title id="t">{title}</title>\n<desc id="d">{desc}</desc>\n')
        self.add(f'<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C["bg0"]}"/>'
                 f'<stop offset="1" stop-color="{C["bg1"]}"/></linearGradient></defs>')
        self.add(f'<rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="24" fill="url(#bg)" stroke="#FFFFFF" '
                 f'stroke-opacity="0.10"/>')

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size, box, color=C["text"], weight=600, anchor="middle", mono=False):
        """box: the width the text may take; it must fit in FILL of it."""
        need = em_width(s, weight >= 600, mono) * size
        if need > box * FILL:
            raise SystemExit(f"text too wide: {s!r} needs ~{need:.0f}px, box {box}px (x={x}, y={y})")
        fam = MONO if mono else FONT
        esc = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.add(f'<text x="{x}" y="{y}" fill="{color}" font-family="{fam}" font-size="{size}" '
                 f'font-weight="{weight}" text-anchor="{anchor}">{esc}</text>')

    def box(self, x, y, w, h, stroke=C["line"], fill=C["panel"], rx=18, sw=2, dash=None, op=1):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" fill-opacity="{op}" '
                 f'stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def pill(self, cx, y, s, size, color, box):
        w = min(box, em_width(s, True) * size / FILL + 28)
        self.box(cx - w / 2, y, w, size + 16, stroke=color, fill=color, rx=(size + 16) / 2, sw=1.5, op=0.12)
        self.text(cx, y + size + 3, s, size, w, color=color, weight=600)

    def save(self, path):
        pathlib.Path(path).write_text(self.head + "\n".join(self.parts) + "\n</svg>\n", encoding="utf-8")


# ---- icons, drawn around a centre ----

def icon_power(s, cx, cy, r, color):
    s.add(f'<path d="M {cx - r * 0.62:.1f} {cy - r * 0.55:.1f} A {r * 0.82:.1f} {r * 0.82:.1f} 0 1 0 '
          f'{cx + r * 0.62:.1f} {cy - r * 0.55:.1f}" fill="none" stroke="{color}" stroke-width="{r * 0.16:.1f}" '
          f'stroke-linecap="round"/>')
    s.add(f'<line x1="{cx}" y1="{cy - r * 0.95:.1f}" x2="{cx}" y2="{cy - r * 0.15:.1f}" stroke="{color}" '
          f'stroke-width="{r * 0.16:.1f}" stroke-linecap="round"/>')


def icon_wallet(s, cx, cy, r, color):
    s.add(f'<rect x="{cx - r * 0.8:.1f}" y="{cy - r * 0.55:.1f}" width="{r * 1.6:.1f}" height="{r * 1.1:.1f}" '
          f'rx="{r * 0.18:.1f}" fill="none" stroke="{color}" stroke-width="{r * 0.12:.1f}"/>')
    s.add(f'<rect x="{cx + r * 0.25:.1f}" y="{cy - r * 0.18:.1f}" width="{r * 0.55:.1f}" height="{r * 0.36:.1f}" '
          f'rx="{r * 0.1:.1f}" fill="none" stroke="{color}" stroke-width="{r * 0.1:.1f}"/>')


def icon_gpu(s, cx, cy, r, color):
    s.add(f'<rect x="{cx - r:.1f}" y="{cy - r * 0.55:.1f}" width="{r * 2:.1f}" height="{r * 1.1:.1f}" '
          f'rx="{r * 0.18:.1f}" fill="none" stroke="{color}" stroke-width="{r * 0.1:.1f}"/>')
    for dx in (-0.45, 0.45):
        s.add(f'<circle cx="{cx + r * dx:.1f}" cy="{cy:.1f}" r="{r * 0.32:.1f}" fill="none" stroke="{color}" '
              f'stroke-width="{r * 0.08:.1f}"/>')
        s.add(f'<circle cx="{cx + r * dx:.1f}" cy="{cy:.1f}" r="{r * 0.07:.1f}" fill="{color}"/>')


def icon_moon_off(s, cx, cy, r, color):
    icon_power(s, cx, cy + r * 0.05, r * 0.9, color)


def icon_receipt(s, cx, cy, r, color):
    x0, x1, y0, y1 = cx - r * 0.55, cx + r * 0.55, cy - r * 0.75, cy + r * 0.75
    s.add(f'<path d="M {x0:.1f} {y0:.1f} H {x1:.1f} V {y1:.1f} l -{r * 0.18:.1f} -{r * 0.12:.1f} '
          f'l -{r * 0.18:.1f} {r * 0.12:.1f} l -{r * 0.19:.1f} -{r * 0.12:.1f} l -{r * 0.18:.1f} {r * 0.12:.1f} '
          f'l -{r * 0.18:.1f} -{r * 0.12:.1f} l -{r * 0.19:.1f} {r * 0.12:.1f} Z" fill="none" stroke="{color}" '
          f'stroke-width="{r * 0.1:.1f}" stroke-linejoin="round"/>')
    for k in (-0.4, -0.12):
        s.add(f'<line x1="{x0 + r * 0.22:.1f}" y1="{cy + r * k:.1f}" x2="{x1 - r * 0.22:.1f}" y2="{cy + r * k:.1f}" '
              f'stroke="{color}" stroke-width="{r * 0.09:.1f}" stroke-linecap="round"/>')
    s.add(f'<path d="M {cx - r * 0.25:.1f} {cy + r * 0.25:.1f} l {r * 0.18:.1f} {r * 0.18:.1f} l {r * 0.32:.1f} '
          f'-{r * 0.32:.1f}" fill="none" stroke="{color}" stroke-width="{r * 0.1:.1f}" stroke-linecap="round" '
          f'stroke-linejoin="round"/>')


def icon_shield(s, cx, cy, r, color):
    s.add(f'<path d="M {cx:.1f} {cy - r:.1f} L {cx + r * 0.8:.1f} {cy - r * 0.65:.1f} V {cy + r * 0.05:.1f} '
          f'Q {cx + r * 0.8:.1f} {cy + r * 0.7:.1f} {cx:.1f} {cy + r:.1f} Q {cx - r * 0.8:.1f} {cy + r * 0.7:.1f} '
          f'{cx - r * 0.8:.1f} {cy + r * 0.05:.1f} V {cy - r * 0.65:.1f} Z" fill="none" stroke="{color}" '
          f'stroke-width="{r * 0.1:.1f}" stroke-linejoin="round"/>')
    s.add(f'<path d="M {cx - r * 0.5:.1f} {cy:.1f} H {cx - r * 0.2:.1f} L {cx - r * 0.05:.1f} {cy - r * 0.3:.1f} '
          f'L {cx + r * 0.12:.1f} {cy + r * 0.28:.1f} L {cx + r * 0.25:.1f} {cy:.1f} H {cx + r * 0.5:.1f}" '
          f'fill="none" stroke="{color}" stroke-width="{r * 0.08:.1f}" stroke-linecap="round" '
          f'stroke-linejoin="round"/>')


def icon_clock(s, cx, cy, r, color):
    s.add(f'<circle cx="{cx}" cy="{cy}" r="{r * 0.8:.1f}" fill="none" stroke="{color}" '
          f'stroke-width="{r * 0.1:.1f}"/>')
    s.add(f'<path d="M {cx} {cy - r * 0.45:.1f} V {cy} L {cx + r * 0.32:.1f} {cy + r * 0.2:.1f}" fill="none" '
          f'stroke="{color}" stroke-width="{r * 0.1:.1f}" stroke-linecap="round" stroke-linejoin="round"/>')
    for dx in (-0.62, 0.62):
        s.add(f'<line x1="{cx + r * dx:.1f}" y1="{cy - r * 0.95:.1f}" x2="{cx + r * dx * 0.7:.1f}" '
              f'y2="{cy - r * 0.78:.1f}" stroke="{color}" stroke-width="{r * 0.1:.1f}" stroke-linecap="round"/>')


def icon_gate(s, cx, cy, r, color):
    s.add(f'<line x1="{cx - r * 0.8:.1f}" y1="{cy - r * 0.8:.1f}" x2="{cx - r * 0.8:.1f}" y2="{cy + r * 0.8:.1f}" '
          f'stroke="{color}" stroke-width="{r * 0.12:.1f}" stroke-linecap="round"/>')
    s.add(f'<line x1="{cx + r * 0.8:.1f}" y1="{cy - r * 0.8:.1f}" x2="{cx + r * 0.8:.1f}" y2="{cy + r * 0.8:.1f}" '
          f'stroke="{color}" stroke-width="{r * 0.12:.1f}" stroke-linecap="round"/>')
    s.add(f'<line x1="{cx - r * 0.8:.1f}" y1="{cy - r * 0.15:.1f}" x2="{cx + r * 0.8:.1f}" y2="{cy - r * 0.15:.1f}" '
          f'stroke="{color}" stroke-width="{r * 0.12:.1f}" stroke-linecap="round"/>')
    s.add(f'<circle cx="{cx}" cy="{cy + r * 0.38:.1f}" r="{r * 0.22:.1f}" fill="none" stroke="{color}" '
          f'stroke-width="{r * 0.09:.1f}"/>')


def icon_user(s, cx, cy, r, color):
    s.add(f'<circle cx="{cx}" cy="{cy - r * 0.3:.1f}" r="{r * 0.32:.1f}" fill="none" stroke="{color}" '
          f'stroke-width="{r * 0.1:.1f}"/>')
    s.add(f'<path d="M {cx - r * 0.6:.1f} {cy + r * 0.65:.1f} Q {cx:.1f} {cy - r * 0.05:.1f} {cx + r * 0.6:.1f} '
          f'{cy + r * 0.65:.1f}" fill="none" stroke="{color}" stroke-width="{r * 0.1:.1f}" stroke-linecap="round"/>')


def icon_ai(s, cx, cy, r, color):
    s.add(f'<circle cx="{cx}" cy="{cy}" r="{r * 0.28:.1f}" fill="none" stroke="{color}" stroke-width="{r * 0.1:.1f}"/>')
    for k in range(8):
        import math
        a = k * math.pi / 4
        s.add(f'<line x1="{cx + math.cos(a) * r * 0.45:.1f}" y1="{cy + math.sin(a) * r * 0.45:.1f}" '
              f'x2="{cx + math.cos(a) * r * 0.75:.1f}" y2="{cy + math.sin(a) * r * 0.75:.1f}" stroke="{color}" '
              f'stroke-width="{r * 0.09:.1f}" stroke-linecap="round"/>')


def icon_hourglass(s, cx, cy, r, color):
    s.add(f'<path d="M {cx - r * 0.5:.1f} {cy - r * 0.75:.1f} H {cx + r * 0.5:.1f} L {cx - r * 0.5:.1f} '
          f'{cy + r * 0.75:.1f} H {cx + r * 0.5:.1f} Z" fill="none" stroke="{color}" stroke-width="{r * 0.1:.1f}" '
          f'stroke-linejoin="round"/>')


def icon_copy(s, cx, cy, r, color):
    for dx, dy in ((-0.2, -0.2), (0.2, 0.2)):
        s.add(f'<rect x="{cx + r * dx - r * 0.45:.1f}" y="{cy + r * dy - r * 0.45:.1f}" width="{r * 0.9:.1f}" '
              f'height="{r * 0.9:.1f}" rx="{r * 0.12:.1f}" fill="{C["panel"]}" stroke="{color}" '
              f'stroke-width="{r * 0.1:.1f}"/>')


def icon_terminal(s, cx, cy, r, color):
    s.add(f'<rect x="{cx - r * 0.85:.1f}" y="{cy - r * 0.65:.1f}" width="{r * 1.7:.1f}" height="{r * 1.3:.1f}" '
          f'rx="{r * 0.18:.1f}" fill="none" stroke="{color}" stroke-width="{r * 0.1:.1f}"/>')
    s.add(f'<path d="M {cx - r * 0.5:.1f} {cy - r * 0.25:.1f} l {r * 0.3:.1f} {r * 0.25:.1f} l -{r * 0.3:.1f} '
          f'{r * 0.25:.1f} M {cx:.1f} {cy + r * 0.3:.1f} h {r * 0.45:.1f}" fill="none" stroke="{color}" '
          f'stroke-width="{r * 0.1:.1f}" stroke-linecap="round" stroke-linejoin="round"/>')


def badge(s, cx, cy, r, color, icon):
    s.add(f'<circle cx="{cx}" cy="{cy}" r="{r + 10}" fill="{color}" fill-opacity="0.10"/>')
    s.add(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{C["bg0"]}" stroke="{color}" stroke-width="3"/>')
    icon(s, cx, cy, r * 0.62, color)


def arrow(s, x1, y1, x2, y2, color, dash=None, width=4, head=True):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    s.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{width}" '
          f'stroke-linecap="round"{d}/>')
    if head:
        import math
        a = math.atan2(y2 - y1, x2 - x1)
        p = [(x2, y2), (x2 - 16 * math.cos(a) + 8 * math.sin(a), y2 - 16 * math.sin(a) - 8 * math.cos(a)),
             (x2 - 16 * math.cos(a) - 8 * math.sin(a), y2 - 16 * math.sin(a) + 8 * math.cos(a))]
        s.add(f'<polygon points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in p)}" fill="{color}"/>')


# ---- the four pictures ----

T = {
    "cn": {
        "hero_title": "AutoDL Autopilot 项目头图",
        "hero_desc": "项目名 AutoDL Autopilot，右侧是一块指针指向关机的自动驾驶仪表；文字为开机、运行任务、关机，以及三项特点。",
        "tag": "开机、运行任务、关机，交给 AI", "sub": "空闲自动关机，每一笔都记账",
        "chips": ["控制台接口，无需浏览器", "开机前过预算关口", "空闲自动关机"],
        "dial": ["开", "关"],
        "demo_desc": "一次运行：用户说把训练跑起来，依次经过预算核对、开机、运行任务、关机与记账，历时 8 分 47 秒，费用 0.14 元。",
        "prompt": "把训练跑起来", "steps": ["预算核对", "开机", "运行任务", "关机", "记账"],
        "result": "本次 8 分 47 秒 · ￥0.14",
        "arch_desc": "架构：用户一句话交给 AI；AI 经 skill 调用本机的 autopilot；autopilot 先过预算与账本，再经控制台接口开关机、设定时与读账单，经 SSH 在实例上运行任务，实例上的守护程序空闲即关机。",
        "you": "你", "ai": "AI + skill", "cli": "autopilot", "cli_sub": "本机命令行", "ledger": "预算与账本",
        "console": "AutoDL 控制台", "console_sub": "开关机 · 定时 · 账单",
        "instance": "AutoDL 实例", "instance_sub": "任务 · 守护程序",
        "api": "控制台接口", "ssh": "SSH",
        "safety_desc": "费用控制的三道机制：预算关口在开机前核对，守护程序空闲即关机，控制台定时关机到时即关机。",
        "safety": [("预算关口", "每次开机前核对"), ("守护程序", "空闲即关机，对话中断也生效"),
                   ("控制台定时关机", "到时即关机，按需设置")],
    },
    "en": {
        "hero_title": "AutoDL Autopilot banner",
        "hero_desc": "The name AutoDL Autopilot beside an autopilot dial whose needle points to off; the text names power-on, the job and shutdown, and three features.",
        "tag": "Your AI powers AutoDL on and off", "sub": "idle shutdown, every charge on the books",
        "chips": ["Console API, no browser", "Budget gate", "Idle shutdown"],
        "dial": ["ON", "OFF"],
        "demo_desc": "One run: the user asks to start the training; budget check, power-on, the job, shutdown and bookkeeping follow, 8 min 47 s, 0.14 yuan.",
        "prompt": "start the training", "steps": ["Budget", "Power on", "Run", "Shut down", "Booked"],
        "result": "8 min 47 s · 0.14 yuan",
        "arch_desc": "Architecture: you tell the AI; the AI calls autopilot on your computer through the skill; autopilot checks the budget and the ledger, powers the instance and reads the bills through the console API, and runs jobs over SSH; the guard on the instance shuts it down when idle.",
        "you": "You", "ai": "AI + skill", "cli": "autopilot", "cli_sub": "local CLI", "ledger": "Ledger",
        "console": "AutoDL console", "console_sub": "power · timer · bills",
        "instance": "AutoDL instance", "instance_sub": "jobs · guard",
        "api": "console API", "ssh": "SSH",
        "safety_desc": "Three safeguards: the budget gate before every power-on, the guard that shuts down when idle, the console timer.",
        "safety": [("Budget gate", "before every power-on"), ("Guard", "shuts down when idle"),
                   ("Console timer", "hard stop, if you ask")],
    },
}


def hero(t, out):
    import math
    s = Svg(1280, 400, t["hero_title"], t["hero_desc"])
    s.add(f'<text x="72" y="150" font-family="{FONT}" font-size="64" font-weight="800">'
          f'<tspan fill="{C["text"]}">AutoDL </tspan><tspan fill="{C["orange"]}">Autopilot</tspan></text>')
    if em_width("AutoDL Autopilot", True) * 64 > 760 * FILL:
        raise SystemExit("hero name too wide")
    s.add(f'<rect x="74" y="172" width="64" height="5" rx="2.5" fill="{C["orange"]}"/>'
          f'<rect x="146" y="172" width="24" height="5" rx="2.5" fill="{C["green"]}"/>')
    s.text(72, 228, t["tag"], 30, 760, anchor="start")
    s.text(72, 270, t["sub"], 22, 760, color=C["dim"], weight=500, anchor="start")
    x = 72
    for chip, col in zip(t["chips"], (C["orange"], C["amber"], C["green"])):
        w = em_width(chip, True) * 17 / FILL + 28
        s.box(x, 304, w, 40, stroke=col, fill=col, rx=20, sw=1.5, op=0.12)
        s.text(x + w / 2, 330, chip, 17, w, color=col)
        x += w + 12
    if x > 840:
        raise SystemExit(f"hero chips reach x={x:.0f}, into the dial")
    # right: an autopilot dial, 240 degrees, the needle resting on OFF
    cx, cy, r = 1035, 205, 135
    s.add(f'<circle cx="{cx}" cy="{cy}" r="{r + 26}" fill="{C["orange"]}" fill-opacity="0.05"/>')
    s.add(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{C["panel"]}" stroke="{C["line"]}" stroke-width="2"/>')

    def pt(deg, rad):
        a = math.radians(deg)
        return cx + rad * math.cos(a), cy - rad * math.sin(a)
    a0, a1 = 210, -30                       # from lower left over the top to lower right
    x0, y0 = pt(a0, r - 22)
    xm, ym = pt(90, r - 22)
    x1, y1 = pt(a1, r - 22)
    s.add(f'<path d="M {x0:.1f} {y0:.1f} A {r - 22} {r - 22} 0 0 1 {xm:.1f} {ym:.1f}" fill="none" '
          f'stroke="{C["green"]}" stroke-width="12" stroke-linecap="round" stroke-opacity="0.85"/>')
    s.add(f'<path d="M {xm:.1f} {ym:.1f} A {r - 22} {r - 22} 0 0 1 {x1:.1f} {y1:.1f}" fill="none" '
          f'stroke="{C["orange"]}" stroke-width="12" stroke-linecap="round"/>')
    for k in range(13):
        d = a0 - k * (a0 - a1) / 12
        xa, ya = pt(d, r - 44)
        xb, yb = pt(d, r - 34 if k % 3 else r - 30)
        s.add(f'<line x1="{xa:.1f}" y1="{ya:.1f}" x2="{xb:.1f}" y2="{yb:.1f}" stroke="{C["dim"]}" '
              f'stroke-width="{3 if k % 3 == 0 else 2}" stroke-opacity="0.7"/>')
    for label, d, col in ((t["dial"][0], a0 - 4, C["green"]), (t["dial"][1], a1 + 4, C["orange"])):
        xl, yl = pt(d, r - 70)
        s.text(round(xl, 1), round(yl + 7, 1), label, 18, 70, color=col, weight=700)
    xn, yn = pt(-10, r - 50)                # the needle, near OFF
    s.add(f'<line x1="{cx}" y1="{cy}" x2="{xn:.1f}" y2="{yn:.1f}" stroke="{C["text"]}" stroke-width="6" '
          f'stroke-linecap="round"/>')
    s.add(f'<circle cx="{cx}" cy="{cy}" r="16" fill="{C["orange"]}"/><circle cx="{cx}" cy="{cy}" r="6" '
          f'fill="{C["bg0"]}"/>')
    icon_power(s, cx, cy + 74, 18, C["dim"])
    s.save(out)


def demo(t, out):
    s = Svg(1280, 420, "demo", t["demo_desc"])
    s.box(40, 28, 1200, 364, fill="#17181C")
    for i, col in enumerate((C["red"], C["amber"], C["green"])):
        s.add(f'<circle cx="{72 + i * 22}" cy="58" r="6" fill="{col}"/>')
    s.box(70, 84, 560, 52, stroke=C["line"], fill=C["bg0"], rx=12, sw=1.5)
    s.add(f'<path d="M 92 100 l 9 9 l -9 9" fill="none" stroke="{C["orange"]}" stroke-width="3.5" '
          f'stroke-linecap="round" stroke-linejoin="round"/>')
    s.text(122, 119, t["prompt"], 24, 490, anchor="start", mono=True, weight=500)
    cols = (C["amber"], C["orange"], C["green"], C["red"], C["amber"])
    icons = (icon_wallet, icon_power, icon_gpu, icon_moon_off, icon_receipt)
    xs = [180 + i * 230 for i in range(5)]
    for i in range(4):
        s.add(f'<line x1="{xs[i] + 52}" y1="226" x2="{xs[i + 1] - 52}" y2="226" stroke="{cols[i]}" '
              f'stroke-width="4" stroke-opacity="0.6"/>')
    for x, col, ic, label in zip(xs, cols, icons, t["steps"]):
        badge(s, x, 226, 42, col, ic)
        s.text(x, 316, label, 22, 210)
    s.pill(640, 340, t["result"], 22, C["orange"], 460)
    s.save(out)


def node(s, x, y, w, h, col, icon, title, sub=None):
    """A card: an icon on the left, a title and an optional line below it on the right."""
    s.box(x, y, w, h, stroke=col, fill=col, op=0.07, sw=2)
    badge(s, x + 50, y + h / 2, 28, col, icon)
    tx, tw = x + 96, w - 108
    if sub:
        s.text(tx, y + h / 2 - 4, title, 20, tw, anchor="start")
        s.text(tx, y + h / 2 + 24, sub, 16, tw, color=C["dim"], weight=500, anchor="start")
    else:
        s.text(tx, y + h / 2 + 7, title, 20, tw, anchor="start")


def arch(t, out):
    s = Svg(1280, 500, "arch", t["arch_desc"])
    # a pipeline from left to right that splits at autopilot: up to the console, down to the instance
    node(s, 40, 190, 170, 100, C["dim"], icon_user, t["you"])
    node(s, 250, 190, 250, 100, C["orange"], icon_ai, t["ai"])
    node(s, 530, 190, 280, 100, C["orange"], icon_terminal, t["cli"], t["cli_sub"])
    node(s, 530, 360, 280, 100, C["amber"], icon_receipt, t["ledger"])
    node(s, 900, 70, 340, 110, C["orange"], icon_power, t["console"], t["console_sub"])
    node(s, 900, 300, 340, 110, C["green"], icon_shield, t["instance"], t["instance_sub"])
    arrow(s, 212, 240, 242, 240, C["dim"], width=3)
    arrow(s, 502, 240, 522, 240, C["orange"], width=3)
    s.add(f'<line x1="670" y1="292" x2="670" y2="352" stroke="{C["amber"]}" stroke-width="3" '
          f'stroke-dasharray="5 7" stroke-linecap="round"/>')
    s.add(f'<path d="M 812 220 C 856 220, 850 125, 892 125" fill="none" stroke="{C["orange"]}" stroke-width="4" '
          f'stroke-linecap="round"/>')
    s.add(f'<polygon points="898,125 884,117 884,133" fill="{C["orange"]}"/>')
    s.add(f'<path d="M 812 260 C 856 260, 850 355, 892 355" fill="none" stroke="{C["green"]}" stroke-width="4" '
          f'stroke-linecap="round"/>')
    s.add(f'<polygon points="898,355 884,347 884,363" fill="{C["green"]}"/>')
    s.text(800, 128, t["api"], 18, 180, color=C["orange"])
    s.text(778, 330, t["ssh"], 18, 120, color=C["green"])
    arrow(s, 1070, 182, 1070, 292, C["orange"], width=3, dash="6 8")
    s.save(out)


def safety(t, out):
    s = Svg(1280, 400, "safety", t["safety_desc"])
    cols = (C["amber"], C["green"], C["red"])
    icons = (icon_gate, icon_shield, icon_clock)
    for i, ((title, sub), col, ic) in enumerate(zip(t["safety"], cols, icons)):
        x = 40 + i * 410
        s.box(x, 40, 380, 320, stroke=col, fill=col, op=0.06, sw=1.5)
        badge(s, x + 190, 140, 56, col, ic)
        s.text(x + 190, 262, title, 30, 340)
        s.text(x + 190, 304, sub, 20, 340, color=C["dim"], weight=500)
    s.save(out)


def main():
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[2] / "assets"
    out.mkdir(parents=True, exist_ok=True)
    for lang, suffix in (("cn", ".cn"), ("en", "")):
        t = T[lang]
        hero(t, out / f"hero{suffix}.svg")
        demo(t, out / f"demo{suffix}.svg")
        arch(t, out / f"arch{suffix}.svg")
        safety(t, out / f"safety{suffix}.svg")
    print(f"wrote 8 pictures to {out}")


if __name__ == "__main__":
    main()
