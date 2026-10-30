"""Regenerate README media: uv run python docs/render_media.py [--gif | --demo NAME].

Requires a JDK. GIFs also need VHS, ttyd, ffmpeg, and Chromium.
Builds valid, unreadable, and compile-check inputs in a temporary directory.
Uses built-in engine pins; its first run may download engines into decaf's cache.
"""

from __future__ import annotations

import argparse
import hashlib
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

import httpx
from rich.console import Console
from rich.table import Table
from rich.terminal_theme import TerminalTheme
from rich.text import Text
from rich.tree import Tree

from decaf.engines import ENGINES, ensure_engine
from decaf.report_view import load_report, render_artifact, render_ending, render_problems


DOCS = Path(__file__).resolve().parent
DEMOS = ("sources", "layouts", "checks", "reports")
MAVEN_REPO = "https://repo.maven.apache.org/maven2"
MAVEN_JAR = "slf4j-api-2.0.13.jar"
MAVEN_SHA256 = "e7c2a48e8515ba1f49fa637d57b4e2f590b3f5bd97407ac699c3aa5efb1204a9"
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
    "Checked": 'package demo; public class Checked {\n'
               '    public static void read() throws java.io.IOException {\n'
               '        throw new java.io.IOException("demo");\n'
               '    }\n'
               '}\n',
    "Tool": 'package demo; public class Tool {\n'
            '    public static int twice(int value) { return value * 2; }\n'
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
    libs = work / "libs"
    libs.mkdir()
    shutil.copyfile(work / "app.war", libs / "app.war")
    with zipfile.ZipFile(libs / "tools.jar", "w") as jar:
        jar.write(classes / "demo/Tool.class", "demo/Tool.class")
    samples = work / "samples"
    samples.mkdir()
    shutil.copyfile(work / "app.war", samples / "app.war")
    (samples / "broken.jar").write_bytes(b"This is deliberately not a ZIP archive.\n")

    # A JVM method can throw a checked exception without declaring it. Rename
    # the attribute so engines see that bytecode case, then let real javac
    # diagnostics drive fallback. No engine output or report is fabricated.
    data = (classes / "demo/Checked.class").read_bytes()
    attribute = b"\x01\x00\x0aExceptions"
    if data.count(attribute) != 1:
        raise RuntimeError("Expected one Exceptions attribute name in Checked.class")
    data = data.replace(attribute, b"\x01\x00\x0aDemoThrows")
    with zipfile.ZipFile(samples / "bytecode.jar", "w") as jar:
        jar.writestr("demo/Checked.class", data)
    (work / "config.toml").write_text(
        f'update_check = false\nrepositories = ["{MAVEN_REPO}"]\n', encoding="utf-8",
    )


def run_decaf(
    work: Path, output: str, env: dict[str, str], *, merge: bool = False,
    source: str = "app.war", expected_exit: int = 0,
) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from decaf.cli import app; app()", source,
         "-o", output, "--config", "config.toml", "--no-maven",
         "--cpus", "2", "--memory", "2g", *(["--merge"] if merge else [])],
        cwd=work, env=env, check=False,
    )
    if result.returncode != expected_exit:
        raise RuntimeError(f"{source}: expected exit {expected_exit}, got {result.returncode}")


