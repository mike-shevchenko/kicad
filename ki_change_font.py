#!/usr/bin/env python3
"""Change the font, size or thickness of every text that uses a given one. See --help."""
# Written with the help of Claude Opus 5.5.
# Run as "ki change-font" through the ki launcher, or directly as "python ki_change_font.py".

import argparse
import glob
import os
import re
import sys
from collections import Counter

from ki_common_lib import die, exit_with, help_formatter, keep_backup, shown

DESCRIPTION = "Change the font, size or thickness of every text that uses a given one."

EPILOG = """\
Usage

  ki change-font [--pcb | --sch] SRC DST [FILE]

  Every text whose font matches SRC takes each attribute DST gives, and keeps the rest.

  SRC, DST  a font, written as  [NAME] [@W[xH]] [+T]
              NAME  the font's name, as KiCad lists it; KiCad Font is the built-in one
              W, H  the character width and height in mm; H defaults to W
              T     the stroke thickness in mm
            Each part is optional, but each spec needs at least one. A name may hold any
            character but @ and +, and a name with spaces needs quotes.
  FILE      one .kicad_sch, .kicad_pcb, .kicad_sym or .kicad_mod to work on instead

  Without FILE, every .kicad_sch and .kicad_pcb in the current directory is searched;
  --pcb or --sch narrows that to the boards or the schematics.

Examples

  ki change-font @1.27 @1                 1.27 mm texts become 1 mm, font and stroke kept
  ki change-font @1.15x1 @1+0.15 --pcb    1.15 mm wide, 1 mm high: made square, stroke set
  ki change-font +0.25 +0.2 --pcb         every 0.25 mm stroke on the board becomes 0.2 mm
  ki change-font "KiCad Font" Osifont --sch
  ki change-font Arial@2 "KiCad Font@1.5+0.2" board.kicad_pcb

Matching

  A text matches when every attribute SRC names is equal; names compare ignoring case. A
  text written without a thickness, as schematic texts usually are, matches no +T.

  In a schematic, the copies of library symbols it carries are left alone: their pin names
  and graphics belong to the library, and editing the copy only makes ERC report it as
  differing. Fields of placed symbols are the schematic's own and do change. On a board,
  footprint fields and footprint texts change too.

Writing

  Files are rewritten in place, and each one's previous version is kept alongside as .BAK.
  Close them in KiCad first, or saving from there brings the old fonts back. Only the font
  lines change, and each file keeps its line endings. A board text in a TrueType font
  carries a cached outline; when that text changes, the stale cache is dropped and KiCad
  draws the text anew.
"""

DEFAULT_FONT = "KiCad Font"
SUFFIXES = (".kicad_sch", ".kicad_pcb", ".kicad_sym", ".kicad_mod")
EPSILON = 5e-7  # half a KiCad internal unit, in mm

NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
SPEC = re.compile(r"(?P<name>[^@+]*?)\s*(?:@\s*(?P<w>%s)\s*(?:[xX]\s*(?P<h>%s))?)?\s*"
    r"(?:\+\s*(?P<t>%s))?" % (NUMBER, NUMBER, NUMBER))
# Strings first, so a parenthesis inside one is not taken for structure.
TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]')
FORM_NAME = re.compile(r'[^\s()"]+')
FACE = re.compile(r'\(face\s+"((?:[^"\\]|\\.)*)"\s*\)')
SIZE = re.compile(r"\(size\s+(\S+)\s+(\S+?)\s*\)")
THICKNESS = re.compile(r"\(thickness\s+(\S+?)\s*\)")


class Font:
    """A font as a spec gives it or a file stores it; None marks what is not given."""

    def __init__(self, name=None, width=None, height=None, thickness=None):
        self.name, self.width, self.height, self.thickness = name, width, height, thickness

    def __str__(self):
        out = self.name or ""
        if self.width is not None:
            out += "@" + number(self.width)
            if not same(self.width, self.height):
                out += "x" + number(self.height)
        if self.thickness is not None:
            out += "+" + number(self.thickness)
        return out


def same(a, b):
    return a is not None and b is not None and abs(a - b) < EPSILON


def number(value):
    """A length the way KiCad writes one: no trailing zeros, no exponent."""
    return ("%.6f" % value).rstrip("0").rstrip(".")


