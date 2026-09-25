#!/usr/bin/env python3
"""
Invert a knocked-out logo footprint: the letters become the filled shapes
instead of the background, and the pixel signature is added as filled pixels.

    ki invert-logo AGAT-LOGO-10mm.kicad_mod out.kicad_mod [name]

KiCad polygons cannot have holes, so any hole left by inversion (the counters
inside the letters) is resolved by splitting the polygon vertically through
the hole until every piece is simply connected.
"""
# Written with the help of Claude Opus 5.
# Run as "ki invert-logo" through the ki launcher, or directly as "python ki_invert_logo.py".

import argparse
import re
import sys
import uuid

from ki_common_lib import die, exit_with, form_end, help_formatter

PIXEL = 0.15
MARGIN = 1

GLYPH = [
    "## # ",
    "# # #",
    "# # #",
    "# # #",
    "# # #",
    ]


def parse_polys(text):
    out = []
    for m in re.finditer(r'\(fp_poly\b', text):
        block = text[m.start():form_end(text, m.start())]
        pts = [(float(x), float(y))
            for x, y in re.findall(r'\(xy ([-\d.]+) ([-\d.]+)\)', block)]
        layer = re.search(r'\(layer "([^"]+)"', block)
        out.append((pts, layer.group(1) if layer else "F.SilkS"))
    return out


def split_holes(poly):
    """Return hole-free polygons whose union is `poly`."""
    if not poly.interiors:
        return [poly]
    x = poly.interiors[0].centroid.x
    minx, miny, maxx, maxy = poly.bounds
    out = []
    for half in (box(minx - 1, miny - 1, x, maxy + 1),
        box(x, miny - 1, maxx + 1, maxy + 1)):
        part = poly.intersection(half)
        if part.is_empty:
            continue
        for g in (part.geoms if part.geom_type == "MultiPolygon" else [part]):
            if g.geom_type == "Polygon" and g.area > 1e-9:
                out.extend(split_holes(g))
    return out


def glyph_boxes(left, top):
    out = []
    for r, row in enumerate(GLYPH):
        c = 0
        while c < len(row):
            if row[c] == '#':
                run = c
                while run < len(row) and row[run] == '#':
                    run += 1
                out.append(box(left + c * PIXEL, top + r * PIXEL,
                    left + run * PIXEL, top + (r + 1) * PIXEL))
                c = run
            else:
                c += 1
    return out


def fmt_poly(pts, layer):
    body = "".join(f"      (xy {x:.6f} {y:.6f})\n" for x, y in pts)
    return (f"  (fp_poly\n    (pts\n{body}    )\n"
        f"    (stroke (width 0) (type solid)) (fill solid)\n"
        f'    (layer "{layer}") (tstamp {uuid.uuid4()})\n  )\n')


def main():
    parser = argparse.ArgumentParser(
        prog="ki invert-logo", description=__doc__,
        formatter_class=help_formatter)
    parser.add_argument("source", metavar="IN.kicad_mod",
        help="knocked-out logo footprint to invert")
    parser.add_argument("output", metavar="OUT.kicad_mod",
        help="where the inverted footprint is written")
    parser.add_argument("name", nargs="?", default="LOGO-INV",
        help="footprint name to store (default: LOGO-INV)")
    args = parser.parse_args()
    # Imported here, not at the top, so --help works without the dependency.
    global Polygon, box, unary_union
    try:
        from shapely.geometry import Polygon, box
        from shapely.ops import unary_union
    except ImportError:
        sys.exit("this script needs shapely: python -m pip install shapely")
    src, dst, name = args.source, args.output, args.name

    text = open(src, encoding="utf-8").read()
    polys = parse_polys(text)
    if not polys:
        die("no fp_poly in %s, so there is nothing to invert" % src)
    layer = polys[0][1]

    shapes = [Polygon(p).buffer(0) for p, _ in polys]
    filled = unary_union(shapes)

    outer = max(shapes, key=lambda s: s.bounds[2] - s.bounds[0])
    minx, miny, maxx, maxy = outer.bounds
    rect = box(minx, miny, maxx, maxy)

    inverted = rect.difference(filled)

    g_right = maxx - MARGIN * PIXEL
    g_bottom = maxy - MARGIN * PIXEL
    g_left = g_right - len(GLYPH[0]) * PIXEL
    g_top = g_bottom - len(GLYPH) * PIXEL
    inverted = unary_union([inverted] + glyph_boxes(g_left, g_top))

    parts = inverted.geoms if inverted.geom_type == "MultiPolygon" else [inverted]
    pieces = []
    for p in parts:
        pieces.extend(split_holes(p))

    body = "".join(fmt_poly(list(p.exterior.coords)[:-1], layer) for p in pieces)

    out = (f'(footprint "{name}" (version 20221018) (generator pcbnew)\n'
        '  (layer "F.Cu")\n'
        f'  (descr "Inverted Agat logo with signature")\n'
        '  (tags "agat logo")\n'
        '  (attr board_only exclude_from_pos_files exclude_from_bom)\n'
        f'  (fp_text reference "G***" (at 0 {miny - 1:.3f}) (layer "F.SilkS") hide\n'
        '      (effects (font (size 1 1) (thickness 0.15)))\n'
        f'    (tstamp {uuid.uuid4()})\n  )\n'
        f'  (fp_text value "{name}" (at 0 {maxy + 1:.3f}) (layer "F.SilkS") hide\n'
        '      (effects (font (size 1 1) (thickness 0.15)))\n'
        f'    (tstamp {uuid.uuid4()})\n  )\n'
        f'{body})\n')

    open(dst, "w", encoding="utf-8", newline="\n").write(out)
    print(f"{len(pieces)} polygon(s); extent "
        f"{maxx - minx:.3f} x {maxy - miny:.3f} mm")


if __name__ == "__main__":
    exit_with(main)