def verify_demo(output: Path) -> None:
    """Refuse media that no longer demonstrates the advertised outcomes."""
    report, _ = load_report(output)
    artifacts = {a.rel: a for a in report.artifacts}
    broken = artifacts["broken.jar"]
    checked = artifacts["bytecode.jar"]
    valid = (artifacts["app.war"], artifacts["app.war!/WEB-INF/lib/greeter.jar"])
    checks = {
        "one unreadable archive": broken.outcome == "failed" and broken.failure == "unreadable archive",
        "successful archives alongside failure": all(a.outcome == "ok" and not a.partial for a in valid),
        "successful compile checks": all(a.check and a.check["checked"] == 1 for a in valid),
        "retained sources": all((output / rel).is_file() for rel in (
            "app.war/WEB-INF/classes/demo/App.java",
            "app.war/WEB-INF/lib/greeter.jar/demo/Greeter.java",
            "bytecode.jar/demo/Checked.java",
        )),
        "a partial artifact with a javac diagnostic": checked.partial and any(
            "unreported exception java.io.IOException" in f.get("first", "") for f in checked.flagged
        ),
        "a compile-check retry": [(a.engine, a.flagged) for a in checked.attempts]
            == [("vineflower", 1), ("cfr", 1)],
        "failed and partial totals": report.totals["failed"] == 1 and report.totals["partial"] == 1,
    }
    missing = [name for name, passed in checks.items() if not passed]
    if missing:
        raise RuntimeError("Demo did not show: " + ", ".join(missing))


def verify_sources(work: Path) -> None:
    for name, cached in (("original", False), ("cached", True)):
        report, _ = load_report(work / name)
        if len(report.artifacts) != 1:
            raise RuntimeError(f"{name}: expected one Maven artifact")
        artifact = report.artifacts[0]
        if not (
            artifact.outcome == "ok" and not artifact.partial
            and artifact.method == "maven" and artifact.gav == "org.slf4j:slf4j-api:2.0.13"
            and artifact.sources_cached == cached and not artifact.attempts
        ):
            raise RuntimeError(f"{name}: expected Maven sources with cached={cached}")
    source = Path(MAVEN_JAR) / "org/slf4j/Logger.java"
    original = (work / "original" / source).read_bytes()
    if original != (work / "cached" / source).read_bytes():
        raise RuntimeError("Cached Maven sources differ from the first download")
    sources_jar = work / ".cache/decaf/sources/org.slf4j_slf4j-api_2.0.13-sources.jar"
    with zipfile.ZipFile(sources_jar) as jar:
        if original != jar.read("org/slf4j/Logger.java"):
            raise RuntimeError("Recovered Java differs from the published source")


def verify_layouts(work: Path) -> None:
    expected = {"app.war", "app.war!/WEB-INF/lib/greeter.jar", "tools.jar"}
    for name in ("mirror", "merged"):
        report, _ = load_report(work / name)
        if {a.rel for a in report.artifacts} != expected or not all(
            a.outcome == "ok" and not a.partial for a in report.artifacts
        ):
            raise RuntimeError(f"{name}: expected three complete artifacts, including the nested JAR")
        if report.totals["collisions"]:
            raise RuntimeError(f"{name}: unexpected source collision")
    for rel in (
        "app.war/WEB-INF/classes/demo/App.java",
        "app.war/WEB-INF/lib/greeter.jar/demo/Greeter.java",
        "tools.jar/demo/Tool.java",
    ):
        if (work / "mirror" / rel).read_bytes() != (
            work / "merged/src/demo" / Path(rel).name
        ).read_bytes():
            raise RuntimeError(f"Layouts disagree on source contents: {rel}")
    if (work / "mirror/app.war/WEB-INF/web.xml").read_text() != "<web-app/>\n":
        raise RuntimeError("Mirror output lost the original resource")
    if list((work / "merged").rglob("*.xml")):
        raise RuntimeError("Merged output unexpectedly contains resources")


def verify_checks(work: Path) -> None:
    report, _ = load_report(work / "checked")
    if len(report.artifacts) != 1:
        raise RuntimeError("Compile-check demo should contain one artifact")
    artifact = report.artifacts[0]
    if not (
        artifact.rel == "bytecode.jar" and artifact.partial
        and [(a.engine, a.flagged) for a in artifact.attempts] == [("vineflower", 1), ("cfr", 1)]
        and any("unreported exception java.io.IOException" in f.get("first", "") for f in artifact.flagged)
        and (work / "checked/bytecode.jar/demo/Checked.java").is_file()
    ):
        raise RuntimeError("Compile-check demo lost the diagnostic, fallback, or retained source")


