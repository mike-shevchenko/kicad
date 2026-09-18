#!/usr/bin/env python3
"""Draw a connector on a schematic sheet, or make a footprint for one.

See --help for the modes, the specification language and the options.
"""
# Written with the help of Claude Opus 5.
# Wrappers on PATH: ki-draw-outline.cmd for cmd.exe, Fork and git; ki-draw-outline for
# cygwin and git-bash, which converts POSIX paths first. Create them with ki_install.py.

import argparse
import glob
import math
import os
import re
import shutil
import sys
import uuid

DESCRIPTION = "Draw a connector on a schematic sheet, or make a footprint for one."

EPILOG = """\
Arguments

  SOURCE  where the picture comes from, recognized by extension:
            board.kicad_pcb    footprints already placed on a board; name them as REFs
            conn.kicad_mod     one footprint from a library file
            "(rect NAME ...)"  a specification, written on the command line
            spec.txt           the same specification kept in a file
  TARGET  where the picture goes:
            sheet.kicad_sch    draw on this schematic sheet
            conn.kicad_mod     write a footprint instead, only from a specification
  REF     which footprints to draw from a board, e.g. XP1 J2 JP10

Both files may be left out inside a project directory, where the only .kicad_pcb and the only
.kicad_sch are taken. Drawing a connector from the project's board onto its sheet is then just

  ki-draw-outline XP1

Order does not matter, because each argument is identified by what it is, so these agree:

  ki-draw-outline board.kicad_pcb sheet.kicad_sch XP1 J2
  ki-draw-outline XP1 J2 sheet.kicad_sch board.kicad_pcb

Modes

  SOURCE                TARGET            what happens
  board.kicad_pcb       sheet.kicad_sch   the named footprints are drawn (one or more REFs)
  conn.kicad_mod        sheet.kicad_sch   that footprint is drawn
  (rect NAME ...)       sheet.kicad_sch   the specification is drawn
  (rect NAME ...)       conn.kicad_mod    a footprint is written

A specification may be given on the command line or kept in a .txt file, which is easier
once it runs to several lines.

What is drawn

The footprint's own F.SilkS graphics, as drawn in the Footprint Editor - the board placement's
rotation and flip are undone, so the picture shows the connector as designed. Pad numbers are
added as small text, and a letter prefix shared by a whole row is factored out into one label at
the left of that row. Each picture is parked past the right edge of the page, below anything
parked there already, with its reference above it.

Specification

  (rect NAME
    (outer W H (top_notches (xwh X W H)...)? (bottom_notches ...)?)
    (inner W H ... (offset X Y)?)?
    (pins PITCH (row PIN_NAME...)... (offset X Y)?)?
  )

Coordinates start at the top-left corner, as on the board. A notch of height H steps down into
the shape; a negative height steps up. A notch touching the left or right end of its edge has no
vertical line there. The inner outline is centered in the outer one, and the pins are centered in
the inner outline (or the outer one, if there is no inner), ignoring the notches. Either can be
shifted from there with (offset X Y); moving the inner outline moves the pins with it.

Generated footprint

The outline goes on F.SilkS and the pins become through-hole pads of 2.54 mm pin-header size,
square for pin 1 of each row and round for the rest. The origin sits on the first pin. Reference
and Value are placed at the top-left corner and the center of the outline.

Whatever is written - sheet or footprint - the previous contents are kept alongside it as a
.BAK file.
"""

SHAPES = ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc")

# Width of one character as a fraction of the font size, used to center a
# label on its pad. KiCad's stroke font advances about 0.8 of the size per
# character; too small a value pushes labels to the right.


