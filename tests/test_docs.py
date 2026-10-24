"""The documents stay in step with the code: every command SKILL.md and the READMEs name exists, every picture a
README shows is in assets/ and nothing there is unused, the links inside a README lead to its sections, and the
disclaimer with the takedown contact stays reachable."""
import argparse
import pathlib
import re

from autopilot import cli

ROOT = pathlib.Path(__file__).resolve().parents[1]
READMES = [ROOT / "README.md", ROOT / "README.en.md"]


def commands():
    sub = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction))
    return {name: {o for a in p._actions for o in a.option_strings} for name, p in sub.choices.items()}


def test_documents_name_only_real_commands_and_options():
    known = commands()
    for path in [ROOT / "SKILL.md", *READMES]:
        for m in re.finditer(r"autopilot ([a-z]+)((?: [^`\n]*)?)", path.read_text(encoding="utf-8")):
            name = m.group(1)
            if name in ("autopilot",) or path.name.startswith("README") and name not in known:
                continue
            assert name in known, (path.name, m.group(0))
            for opt in re.findall(r"(--[a-z][a-z-]*)", m.group(2)):
                assert opt in known[name], (path.name, name, opt)


def test_readme_pictures_exist_and_none_is_unused():
    shown = set()
    for path, suffix in ((READMES[0], ".cn.svg"), (READMES[1], ".svg")):
        pics = re.findall(r'<img[^>]*src="(assets/[^"]+)"', path.read_text(encoding="utf-8"))
        assert pics == [f"assets/{n}{suffix}" for n in ("hero", "demo", "arch", "safety")], (path.name, pics)
        shown |= set(pics)
    assert {f"assets/{p.name}" for p in (ROOT / "assets").iterdir()} == shown


def test_readme_anchors():
    for path in READMES:
        text = path.read_text(encoding="utf-8")
        anchors = set(re.findall(r'<a id="([a-z-]+)"></a>', text))
        links = set(re.findall(r'<a href="#([a-z-]+)">', text)) | set(re.findall(r"\]\(#([a-z-]+)\)", text))
        assert links <= anchors and anchors
        for target in re.findall(r"\]\(([^)#:]+)\)", text):
            assert (ROOT / target).exists(), (path.name, target)


def test_disclaimer_and_takedown_contact():
    contact = "ButterFuture@proton.me"
    for path in [*READMES, ROOT / "DISCLAIMER.md", ROOT / "docs" / "DISCLAIMER.md"]:
        assert contact in path.read_text(encoding="utf-8"), path.name
    for path in READMES:
        text = path.read_text(encoding="utf-8")
        assert "docs/DISCLAIMER.md" in text and '<a id="disclaimer"></a>' in text, path.name


def test_no_trace_of_other_projects():
    for path in [ROOT / "SKILL.md", ROOT / "LICENSE", ROOT / "CHANGELOG.md", *READMES,
                 ROOT / "DISCLAIMER.md", ROOT / "docs" / "DISCLAIMER.md",
                 *(ROOT / "autopilot").iterdir(), ROOT / "docs" / "design.md"]:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            for word in ("grenbel", "Hockwang", "autogpu", "致谢", "Acknowledg"):
                assert word not in text, (path.name, word)


def test_skill_front_matter():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"---\nname: autodl-autopilot\ndescription: (Use when [^\n]+)\n---\n", text)
    assert m and len(m.group(1)) <= 500