def parse_spec(text):
    match = SPEC.fullmatch(text.strip())
    if not match or not any(match.group(part) for part in ("name", "w", "t")):
        raise argparse.ArgumentTypeError(
            "%r is not a font; give it as [NAME] [@W[xH]] [+T], for example "
            "'KiCad Font@1.27+0.15'" % text)
    width = float(match.group("w")) if match.group("w") else None
    height = float(match.group("h")) if match.group("h") else width
    thickness = float(match.group("t")) if match.group("t") else None
    if any(value is not None and value <= 0 for value in (width, height, thickness)):
        raise argparse.ArgumentTypeError("%r: sizes and thickness must be above zero" % text)
    return Font(match.group("name").strip() or None, width, height, thickness)


def forms(text):
    """Every s-expression form as [name, start, end, parent index], parents first."""
    out, stack = [], []
    for token in TOKEN.finditer(text):
        if token.group() == "(":
            name = FORM_NAME.match(text, token.end())
            out.append([name.group() if name else "", token.start(), None,
                stack[-1] if stack else -1])
            stack.append(len(out) - 1)
        elif token.group() == ")":
            if not stack:
                die("unbalanced parentheses at line %d"
                    % (text.count("\n", 0, token.start()) + 1))
            out[stack.pop()][2] = token.end()
    if stack:
        die("unbalanced parentheses: a form at line %d is never closed"
            % (text.count("\n", 0, out[stack[-1]][1]) + 1))
    return out


def stored_font(body):
    face = FACE.search(body)
    size = SIZE.search(body)
    thickness = THICKNESS.search(body)
    # KiCad writes (size HEIGHT WIDTH).
    return Font(face.group(1) if face else DEFAULT_FONT,
        float(size.group(2)) if size else None,
        float(size.group(1)) if size else None,
        float(thickness.group(1)) if thickness else None)


def matches(font, spec):
    if spec.name is not None and spec.name.lower() != font.name.lower():
        return False
    if spec.width is not None and not (same(spec.width, font.width)
            and same(spec.height, font.height)):
        return False
    return spec.thickness is None or same(spec.thickness, font.thickness)


def insert(body, anchor, child, after, newline):
    """Add a child form beside an existing one, laid out as the font form already is."""
    if "\n" in body:
        line_start = body.rfind("\n", 0, anchor.start()) + 1
        indent = body[line_start:anchor.start()]
        if after:
            return body[:anchor.end()] + newline + indent + child + body[anchor.end():]
        return body[:anchor.start()] + child + newline + indent + body[anchor.start():]
    if after:
        return body[:anchor.end()] + " " + child + body[anchor.end():]
    return body[:anchor.start()] + child + " " + body[anchor.start():]


def remove(body, form):
    """Drop a child form together with the line or the space it occupies."""
    line_start = body.rfind("\n", 0, form.start()) + 1
    if line_start and not body[line_start:form.start()].strip():
        cut = line_start - 1  # the newline that opens the form's own line
        if body[cut - 1:cut] == "\r":
            cut -= 1
        return body[:cut] + body[form.end():]
    start = form.start()
    while start and body[start - 1] in " \t":
        start -= 1
    return body[:start] + body[form.end():]


def restyled(body, target, newline):
    """The font form with each attribute the target gives put in place."""
    if target.width is not None:
        size = SIZE.search(body)
        if not size:
            return body  # a font form without a size is not a text's font
        body = (body[:size.start()] + "(size %s %s)" % (number(target.height),
            number(target.width)) + body[size.end():])
    if target.thickness is not None:
        form = "(thickness %s)" % number(target.thickness)
        old = THICKNESS.search(body)
        if old:
            body = body[:old.start()] + form + body[old.end():]
        elif SIZE.search(body):
            body = insert(body, SIZE.search(body), form, True, newline)
    if target.name is not None:
        old = FACE.search(body)
        if target.name.lower() == DEFAULT_FONT.lower():
            if old:
                body = remove(body, old)
        else:
            escaped = target.name.replace("\\", "\\\\").replace('"', '\\"')
            form = '(face "%s")' % escaped
            if old:
                body = body[:old.start()] + form + body[old.end():]
            elif SIZE.search(body):
                body = insert(body, SIZE.search(body), form, False, newline)
    return body


def whole_lines(text, start, end, newline):
    """The span to delete for a form, widened to its own lines when it stands alone."""
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip():
        while start and text[start - 1] in " \t":
            start -= 1
        return start, end
    if text.startswith(newline, end):
        return line_start, end + len(newline)
    return line_start, end