def form_end(text, start):
    """Index just past the closing paren of the form starting at `start`.

    Parentheses inside quoted strings are ignored, so a specification or a
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


EPS = 1e-6


def sexpr(text):
    """Parse a parenthesised expression into nested lists of strings."""
    tokens = re.findall(r'\(|\)|[^\s()]+', text)
    stack, cur = [], []
    for t in tokens:
        if t == '(':
            stack.append(cur)
            cur = []
        elif t == ')':
            if not stack:
                sys.exit("spec: too many closing parentheses")
            parent = stack.pop()
            parent.append(cur)
            cur = parent
        else:
            cur.append(t)
    if stack:
        sys.exit("spec: missing closing parenthesis")
    if len(cur) != 1:
        sys.exit("spec: expected exactly one top-level form")
    return cur[0]


def take(form, tag):
    """The first sub-form with this tag, or None."""
    for item in form:
        if isinstance(item, list) and item and item[0] == tag:
            return item
    return None


def notches_of(form, tag):
    """[(x, w, h), ...] from a (top_notches (xwh ...) ...) sub-form."""
    block = take(form, tag)
    if not block:
        return []
    out = []
    for item in block[1:]:
        if not (isinstance(item, list) and item and item[0] == "xwh"):
            sys.exit(f"spec: {tag} takes (xwh X W H) items")
        x, w, h = (float(v) for v in item[1:4])
        out.append((x, w, h))
    return out


def edge_path(width, y, notches):
    """One horizontal edge, left to right, with its notches.

    A notch of height h steps to y + h; negative h steps the other way.
    A notch touching the left or right end of the edge has no vertical
    line on that side.
    """
    if not notches:
        return [(0.0, y), (width, y)]

    pts = []
    cur = 0.0
    for i, (x, w, h) in enumerate(sorted(notches)):
        if i == 0 and abs(x) < EPS:
            pts.append((0.0, y + h))  # starts on the left edge
        else:
            if i == 0:
                pts.append((0.0, y))
            pts.append((x, y))
            pts.append((x, y + h))
        pts.append((x + w, y + h))
        if abs((x + w) - width) > EPS:
            pts.append((x + w, y))
        cur = x + w
    if abs(cur - width) > EPS:
        pts.append((width, y))
    return pts


def rect_outline(form):
    """A closed polyline for an (outer ...) or (inner ...) form."""
    w, h = float(form[1]), float(form[2])
    top = edge_path(w, 0.0, notches_of(form, "top_notches"))
    bottom = edge_path(w, h, notches_of(form, "bottom_notches"))
    pts = top + list(reversed(bottom))
    pts.append(pts[0])
    return (w, h), pts


def spec_picture(text):
    """(title, shapes, pads) for a (rect NAME ...) specification."""
    form = sexpr(text)
    if not form or form[0] != "rect":
        sys.exit("spec: expected (rect NAME ...)")
    title = form[1]

    outer = take(form, "outer")
    if not outer:
        sys.exit("spec: (outer W H ...) is required")
    (ow, oh), opts = rect_outline(outer)
    shapes = [("fp_poly", opts)]

    ref_w, ref_h, ref_x, ref_y = ow, oh, 0.0, 0.0
    inner = take(form, "inner")
    if inner:
        (iw, ih), ipts = rect_outline(inner)
        dx, dy = (ow - iw) / 2, (oh - ih) / 2  # centered in the outer
        off = take(inner, "offset")
        if off:
            dx += float(off[1])
            dy += float(off[2])
        shapes.append(("fp_poly", [(x + dx, y + dy) for x, y in ipts]))
        ref_w, ref_h, ref_x, ref_y = iw, ih, dx, dy

    pads = []
    pins = take(form, "pins")
    if pins:
        pitch = float(pins[1])
        rows = [item[1:] for item in pins[2:]
            if isinstance(item, list) and item and item[0] == "row"]
        if not rows:
            sys.exit("spec: (pins PITCH (row ...) ...) needs at least one row")
        cols = max(len(r) for r in rows)
        bw, bh = (cols - 1) * pitch, (len(rows) - 1) * pitch
        x0 = ref_x + (ref_w - bw) / 2
        y0 = ref_y + (ref_h - bh) / 2
        off = take(pins, "offset")
        if off:
            x0 += float(off[1])
            y0 += float(off[2])
        for r, names in enumerate(rows):
            for c, name in enumerate(names):
                pads.append((name, x0 + c * pitch, y0 + r * pitch, 0.0, 0.0, False))

    return title, shapes, pads, (ref_x, ref_y, ref_w, ref_h)



PAD_SIZE = 1.7  # a typical 2.54 mm pin header pad
PAD_DRILL = 1.0
PAD_RRATIO = 0.25  # roundrect corner ratio for pin 1


def is_first_pin(name):
    """True for pin 1 of a row: "1", "A1", "XY1" - any non-digit prefix."""
    return re.fullmatch(r'\D*1', name) is not None


def write_footprint(title, shapes, pads, rect, path):
    """Write a .kicad_mod with the outline on F.SilkS and through-hole pads."""
    ox, oy = (pads[0][1], pads[0][2]) if pads else (0.0, 0.0)  # origin on pin 1
    rx, ry, rw, rh = rect
    vx, vy = rx + rw / 2 - ox, ry + rh / 2 - oy  # center of the outline
    txs = [p[0] for _k, pts in shapes for p in pts]
    tys = [p[1] for _k, pts in shapes for p in pts]
    ref_x = (min(txs) - ox) if txs else 0.0
    ref_y = (min(tys) - oy - 0.3) if tys else -3.0  # just above the outline

    out = [f'(footprint "{title}"',
        '\t(version 20240108)',
        '\t(generator "fp_outline_to_sch")',
        '\t(layer "F.Cu")',
        '\t(property "Reference" "REF**"',
        f'\t\t(at {ref_x:.4f} {ref_y:.4f} 0)\n\t\t(layer "F.SilkS")',
        f'\t\t(uuid "{uuid.uuid4()}")',
        '\t\t(effects\n\t\t\t(font (size 1.27 1.27) (thickness 0.15))',
        '\t\t\t(justify left bottom)\n\t\t)\n\t)',
        f'\t(property "Value" "{title}"',
        f'\t\t(at {vx:.4f} {vy:.4f} 0)\n\t\t(layer "F.Fab")',
        f'\t\t(uuid "{uuid.uuid4()}")',
        '\t\t(effects (font (size 1.8 1.8) (thickness 0.25)))\n\t)',
        '\t(attr through_hole)']

    for _kind, pts in shapes:
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            out.append('\t(fp_line\n'
                f'\t\t(start {x1 - ox:.4f} {y1 - oy:.4f})\n'
                f'\t\t(end {x2 - ox:.4f} {y2 - oy:.4f})\n'
                '\t\t(stroke (width 0.12) (type solid))\n'
                f'\t\t(layer "F.SilkS")\n\t\t(uuid "{uuid.uuid4()}")\n\t)')

    for name, x, y, _w, _h, _r in pads:
        shape = "roundrect" if is_first_pin(name) else "circle"
        extra = (f'\n\t\t(roundrect_rratio {PAD_RRATIO})' if shape == "roundrect" else "")
        out.append(f'\t(pad "{name}" thru_hole {shape}\n'
            f'\t\t(at {x - ox:.4f} {y - oy:.4f})\n'
            f'\t\t(size {PAD_SIZE} {PAD_SIZE})\n'
            f'\t\t(drill {PAD_DRILL})\n'
            '\t\t(layers "*.Cu" "*.Mask")'
            f'{extra}\n\t\t(uuid "{uuid.uuid4()}")\n\t)')

    xs = [p[0] for _k, pts in shapes for p in pts]
    ys = [p[1] for _k, pts in shapes for p in pts]
    if xs:
        out.append('\t(fp_rect\n'
            f'\t\t(start {min(xs) - ox - 0.25:.4f} {min(ys) - oy - 0.25:.4f})\n'
            f'\t\t(end {max(xs) - ox + 0.25:.4f} {max(ys) - oy + 0.25:.4f})\n'
            '\t\t(stroke (width 0.05) (type solid))\n\t\t(fill no)\n'
            f'\t\t(layer "F.CrtYd")\n\t\t(uuid "{uuid.uuid4()}")\n\t)')

    if os.path.exists(path):
        shutil.copyfile(path, path + ".BAK")
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n)\n")
    print(f"{title}: {len(pads)} pad(s), pin 1 at the origin -> {path}")


def footprint_picture(path, args):
    """(title, shapes, pads) for a .kicad_mod file."""
    text = open(path, encoding="utf-8", errors="replace").read()
    title = re.match(r'\(footprint "([^"]*)"', text.lstrip()).group(1)
    shapes = []
    for kind in SHAPES:
        for _, blk in blocks(text, kind):
            layer = re.search(r'\(layer "([^"]+)"', blk)
            if not layer or layer.group(1) != args.layer:
                continue
            pts = [(float(x), float(y)) for x, y in re.findall(
                r'\((?:xy|start|end|center|mid) (-?[\d.]+) (-?[\d.]+)', blk)]
            if pts:
                shapes.append((kind, pts))
    pads = [] if args.no_pads else pads_of(text, False)
    return title, shapes, pads


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
    """Text centered on (x, y).

    KiCad centers schematic text on its anchor when no justification is
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


