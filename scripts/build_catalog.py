"""Extract factual API names/types/ranges, without copying manual prose.

Usage: python scripts/build_catalog.py .research/_elements.html
Source: https://lua.uvi.net/_elements.html (UVI official documentation).
"""

import json
import sys
from pathlib import Path

from bs4 import BeautifulSoup

soup = BeautifulSoup(Path(sys.argv[1]).read_text(encoding="utf8"), "html.parser")
modules = []
category = ""
for heading in soup.find_all(["h1", "h2"]):
    if heading.name == "h1":
        category = heading.get_text(" ", strip=True)
        continue
    anchor = heading.find("a", class_="anchor")
    if not anchor:
        continue
    table = None
    for sibling in heading.find_next_siblings():
        if sibling.name in {"h1", "h2"}:
            break
        if sibling.name == "table":
            table = sibling
            break
    if table is None:
        continue
    parameters = []
    for row in table.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td", recursive=False)]
        if len(cells) < 8:
            continue
        # Names and numeric metadata only; no descriptions, examples or paragraphs.
        parameters.append(
            dict(zip(["name", "display_name", "type", "min", "max", "default", "unit"], cells[:7]))
        )
    modules.append(
        {
            "type": anchor["id"],
            "name": heading.get_text(" ", strip=True),
            "category": category,
            "parameters": parameters,
        }
    )
target = Path("src/falcon_mcp/data/parameters.json")
target.write_text(
    json.dumps(
        {"source": "https://lua.uvi.net/_elements.html", "retrieved": "2026-10-20", "modules": modules},
        ensure_ascii=False,
        separators=(",", ":"),
    ),
    encoding="utf8",
)
print(f"{len(modules)} module types; {sum(len(m['parameters']) for m in modules)} parameters")