def change_file(path, source, target, fonts_seen):
    """Rewrite one file; returns (texts changed, texts already as wanted, cache matches)."""
    text = open(path, encoding="utf-8", newline="").read()
    newline = "\r\n" if "\r\n" in text else "\n"
    tree = forms(text)
    children = {}
    for index, (_name, _start, _end, parent) in enumerate(tree):
        children.setdefault(parent, []).append(index)
    skip_caches = path.endswith(".kicad_sch")

    def in_cache(index):
        while index >= 0:
            if tree[index][0] == "lib_symbols":
                return True
            index = tree[index][3]
        return False

    edits, changed, unchanged, cached = [], 0, 0, 0
    for name, start, end, parent in tree:
        if name != "font" or not SIZE.search(text, start, end):
            continue
        body = text[start:end]
        font = stored_font(body)
        if skip_caches and in_cache(parent):
            cached += matches(font, source)
            continue
        fonts_seen[str(font)] += 1
        if not matches(font, source):
            continue
        new = restyled(body, target, newline)
        if new == body:
            unchanged += 1
            continue
        edits.append((start, end, new))
        changed += 1
        item = tree[parent][3] if parent >= 0 else -1
        for child in children.get(item, ()):
            if tree[child][0] == "render_cache":
                cut = whole_lines(text, tree[child][1], tree[child][2], newline)
                edits.append(cut + ("",))
    if edits:
        for start, end, new in sorted(edits, reverse=True):
            text = text[:start] + new + text[end:]
        keep_backup(path)
        open(path, "w", encoding="utf-8", newline="").write(text)
    return changed, unchanged, cached


def project_files(boards, schematics):
    found = []
    if schematics:
        found += sorted(glob.glob("*.kicad_sch"))
    if boards:
        found += sorted(glob.glob("*.kicad_pcb"))
    if not found:
        kinds = {(True, True): ".kicad_sch or .kicad_pcb", (True, False): ".kicad_pcb",
            (False, True): ".kicad_sch"}[(boards, schematics)]
        die("no %s in %s; run inside the project directory, or name a FILE"
            % (kinds, shown(os.getcwd())))
    return found


def main():
    ap = argparse.ArgumentParser(prog="ki change-font", description=DESCRIPTION,
        epilog=EPILOG, formatter_class=help_formatter)
    ap.add_argument("source", metavar="SRC", type=parse_spec,
        help="the font to look for: [NAME] [@W[xH]] [+T]")
    ap.add_argument("target", metavar="DST", type=parse_spec,
        help="what the matching texts become, in the same form")
    ap.add_argument("file", metavar="FILE", nargs="?",
        help="a single file to work on instead of the project's")
    ap.add_argument("--pcb", action="store_true",
        help="work only on the project's .kicad_pcb")
    ap.add_argument("--sch", action="store_true",
        help="work only on the project's .kicad_sch files")
    if len(sys.argv) == 1:
        ap.print_help()
        return
    args = ap.parse_args()

    if args.file:
        if args.pcb or args.sch:
            die("give either FILE or --pcb/--sch, not both")
        if not args.file.endswith(SUFFIXES):
            die("%s is not a KiCad schematic, board, symbol library or footprint"
                % shown(args.file))
        files = [args.file]
    else:
        everything = not (args.pcb or args.sch)
        files = project_files(args.pcb or everything, args.sch or everything)

    fonts_seen = Counter()
    total = unchanged_total = cached_total = 0
    for path in files:
        changed, unchanged, cached = change_file(path, args.source, args.target, fonts_seen)
        total += changed
        unchanged_total += unchanged
        cached_total += cached
        if changed:
            print("%s: %d text(s) changed" % (shown(path), changed))
    if cached_total:
        print("%d matching text(s) in the schematic's library symbol copies left alone"
            % cached_total)
    if total or unchanged_total:
        if unchanged_total:
            print("%d matching text(s) already as wanted" % unchanged_total)
        print("%d text(s) changed from %s to %s" % (total, args.source, args.target))
        return
    in_use = "".join("\n  %s, %d text(s)" % (font, count)
        for font, count in fonts_seen.most_common())
    die("no text uses %s in %s; fonts in use:%s"
        % (args.source, ", ".join(shown(path) for path in files), in_use or " none"))


if __name__ == "__main__":
    exit_with(main)
