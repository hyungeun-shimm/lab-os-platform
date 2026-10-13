"""Syntax-check inline and shared JavaScript without a build step."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import sys


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attrs = dict(attrs)
            self.active = not attrs.get("src") and attrs.get("type", "") not in {"application/json", "application/ld+json"}
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.scripts.append("".join(self.parts))
            self.active = False


def main():
    root = Path(__file__).resolve().parents[1]
    count = 0
    for path in sorted((root / "modules").rglob("*")):
        if path.suffix == ".html":
            parser = Scripts()
            parser.feed(path.read_text())
            scripts = parser.scripts
        elif path.suffix == ".js":
            scripts = [path.read_text()]
        else:
            continue
        for index, source in enumerate(scripts):
            result = subprocess.run(["node", "--check", "-"], input=source, text=True, capture_output=True)
            if result.returncode:
                print(f"{path.relative_to(root)} script {index + 1}:\n{result.stderr}", file=sys.stderr)
                return 1
            count += 1
    print(f"JavaScript syntax verified: {count} scripts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
