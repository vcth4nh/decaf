"""Regenerate README media: uv run python docs/render_media.py [--gif].

Requires a JDK. The GIF also needs VHS, ttyd, ffmpeg, and Chromium.
Builds sample inputs in a temporary directory and uses the built-in
Vineflower pin. Its first run may download the engine into decaf's cache.
"""

from __future__ import annotations

import argparse
from html import escape
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from rich.console import Console
from rich.table import Table
from rich.terminal_theme import TerminalTheme
from rich.text import Text
from rich.tree import Tree

from decaf.report_view import load_report, render_ending, render_memory


DOCS = Path(__file__).resolve().parent
THEME = TerminalTheme(
    (30, 30, 46), (205, 214, 244),
    [(69, 71, 90), (243, 139, 168), (166, 227, 161), (249, 226, 175),
     (137, 180, 250), (203, 166, 247), (148, 226, 213), (205, 214, 244)],
)
SOURCES = {
    "Greeter": 'package demo; public class Greeter {\n'
               '    public static String greet(String name) { return "Hello, " + name; }\n'
               '}\n',
    "App": 'package demo; public class App {\n'
           '    public static void main(String[] args) {\n'
           '        System.out.println(Greeter.greet("decaf"));\n'
           '    }\n'
           '}\n',
}


def build_sample(work: Path) -> None:
    source = work / "source" / "demo"
    source.mkdir(parents=True)
    for name, text in SOURCES.items():
        (source / f"{name}.java").write_text(text, encoding="utf-8")
    classes = work / "classes"
    subprocess.run(
        ["javac", "--release", "11", "-d", str(classes),
         *(str(p) for p in sorted(source.glob("*.java")))], check=True,
    )
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as jar:
        jar.write(classes / "demo/Greeter.class", "demo/Greeter.class")
    with zipfile.ZipFile(work / "app.war", "w") as war:
        war.write(classes / "demo/App.class", "WEB-INF/classes/demo/App.class")
        war.writestr("WEB-INF/lib/greeter.jar", nested.getvalue())
        war.writestr("WEB-INF/web.xml", "<web-app/>\n")
    (work / "config.toml").write_text("", encoding="utf-8")


def run_decaf(work: Path, output: str, env: dict[str, str], *, merge: bool = False) -> None:
    subprocess.run(
        [sys.executable, "-c", "from decaf.cli import app; app()", "app.war",
         "-o", output, "--config", "config.toml", "--no-maven", "--no-fallback",
         "--cpus", "2", "--memory", "2g", *(["--merge"] if merge else [])],
        cwd=work, env=env, check=True,
    )


def console() -> Console:
    return Console(file=io.StringIO(), width=100, record=True, color_system="truecolor")


def save_svg(c: Console, name: str, title: str) -> None:
    svg = c.export_svg(title=title, theme=THEME, unique_id=Path(name).stem)
    # GitHub's image renderer blocks remote fonts; keep the assets self-contained.
    svg = re.sub(r"\s*@font-face\s*\{[^}]*\}", "", svg)
    svg = svg.replace("Fira Code, monospace", '"DejaVu Sans Mono", monospace')
    width, height = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).groups()
    svg = svg.replace('<svg ', f'<svg role="img" width="{width}" height="{height}" ', 1)
    svg = svg.replace('    <!-- Generated', f'    <title>{escape(title)}</title>\n    <!-- Generated', 1)
    svg = "\n".join(line.rstrip() for line in svg.splitlines()) + "\n"
    (DOCS / name).write_text(svg, encoding="utf-8")


def directory_tree(path: Path) -> Tree:
    tree = Tree(Text(path.name + "/", style="bold blue"), guide_style="dim")

    def add(parent: Tree, folder: Path) -> None:
        for entry in sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if entry.is_dir():
                add(parent.add(Text(entry.name + "/", style="blue")), entry)
            else:
                style = "green" if entry.suffix == ".java" else "yellow" if entry.suffix == ".xml" else "dim"
                parent.add(Text(entry.name, style=style))

    add(tree, path)
    return tree


def render_layouts(work: Path) -> None:
    c = console()
    c.print("One sample app.war, decompiled in both output layouts.")
    c.print()
    table = Table.grid(padding=(0, 4), expand=True)
    table.add_column(ratio=1)
    table.add_column(ratio=1)
    table.add_row("[bold]Mirror (default)[/]", "[bold]Merged (--merge)[/]")
    table.add_row("decaf app.war -o mirror", "decaf app.war -o merged --merge")
    table.add_row("", "")
    table.add_row(directory_tree(work / "mirror"), directory_tree(work / "merged"))
    c.print(table)
    c.print()
    c.print("[green]Java sources[/]   [yellow]Original resource[/]   [blue]Directory[/]")
    save_svg(c, "output-layouts.svg", "decaf / output layouts")


def render_report(work: Path) -> None:
    report, _ = load_report(work / "mirror")
    c = console()
    c.print("$ decaf report mirror", style="bold blue")
    render_ending(c, report, output=Path("mirror"),
                  report_path=Path("mirror/decaf-report.json"), verbose=False)
    c.print()
    c.print("$ decaf report mirror --memory", style="bold blue")
    render_memory(c, report)
    save_svg(c, "report.svg", "decaf / report excerpts")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gif", action="store_true", help="Also record docs/demo.tape with VHS")
    args = parser.parse_args()
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    # Use this checkout even when VHS starts a fresh shell in the sample folder.
    env["PYTHONPATH"] = str(DOCS.parent / "src")
    for name in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"):
        env.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="decaf-media-") as folder:
        work = Path(folder)
        build_sample(work)
        run_decaf(work, "mirror", env)
        run_decaf(work, "merged", env, merge=True)
        render_layouts(work)
        render_report(work)
        if args.gif:
            subprocess.run(["vhs", str(DOCS / "demo.tape")], cwd=work, env=env, check=True)
            shutil.copyfile(work / "demo.gif", DOCS / "demo.gif")


if __name__ == "__main__":
    main()