def record_demo(fixtures: Path, name: str, env: dict[str, str]) -> None:
    work = fixtures / "recordings" / name
    work.mkdir(parents=True)
    shutil.copyfile(fixtures / "config.toml", work / "config.toml")
    # VHS resolves Source paths from its working directory.
    shutil.copyfile(DOCS / "demos/common.tape", work / "common.tape")
    if name == "sources":
        url = f"{MAVEN_REPO}/org/slf4j/slf4j-api/2.0.13/{MAVEN_JAR}"
        response = httpx.get(url, follow_redirects=True, timeout=60)
        response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != MAVEN_SHA256:
            raise RuntimeError("Maven demo binary failed its SHA-256 check")
        (work / MAVEN_JAR).write_bytes(response.content)
        # Install engines before recording; only sources and lookup verdicts
        # need an empty cache to demonstrate the first Maven download.
        engine_cache = work / ".cache/decaf/engines"
        engine_cache.mkdir(parents=True)
        with httpx.Client(follow_redirects=True, timeout=120) as client:
            for spec in ENGINES.values():
                jar = ensure_engine(spec, client)
                shutil.copyfile(jar, engine_cache / jar.name)
    elif name == "layouts":
        shutil.copytree(fixtures / "libs", work / "libs")
    elif name == "checks":
        shutil.copyfile(fixtures / "samples/bytecode.jar", work / "bytecode.jar")
    else:
        shutil.copytree(fixtures / "samples", work / "samples")
    subprocess.run(["vhs", str(DOCS / "demos" / f"{name}.tape")], cwd=work, env=env, check=True)
    if name == "sources":
        verify_sources(work)
    elif name == "layouts":
        verify_layouts(work)
    elif name == "checks":
        verify_checks(work)
    else:
        verify_demo(work / "out")
    shutil.copyfile(work / f"{name}.gif", DOCS / "demos" / f"{name}.gif")


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
    report, _ = load_report(work / "out")
    c = console()
    c.print("$ decaf report out", style="bold blue")
    render_ending(c, report, output=Path("out"),
                  report_path=Path("out/decaf-report.json"), verbose=False)
    c.print()
    c.print("$ decaf report out --problems", style="bold blue")
    render_problems(c, report)
    save_svg(c, "report.svg", "decaf / failures and partial results")

    c = console()
    c.print("$ decaf report out --artifact bytecode.jar", style="bold blue")
    render_artifact(c, report, ["bytecode.jar"])
    save_svg(c, "compile-check.svg", "decaf / compile check and fallback")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    recording = parser.add_mutually_exclusive_group()
    recording.add_argument("--gif", action="store_true", help="Also record all four terminal demos with VHS")
    recording.add_argument(
        "--demo", choices=DEMOS, action="append",
        help="Record only this demo; repeat to select several (skips SVGs)",
    )
    args = parser.parse_args()
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    # Recording often starts in a noninteractive shell with TERM=dumb/NO_COLOR.
    # VHS supplies a real terminal; preserve the CLI's status colors and live rows.
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env.pop("NO_COLOR", None)
    # Use this checkout even when VHS starts a fresh shell in the sample folder.
    env["PYTHONPATH"] = str(DOCS.parent / "src")
    for name in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"):
        env.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="decaf-media-") as folder:
        work = Path(folder)
        build_sample(work)
        if not args.demo:
            run_decaf(work, "mirror", env)
            run_decaf(work, "merged", env, merge=True)
            run_decaf(work, "out", env, source="samples", expected_exit=1)
            verify_demo(work / "out")
            render_layouts(work)
            render_report(work)
        for name in dict.fromkeys(args.demo or (DEMOS if args.gif else ())):
            record_demo(work, name, env)


if __name__ == "__main__":
    main()
