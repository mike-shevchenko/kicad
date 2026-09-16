#!/usr/bin/env python3
"""Copy a footprint's graphics from the board onto the schematic, 1:1."""

import argparse
import math
import re
import shutil
import sys
import uuid

DESCRIPTION = "Copy a footprint's graphics from the board onto the schematic, 1:1."

EPILOG = """\
The footprint's own F.SilkS graphics (as drawn in the Footprint Editor,
without the board placement's rotation or flip) are added to the sheet as
schematic graphics at the same millimetre scale, with the pad numbers as
small text. A shared letter prefix within a row is factored out into one
label at the left of that row.

The sheet is rewritten in place; the previous contents are kept as
sheet.kicad_sch.BAK.
"""

SHAPES = ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc")

# Width of one character as a fraction of the font size, used to centre a
# label on its pad. KiCad's stroke font advances about 0.8 of the size per
# character; too small a value pushes labels to the right.


def form_end(text, start):
    """Index just past the closing paren of the form starting at `start`.

    Parentheses inside quoted strings are ignored, so a description or a
    text item containing one does not throw the count off.
    """
    depth = 0
    i = start
    in_string = False
    while i < len(text):
        c = text[i]
        if in_string:
            if c == '\\':
                i += 2
                continue
            if c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    line = text.count("\n", 0, start) + 1
    raise ValueError(f"unbalanced parentheses in the form starting at line {line}")


def blocks(text, tag):
    for m in re.finditer(r'\(' + tag + r'[\s(]', text):
        a = m.start()
        yield a, text[a:form_end(text, a)]


def find_footprint(board, ref):
    for a, blk in blocks(board, "footprint"):
        m = re.search(r'\(property "Reference" "([^"]*)"', blk)
        if m and m.group(1) == ref:
            return blk
    sys.exit(f"no footprint with reference {ref}")


def find_symbol(sheet, ref):
    for a, blk in blocks(sheet, "symbol"):
        m = re.search(r'\(property "Reference" "([^"]*)"', blk)
        if m and m.group(1) == ref:
            at = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)', blk)
            return float(at.group(1)), float(at.group(2))
    return None


def unflip(x, y, flip):
    """Footprint-local coordinates as drawn in the Footprint Editor.

    A footprint placed on the back is stored mirrored about the X axis, so
    the stored y is negated to recover the as-drawn shape. The placement
    rotation is ignored: the picture shows the connector as designed.
    """
    return x, (-y if flip else y)


PAPER = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297),
         "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841),
         "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8)}


def off_page(sheet, height):
    """A free spot just past the right edge, below anything already parked."""
    paper = re.search(r'\(paper "([^"]+)"(\s+portrait)?', sheet)
    name = paper.group(1) if paper else "A4"
    w, h = PAPER.get(name, (297, 210))
    if paper and paper.group(2):  # portrait swaps the dimensions
        w, h = h, w
    if name == "User":  # custom size follows the name
        m = re.search(r'\(paper "User" ([\d.]+) ([\d.]+)\)', sheet)
        if m:
            w, h = float(m.group(1)), float(m.group(2))
    used = [float(y) for x, y in re.findall(
        r'\(xy ((?:[\d.]+)) ([-\d.]+)\)', sheet) if float(x) > w + 2]
    top = (max(used) + 15) if used else 20
    return w + 20, top + height / 2


def pads_of(fp, flip):
    """(name, cx, cy, w, h, round?) for every pad, in as-drawn coordinates."""
    out = []
    for _, blk in blocks(fp, "pad"):
        name = re.match(r'\(pad "([^"]*)"', blk).group(1)
        shape = blk.split("\n")[0].split()[-1]
        at = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)', blk)
        size = re.search(r'\(size ([\d.]+) ([\d.]+)\)', blk)
        if not at or not size:
            continue
        x, y = unflip(float(at.group(1)), float(at.group(2)), flip)
        w, h = float(size.group(1)), float(size.group(2))
        out.append((name, x, y, w, h, shape in ("circle", "oval")))
    return out


