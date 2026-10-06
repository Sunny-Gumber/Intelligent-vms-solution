#!/usr/bin/env python3
"""Check browser scripts and inline handlers with Node's JavaScript parser."""

import argparse
import subprocess
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _FrontendParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.handlers = []
        self.markup_escapes = []
        self._script = None
        self._style = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            if "src" in attrs:
                raise ValueError("External scripts need an explicit syntax-check policy")
            kind = attrs.get("type", "text/javascript")
            if kind not in {"text/javascript", "application/javascript", "module"}:
                raise ValueError("Unsupported inline script type")
            self._script = (self.getpos()[0], kind, [])
        if tag == "style":
            self._style = True
        self.handlers.extend(
            (self.getpos()[0], value) for name, value in attrs.items()
            if name.startswith("on") and value is not None
        )

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            line, kind, parts = self._script
            self.scripts.append((line, kind, "".join(parts)))
            self._script = None
        if tag == "style":
            self._style = False

    def handle_data(self, data):
        if self._script is not None:
            self._script[2].append(data)
        elif not self._style and any(value in data for value in (r"\n", r"\r", r"\t")):
            self.markup_escapes.append(self.getpos()[0])


def check_frontend(html: str, node: str = "node") -> list[str]:
    """Return markup/syntax errors, using a real parser for every executable block.

    Args:
        html: Complete frontend HTML source.
        node: Node executable available in the test environment.

    Returns:
        Safe source-location diagnostics; an empty list means PASS.

    Raises:
        OSError: If Node is unavailable.
        subprocess.TimeoutExpired: If a bounded syntax check stalls.
        ValueError: If an unsupported script type or external script is added.
    """
    parser = _FrontendParser()
    parser.feed(html)
    parser.close()
    errors = [f"literal markup escape at line {line}" for line in parser.markup_escapes]
    if parser._script is not None:
        errors.append("unclosed inline script")
    if not parser.scripts:
        errors.append("no executable inline scripts found")
    units = [(line, kind, script) for line, kind, script in parser.scripts]
    units.extend((line, "text/javascript", "(function(event){\n" + code + "\n})")
                 for line, code in parser.handlers)
    for line, kind, script in units:
        result = subprocess.run(
            [node, "--check", "--input-type=" + ("module" if kind == "module" else "commonjs")],
            input=script, text=True, encoding="utf-8", capture_output=True, timeout=10, check=False,
        )
        if result.returncode:
            # Do not print source snippets: future generated source may contain secrets.
            errors.append(f"JavaScript syntax error in block at HTML line {line}")
    return errors


def main() -> int:
    """Run the frontend gate; return nonzero for any parse or markup defect."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, default=ROOT / "web" / "index.html")
    args = parser.parse_args()
    errors = check_frontend(args.html.read_text(encoding="utf-8"))
    for error in errors:
        print(error)
    if not errors:
        print("frontend_syntax_ok")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
