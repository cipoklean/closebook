"""Rewrite em dashes in user-facing STRING LITERALS, leaving comments alone.

Edits by AST offset, so a `# ... — ...` comment is never touched: the brief says
comments are out of scope, and a blind file replace cannot tell the two apart.

The dash is replaced by punctuation chosen from what follows it, never by
another dash. That is the whole point: "held — not paid" wants a comma,
"Closebook — Money out" wants a colon, and "the draft is gone — nothing moved"
wants a full stop and a capital.

  python scripts/no_dashes.py --dry-run     # show every change
  python scripts/no_dashes.py               # apply
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

EM, EN = "\u2014", "\u2013"
ROOT = pathlib.Path(__file__).resolve().parents[1]

#: A heading followed by its explanation, or a label followed by a value.
LABEL_AFTER = re.compile(r"^(?:[a-z0-9(]|$)")


def rewrite(text: str) -> str:
    """Choose punctuation for each dash from the words around it."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch not in (EM, EN):
            out.append(ch)
            i += 1
            continue
        if ch == EN:
            before = text[i - 1] if i else ""
            after = text[i + 1] if i + 1 < n else ""
            if before.isdigit() and after.isdigit():
                out.append(ch)          # a numeric range keeps its dash
                i += 1
                continue

        # what precedes, ignoring the spaces we are about to swallow
        while out and out[-1] == " ":
            out.pop()          # no space BEFORE a colon, full stop or comma
        left = "".join(out)
        right = text[i + 1:].lstrip(" ")
        if right.startswith(" "):
            right = right[1:]

        if not right:
            out.append(".")
            i += 1
            continue

        first = right[0]
        if first.isupper():
            # a new sentence: full stop, capital already there
            out.append(". ")
        elif first.isdigit() and len(left) > 0 and not left.rstrip()[-1:].isdigit():
            out.append(": ")
        elif LABEL_AFTER.match(right) and left and not left.endswith((',', ':', '.', '(')):
            # a label or heading followed by a lowercase explanation
            out.append(": ")
        else:
            out.append(", ")
        # swallow the spaces around the dash so we do not emit two
        while i < n and text[i] == " ":
            i += 1
            break
        i += 1
        while i < n and text[i] == " ":
            i += 1
    return "".join(out)


def string_spans(path: pathlib.Path) -> list[tuple[int, int, int, int]]:
    """(lineno, col, end_lineno, end_col) for every non-docstring string literal."""
    src = path.read_text()
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docs.add(id(body[0].value))
    spans = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs
                and (EM in node.value or EN in node.value)):
            spans.append((node.lineno, node.col_offset,
                          node.end_lineno, node.end_col_offset))
    return spans


def patch_file(path: pathlib.Path, dry: bool) -> list[str]:
    src = path.read_text()
    lines = src.splitlines(keepends=True)
    # byte offsets do not equal char offsets for non-ASCII, so work in chars
    starts = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line)

    def index(lineno: int, col: int) -> int:
        """Character offset of a 1-based line / 0-based utf-8 byte column."""
        raw = lines[lineno - 1].encode()
        # AST columns are utf-8 byte offsets; convert to characters
        return starts[lineno - 1] + len(raw[:col].decode("utf-8"))

    edits = []
    for l1, c1, l2, c2 in string_spans(path):
        a, b = index(l1, c1), index(l2, c2)
        original = src[a:b]
        # the literal's own quotes are part of the span; leave them
        quote = ""
        for q in ('"""', "'''", '"', "'"):
            if original.startswith(q):
                quote = q
                break
        if quote:
            inner = original[len(quote):-len(quote)]
            new_inner = rewrite(inner)
            replacement = quote + new_inner + quote
        else:
            # An f-string's TEXT PART: the span is the raw text between braces,
            # with no quotes of its own. Splicing it directly is valid, and it is
            # the only way to reach sentences like f"{a} — {b}".
            inner = original
            new_inner = rewrite(inner)
            replacement = new_inner
        if new_inner != inner:
            edits.append((a, b, replacement,
                          f"{path.name}:{l1} {inner[:80]!r} -> {new_inner[:80]!r}"))

    for a, b, replacement, note in sorted(edits, reverse=True):
        src = src[:a] + replacement + src[b:]
        if dry:
            print("   ", note)
    if edits and not dry:
        path.write_text(src)
    return [e[3] for e in edits]


def main() -> int:
    dry = "--dry-run" in sys.argv
    total = 0
    for path in sorted((ROOT / "backend" / "app").rglob("*.py")):
        changed = patch_file(path, dry)
        if changed:
            total += len(changed)
            print(f"  {path.relative_to(ROOT)}: {len(changed)}")
    print(f"{'would change' if dry else 'changed'} {total} string literal(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