def factor_rows(pads, row_name_offset):
    """Strip a shared letter prefix from each row, and label the row once.

    Pads sharing a y coordinate form a row. If every name in the row starts
    with the same letters and has something left after them, the prefix is
    moved out to a single label at the left of the row.
    """
    rows = {}
    for name, x, y, w, h, r in pads:
        rows.setdefault(round(y, 2), []).append((name, x, y))

    labels = []
    for y, row in rows.items():
        names = [n for n, _x, _y in row]
        prefix = ""
        if len(row) > 1:
            for i in range(min(len(n) for n in names)):
                c = names[0][i]
                if not c.isalpha() or any(n[i] != c for n in names):
                    break
                if all(len(n) > i + 1 for n in names):
                    prefix += c
                else:
                    break
        if prefix:
            for n, x, _y in row:
                labels.append((n[len(prefix):], x, y))
            labels.append((prefix, min(x for _n, x, _y in row) - row_name_offset, y))
        else:
            labels.extend((n, x, y) for n, x, _y in row)
    return labels


def emit_text(s, x, y, size, label_drop):
    """Text centred on (x, y).

    KiCad centres schematic text on its anchor when no justification is
    given, so no manual offset is needed. `label_drop` is kept as a small
    optional nudge downward, in font-size units.
    """
    ty = y + size * label_drop
    return (f'\t(text "{s}"\n\t\t(exclude_from_sim no)\n'
            f'\t\t(at {x:.4f} {ty:.4f} 0)\n'
            f'\t\t(effects\n\t\t\t(font\n\t\t\t\t(size {size} {size})\n\t\t\t)\n'
            f'\t\t)\n'
            f'\t\t(uuid "{uuid.uuid4()}")\n\t)\n')


def emit(kind, pts, stroke_width):
    u = uuid.uuid4()
    stroke = f'(stroke (width {stroke_width}) (type default))'
    if kind == "fp_circle":
        (cx, cy), (ex, ey) = pts[0], pts[1]
        r = math.hypot(ex - cx, ey - cy)
        return (f'\t(circle\n\t\t(center {cx:.4f} {cy:.4f})\n\t\t(radius {r:.4f})\n'
                f'\t\t{stroke}\n\t\t(fill (type none))\n\t\t(uuid "{u}")\n\t)\n')
    if kind == "fp_arc":
        (sx, sy), (mx, my), (ex, ey) = pts[:3]
        return (f'\t(arc\n\t\t(start {sx:.4f} {sy:.4f})\n\t\t(mid {mx:.4f} {my:.4f})\n'
                f'\t\t(end {ex:.4f} {ey:.4f})\n\t\t{stroke}\n\t\t(fill (type none))\n'
                f'\t\t(uuid "{u}")\n\t)\n')
    if kind == "fp_rect":
        (sx, sy), (ex, ey) = pts[0], pts[1]
        return (f'\t(rectangle\n\t\t(start {sx:.4f} {sy:.4f})\n\t\t(end {ex:.4f} {ey:.4f})\n'
                f'\t\t{stroke}\n\t\t(fill (type none))\n\t\t(uuid "{u}")\n\t)\n')
    body = "".join(f"\t\t\t(xy {x:.4f} {y:.4f})\n" for x, y in pts)
    return (f'\t(polyline\n\t\t(pts\n{body}\t\t)\n\t\t{stroke}\n\t\t(uuid "{u}")\n\t)\n')


