#!/usr/bin/env python3
"""
Invert a knocked-out logo footprint: the letters become the filled shapes
instead of the background, and the pixel signature is added as filled pixels.

    invert_logo.py AGAT-LOGO-10mm.kicad_mod out.kicad_mod [name]

KiCad polygons cannot have holes, so any hole left by inversion (the counters
inside the letters) is resolved by splitting the polygon vertically through
the hole until every piece is simply connected.
"""

import re
import sys
import uuid
from shapely.geometry import Polygon, box, MultiPolygon
from shapely.ops import unary_union

PIXEL = 0.15
MARGIN = 1

GLYPH = [
    "## # ",
    "# # #",
    "# # #",
    "# # #",
    "# # #",
]


def form_end(text, start):
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unbalanced parentheses")


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
    src, dst = sys.argv[1], sys.argv[2]
    name = sys.argv[3] if len(sys.argv) > 3 else "LOGO-INV"

    text = open(src, encoding="utf-8").read()
    polys = parse_polys(text)
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
    main()
