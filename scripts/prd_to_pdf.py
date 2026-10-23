"""Render testsprite/closebook-PRD.md to a standalone HTML file for printing.

The PDF is produced from this HTML by the browser (Page.printToPDF), so the
markdown and the PDF cannot drift: there is one source and one renderer.

Kept in the repo because it has now been written twice by hand, and a
hand-written converter is exactly the kind of thing that quietly renders a
table as a paragraph the second time.
"""
from __future__ import annotations

import html as H
import pathlib
import re
import sys

ORDERED = re.compile(r"^\d+\. ")


def to_html(md: str) -> str:
    out: list[str] = []
    in_code = in_table = False
    for line in md.splitlines():
        if line.startswith("```"):
            out.append("<pre><code>" if not in_code else "</code></pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(H.escape(line))
            continue

        esc = H.escape(line)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        esc = re.sub(r"`(.+?)`", r"<code>\1</code>", esc)

        if esc.startswith("|"):
            cells = [c.strip() for c in esc.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False

        if esc.startswith("### "):
            out.append(f"<h3>{esc[4:]}</h3>")
        elif esc.startswith("## "):
            out.append(f"<h2>{esc[3:]}</h2>")
        elif esc.startswith("# "):
            out.append(f"<h1>{esc[2:]}</h1>")
        elif esc.startswith("> "):
            out.append(f"<blockquote>{esc[2:]}</blockquote>")
        elif esc.startswith("- "):
            out.append(f"<li>{esc[2:]}</li>")
        elif ORDERED.match(esc):
            # Stripped outside the f-string: a backslash inside one is a syntax
            # error before Python 3.12.
            item = ORDERED.sub("", esc)
            out.append(f"<li>{item}</li>")
        elif esc.strip() == "---":
            out.append("<hr>")
        elif not esc.strip():
            out.append("")
        else:
            out.append(f"<p>{esc}</p>")
    if in_table:
        out.append("</table>")

    body = "\n".join(out)
    body = re.sub(r"(<li>.*?</li>\n?)+", lambda m: "<ul>" + m.group(0) + "</ul>",
                  body, flags=re.S)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ font: 13.5px/1.55 -apple-system, "Segoe UI", Roboto, sans-serif; color: #23262B; }}
h1 {{ font-size: 24px; margin: 0 0 4px; }}
h2 {{ font-size: 17px; margin: 24px 0 8px; border-bottom: 1px solid #E5E5E5; padding-bottom: 4px; }}
h3 {{ font-size: 14.5px; margin: 16px 0 6px; }}
code {{ background: #F4F4F6; padding: 1px 4px; border-radius: 4px; font-size: 12.5px; }}
pre {{ background: #F7F7F9; border: 1px solid #E8E8EC; border-radius: 8px; padding: 10px 12px; font-size: 12px; }}
table {{ border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 12.5px; }}
td {{ border: 1px solid #E8E8EC; padding: 6px 9px; vertical-align: top; }}
tr:first-child td {{ background: #F7F7F9; font-weight: 600; }}
blockquote {{ margin: 12px 0; padding: 8px 12px; background: #FBFAF8; border-left: 3px solid #D8CFC4; color: #4A4F57; }}
hr {{ border: none; border-top: 1px solid #E5E5E5; margin: 20px 0; }}
</style></head><body>{body}</body></html>"""


if __name__ == "__main__":
    src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "testsprite/closebook-PRD.md")
    dst = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/prd.html")
    html = to_html(src.read_text())
    dst.write_text(html)
    heads = html.count("<h2>")
    tables = html.count("<table>")
    print(f"  {src} -> {dst} ({len(html)} bytes, {heads} sections, {tables} tables)")