def main():
    ap = argparse.ArgumentParser(
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", help="the .kicad_pcb to read the footprint from")
    ap.add_argument("sheet", help="the .kicad_sch to add the picture to")
    ap.add_argument("refs", nargs="+", metavar="REF",
                    help="references of the footprints, e.g. J2 J6 JP10")
    ap.add_argument("--layer", default="F.SilkS",
                    help="footprint-side layer to copy (default: F.SilkS)")
    ap.add_argument("--at", metavar="X,Y",
                    help="place the outline's centre here instead of off-page")
    ap.add_argument("--no-pads", action="store_true",
                    help="draw the outline only, without pad numbers")
    ap.add_argument("--font-size", type=float, default=1.0, metavar="MM",
                    help="pad number text height in mm (default: 1.0)")
    ap.add_argument("--label-drop", type=float, default=0.3, metavar="F",
                    help="extra downward nudge of text, in font-size units "
                         "(default: 0.3)")
    ap.add_argument("--row-name-offset", type=float, default=3.6, metavar="MM",
                    help="distance from the first pad to the row letter "
                         "(default: 3.6)")
    ap.add_argument("--dups", action="store_true",
                    help="keep duplicated footprint graphics instead of "
                         "drawing each shape once")
    ap.add_argument("--stroke-width", default="0.15", metavar="MM",
                    help="line width on the schematic (default: 0.15)")
    args = ap.parse_args()

    board = open(args.board, encoding="utf-8", errors="replace").read()
    sheet = open(args.sheet, encoding="utf-8", errors="replace").read()

    shutil.copyfile(args.sheet, args.sheet + ".BAK")
    for ref in args.refs:
        sheet = add_picture(board, sheet, ref, args)
    open(args.sheet, "w", encoding="utf-8", newline="\n").write(sheet)


def add_picture(board, sheet, ref, args):
    """Return the sheet with one footprint's picture appended."""
    fp = find_footprint(board, ref)
    at = re.search(r'\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)', fp)
    angle = float(at.group(3) or 0)
    flip = re.search(r'\(footprint "[^"]+"\s*\n\s*\(layer "B\.', fp) is not None

    # a back-side footprint stores its F.SilkS items on B.SilkS
    stored = args.layer if not flip else (
        "B." + args.layer[2:] if args.layer[:2] == "F." else "F." + args.layer[2:])

    shapes = []
    for kind in SHAPES:
        for _, blk in blocks(fp, kind):
            layer = re.search(r'\(layer "([^"]+)"', blk)
            if not layer or layer.group(1) != stored:
                continue
            pts = [(float(x), float(y)) for x, y in re.findall(
                r'\((?:xy|start|end|center|mid) (-?[\d.]+) (-?[\d.]+)', blk)]
            if pts:
                shapes.append((kind, [unflip(x, y, flip) for x, y in pts]))

    if not args.dups:
        seen, unique = set(), []
        for kind, pts in shapes:
            key = (kind, tuple(pts))
            if key in seen:
                continue
            seen.add(key)
            unique.append((kind, pts))
        if len(unique) != len(shapes):
            print(f"{ref}: ignored {len(shapes) - len(unique)} duplicated "
                  f"shape(s) in the footprint", file=sys.stderr)
        shapes = unique

    if not shapes:
        sys.exit(f"{ref}: nothing on {args.layer}")

    pads = [] if args.no_pads else pads_of(fp, flip)
    labels = factor_rows(pads, args.row_name_offset) if pads else []

    xs = [p[0] for _, pts in shapes for p in pts] + [x for _t, x, _y in labels]
    ys = [p[1] for _, pts in shapes for p in pts] + [y for _t, _x, y in labels]

    # The reference goes above the picture, in the same font as the pads.
    title_y = min(ys) - args.font_size * 1.6
    ys.append(title_y)
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

    if args.at:
        tx, ty = (float(v) for v in args.at.split(","))
    else:
        tx, ty = off_page(sheet, max(ys) - min(ys))

    body = emit_text(ref, tx, title_y - cy + ty,
                     args.font_size, args.label_drop)
    for kind, pts in shapes:
        body += emit(kind, [(x - cx + tx, y - cy + ty) for x, y in pts],
                     args.stroke_width)

    for name, x, y in labels:
        body += emit_text(name, x - cx + tx, y - cy + ty,
                          args.font_size, args.label_drop)

    sheet = sheet.rstrip()
    assert sheet.endswith(")")

    print(f"{ref}: {len(shapes)} graphic(s) from {stored}, {len(pads)} pad(s), "
          f"{max(xs)-min(xs):.2f} x {max(ys)-min(ys):.2f} mm, "
          f"parked off-page at ({tx:.2f}, {ty:.2f})")
    return sheet[:-1] + body + ")\n"


if __name__ == "__main__":
    main()
