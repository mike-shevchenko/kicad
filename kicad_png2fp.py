#!/usr/bin/env python3
"""
Convert a 1-bit-style PNG into a KiCad footprint of exact pixel rectangles
on F.Mask.

    png2kicad.py logo.png ->  logo.kicad_mod

Dark pixels (< 128) become mask openings. The physical pixel size comes from
the PNG's own DPI metadata; set it in Photoshop via Image > Image Size >
Resolution. 84.667 DPI gives 0.3 mm pixels, 101.6 DPI gives 0.25 mm.

Horizontally adjacent pixels are merged into single rectangles, which keeps
the file small without changing the geometry.
"""
# Written with the help of Claude Opus 5.

from PIL import Image
import os
import sys
import uuid

LAYER = "F.Mask"
THRESHOLD = 128
DEFAULT_DPI = 84.666666  # 0.3 mm per pixel, used only if the PNG has no DPI


def main():
    if len(sys.argv) != 2:
        print("usage: png2kicadmod.py <image.png>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    img = Image.open(path).convert("L")
    w, h = img.size

    dpi = img.info.get("dpi", (DEFAULT_DPI, DEFAULT_DPI))[0] or DEFAULT_DPI
    px = 25.4 / float(dpi)

    # centre the artwork on the footprint origin
    x0 = -w * px / 2.0
    y0 = -h * px / 2.0

    rects = []
    pix = img.load()
    for y in range(h):
        x = 0
        while x < w:
            if pix[x, y] < THRESHOLD:
                run = x
                while run < w and pix[run, y] < THRESHOLD:
                    run += 1
                rects.append((x, y, run, y + 1))
                x = run
            else:
                x += 1

    name = os.path.splitext(os.path.basename(path))[0]
    out_path = os.path.splitext(path)[0] + ".kicad_mod"

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f'(footprint "{name}"\n')
        f.write("\t(version 20260206)\n")
        f.write('\t(generator "png2kicadmod")\n')
        f.write('\t(generator_version "10.0")\n')
        f.write('\t(layer "F.Cu")\n')
        f.write('\t(attr board_only exclude_from_pos_files exclude_from_bom)\n')
        f.write(f'\t(property "Reference" "G***"\n\t\t(at 0 {y0 - 1:.4f} 0)\n'
                '\t\t(layer "F.Fab")\n\t\t(hide yes)\n'
                f'\t\t(uuid "{uuid.uuid4()}")\n'
                '\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1 1)\n'
                '\t\t\t\t(thickness 0.15)\n\t\t\t)\n\t\t)\n\t)\n')
        f.write(f'\t(property "Value" "{name}"\n\t\t(at 0 {y0 + h * px + 1:.4f} 0)\n'
                '\t\t(layer "F.Fab")\n\t\t(hide yes)\n'
                f'\t\t(uuid "{uuid.uuid4()}")\n'
                '\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1 1)\n'
                '\t\t\t\t(thickness 0.15)\n\t\t\t)\n\t\t)\n\t)\n')

        for (a, b, c, d) in rects:
            sx = x0 + a * px
            sy = y0 + b * px
            ex = x0 + c * px
            ey = y0 + d * px
            f.write("\t(fp_rect\n")
            f.write(f"\t\t(start {sx:.4f} {sy:.4f})\n")
            f.write(f"\t\t(end {ex:.4f} {ey:.4f})\n")
            f.write("\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type solid)\n\t\t)\n")
            f.write("\t\t(fill yes)\n")
            f.write(f'\t\t(layer "{LAYER}")\n')
            f.write(f'\t\t(uuid "{uuid.uuid4()}")\n')
            f.write("\t)\n")

        f.write(")\n")


if __name__ == "__main__":
    main()
