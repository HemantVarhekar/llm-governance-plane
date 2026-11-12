"""Build the evidence page from the template and the collected data.

The page carries NO numbers of its own. It is a template with one
`__DATA__` placeholder, and every figure it shows is read from
dashboard/data.json at build time. That is the point: a figure cannot be
typed into the page by hand and still appear, so nothing on the page can
drift from what the runs actually produced.

    python dashboard/collect.py    # gather the runs into data.json
    python dashboard/build.py      # render the page from it
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
TEMPLATE = HERE / "page.template.html"
DATA = HERE / "data.json"
OUT = HERE / "index.html"


def main() -> int:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if "__DATA__" not in tpl:
        print(f"  {TEMPLATE.name} has no __DATA__ placeholder")
        return 1
    data = json.loads(DATA.read_text(encoding="utf-8"))

    # </script> inside a JSON string would end the page's script block early.
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    page = tpl.replace("__DATA__", blob)
    OUT.write_text(page, encoding="utf-8")

    print(f"  built {OUT.name}  ({len(page) / 1024:.0f} KB, "
          f"{len(blob) / 1024:.0f} KB of it measurements)")
    print(f"  dataset collected {data.get('generated_at')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