def classify(words):
    """Sort free-form arguments into (source, target, refs).

    Each is identified by what it is rather than by its position: a specification by its
    leading '(', the files by extension, and whatever is left is a footprint reference. A
    .kicad_mod is the source when nothing else is, and the target once a source is known,
    which is the only shape those two modes can take.
    """
    source = target = None
    refs = []
    for word in words:
        lower = word.lower()
        if word.lstrip().startswith("(") or lower.endswith((".kicad_pcb", ".txt")):
            source = source or word
        elif lower.endswith(".kicad_sch"):
            target = target or word
        elif lower.endswith(".kicad_mod"):
            if source is None:
                source = word
            else:
                target = target or word
        else:
            refs.append(word)
    return source, target, refs


def only_file(pattern, what):
    """The single matching file in the current directory, for arguments left out."""
    hits = sorted(glob.glob(pattern))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        sys.exit("no %s here; name one, or run inside the project directory" % pattern)
    sys.exit("several %s files here (%s); name the one you mean" % (what, ", ".join(hits)))


def main():
    ap = argparse.ArgumentParser(
        prog="ki-draw-outline",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(prog, width=99))
    ap.add_argument("words", nargs="*", metavar="SOURCE TARGET REF",
        help="the source and target files and the footprint references; each is "
        "recognized by what it is, and the files may be omitted inside a "
        "project directory (see below)")
    ap.add_argument("--layer", default="F.SilkS",
        help="footprint-side layer to copy (default: F.SilkS)")
    ap.add_argument("--at", metavar="X,Y",
        help="place the outline's center here instead of off-page")
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
    if len(sys.argv) == 1:  # no arguments: show the help
        ap.print_help()
        return
    args = ap.parse_args()
    src, dst, args.refs = classify(args.words)
    if src is None:
        src = only_file("*.kicad_pcb", "board")
    if dst is None:
        dst = only_file("*.kicad_sch", "schematic")

    if src.endswith(".txt"):  # the specification kept in a file
        src = open(src, encoding="utf-8").read()
    is_spec = src.lstrip().startswith("(")

    if dst.endswith(".kicad_mod"):
        if not is_spec:
            sys.exit("a .kicad_mod can only be written from a (rect ...) specification")
        title, shapes, pads, rect = spec_picture(src)
        write_footprint(title, shapes, pads, rect, dst)
        return

    if not dst.endswith(".kicad_sch"):
        sys.exit("the target must be a .kicad_sch or a .kicad_mod")

    sheet = open(dst, encoding="utf-8", errors="replace").read()
    shutil.copyfile(dst, dst + ".BAK")

    if is_spec:
        title, shapes, pads, _rect = spec_picture(src)
        sheet = place(sheet, title, shapes, [] if args.no_pads else pads, args,
            f"{title}: {len(shapes)} graphic(s) from the "
            f"specification, {len(pads)} pin(s)")
    elif src.endswith(".kicad_mod"):
        title, shapes, pads = footprint_picture(src, args)
        sheet = place(sheet, title, shapes, pads, args,
            f"{title}: {len(shapes)} graphic(s) from {args.layer}, "
            f"{len(pads)} pad(s)")
    else:
        board = open(src, encoding="utf-8", errors="replace").read()
        if not args.refs:
            sys.exit("a .kicad_pcb source needs at least one reference")
        for ref in args.refs:
            sheet = add_picture(board, sheet, ref, args)

    open(dst, "w", encoding="utf-8", newline="\n").write(sheet)


