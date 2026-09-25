#!/usr/bin/env python3
"""
Cut a pixel-font glyph out of a filled polygon in a KiCad footprint.

    ki cut-signature AGAT-LOGO-10mm.kicad_mod out.kicad_mod

The glyph is placed in the bottom-right corner of the largest polygon,
one pixel clear of the right and bottom edges.

KiCad polygons have no holes, so the cut is done in two parts:
  * a rectangular notch R is removed from the polygon's corner by editing
    its outline directly, which preserves the existing keyhole structure;
  * the part of R that is not glyph is emitted as new polygons.
The union of the two is the original shape minus the glyph.
"""
# Written with the help of Claude Opus 5.
# Run as "ki cut-signature" through the ki launcher, or directly as "python ki_cut_signature.py".

import argparse
import re
import uuid

from ki_common_lib import die, exit_with, form_end, help_formatter

PIXEL = 0.15
MARGIN = 1  # margin from the edges, in pixels

GLYPH = [
    "## # ",
    "# # #",
    "# # #",
    "# # #",
    "# # #",
    ]


def bbox_area(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


def parse_polys(text):
    out = []
    for m in re.finditer(r'\(fp_poly\b', text):
        a = m.start()
        b = form_end(text, a)
        block = text[a:b]
        pts = [(float(x), float(y))
            for x, y in re.findall(r'\(xy ([-\d.]+) ([-\d.]+)\)', block)]
        layer = re.search(r'\(layer "([^"]+)"', block)
        out.append({"span": (a, b), "pts": pts,
            "layer": layer.group(1) if layer else "F.SilkS"})
    return out


def remainder_runs(cols, rows):
    """Cells of the R grid that are not glyph, as horizontal runs.

    R is a whole number of pixels wide and tall, and every glyph pixel is a
    whole cell, so splitting by rows gives pieces that can never have holes.
    """
    gw, gh = len(GLYPH[0]), len(GLYPH)
    runs = []
    for r in range(rows):
        c = 0
        while c < cols:
            filled = (r < gh and c < gw and GLYPH[r][c] == '#')
            if filled:
                c += 1
                continue
            run = c
            while run < cols and not (r < gh and run < gw and GLYPH[r][run] == '#'):
                run += 1
            runs.append((c, r, run, r + 1))
            c = run
    return runs


def fmt_poly(pts, layer, width=0.0):
    body = "".join(f"      (xy {x:.6f} {y:.6f})\n" for x, y in pts)
    return (f"  (fp_poly\n    (pts\n{body}    )\n"
        f"    (stroke (width {width}) (type solid)) (fill solid)\n"
        f'    (layer "{layer}") (tstamp {uuid.uuid4()})\n  )\n')


def main():
    parser = argparse.ArgumentParser(
        prog="ki cut-signature", description=__doc__,
        formatter_class=help_formatter)
    parser.add_argument("source", metavar="IN.kicad_mod",
        help="footprint holding the polygon to cut")
    parser.add_argument("output", metavar="OUT.kicad_mod",
        help="where the cut footprint is written")
    args = parser.parse_args()
    src, dst = args.source, args.output
    text = open(src, encoding="utf-8").read()

    polys = parse_polys(text)
    if not polys:
        die("no fp_poly in %s, so there is nothing to cut the glyph from" % src)
    target = max(polys, key=lambda p: bbox_area(p["pts"]))
    ring = target["pts"]
    layer = target["layer"]

    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    right, bottom = max(xs), max(ys)  # KiCad Y grows downward

    gw = len(GLYPH[0]) * PIXEL
    gh = len(GLYPH) * PIXEL
    g_right = right - MARGIN * PIXEL
    g_bottom = bottom - MARGIN * PIXEL
    g_left = g_right - gw
    g_top = g_bottom - gh

    # 1. notch R out of the outline
    corner = next(i for i, p in enumerate(ring)
        if abs(p[0] - right) < 1e-6 and abs(p[1] - bottom) < 1e-6)
    prev = ring[corner - 1]
    detour = [(right, g_top), (g_left, g_top), (g_left, bottom)]
    if abs(prev[0] - right) > 1e-6:  # arrived along the bottom edge
        detour.reverse()
    new_ring = ring[:corner] + detour + ring[corner + 1:]

    # 2. the non-glyph part of R, as hole-free rectangles
    cols = round((right - g_left) / PIXEL)
    rows = round((bottom - g_top) / PIXEL)
    additions = ""
    for (a, b, c, d) in remainder_runs(cols, rows):
        x1, y1 = g_left + a * PIXEL, g_top + b * PIXEL
        x2, y2 = g_left + c * PIXEL, g_top + d * PIXEL
        additions += fmt_poly([(x1, y1), (x2, y1), (x2, y2), (x1, y2)], layer)
    pieces = remainder_runs(cols, rows)

    a, b = target["span"]
    out = text[:a] + fmt_poly(new_ring, layer).strip() + text[b:]
    out = out.rstrip()
    assert out.endswith(")")
    out = out[:-1] + additions + ")\n"

    open(dst, "w", encoding="utf-8", newline="\n").write(out)
    print(f"glyph at ({g_left:.3f}, {g_top:.3f}) to ({g_right:.3f}, {g_bottom:.3f}); "
        f"{len(pieces)} added polygon(s)")


if __name__ == "__main__":
    exit_with(main)