def add_picture(board, sheet, ref, args):
    """Return the sheet with one picture appended.

    `ref` is either a footprint reference on the board, or a (rect ...)
    specification of a connector to draw from scratch.
    """
    if ref.lstrip().startswith("("):
        title, shapes, pads, _rect = spec_picture(ref)
        if args.no_pads:
            pads = []
        return place(sheet, title, shapes, pads, args,
            f"{title}: {len(shapes)} graphic(s) from the specification, "
            f"{len(pads)} pin(s)")

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
    return place(sheet, ref, shapes, pads, args,
        f"{ref}: {len(shapes)} graphic(s) from {stored}, "
        f"{len(pads)} pad(s)")


def place(sheet, title, shapes, pads, args, note):
    """Append one picture to the sheet and return it."""
    labels = factor_rows(pads, args.row_name_offset) if pads else []

    xs = [p[0] for _, pts in shapes for p in pts] + [x for _t, x, _y in labels]
    ys = [p[1] for _, pts in shapes for p in pts] + [y for _t, _x, y in labels]

    # the title goes above the picture, in the same font as the pads
    title_y = min(ys) - args.font_size * 1.6
    ys.append(title_y)
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

    if args.at:
        tx, ty = (float(v) for v in args.at.split(","))
    else:
        tx, ty = off_page(sheet, max(ys) - min(ys))

    body = emit_text(title, tx, title_y - cy + ty, args.font_size, args.label_drop)
    for kind, pts in shapes:
        body += emit(kind, [(x - cx + tx, y - cy + ty) for x, y in pts], args.stroke_width)

    for name, x, y in labels:
        body += emit_text(name, x - cx + tx, y - cy + ty, args.font_size, args.label_drop)

    sheet = sheet.rstrip()
    assert sheet.endswith(")")

    print(f"{note}, {max(xs)-min(xs):.2f} x {max(ys)-min(ys):.2f} mm, "
        f"parked off-page at ({tx:.2f}, {ty:.2f})")
    return sheet[:-1] + body + ")\n"


if __name__ == "__main__":
    main()
