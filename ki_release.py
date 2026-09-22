#!/usr/bin/env python3
"""Build the release artifacts for a KiCad board, and publish them as a GitHub draft."""
# Written with the help of Claude Opus 5.
# Wrappers on PATH: ki-release.cmd for cmd.exe, ki-release for cygwin and git-bash.
# Create them with ki_install.py.

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

DESCRIPTION = "Build a KiCad board's release artifacts, and publish them as a GitHub draft."

EPILOG = """\
Building

  ki-release --png
      The board image only: both sides side by side, front on the left, back mirrored as
      if you had flipped the board over. Written into the release directory and opened.

  ki-release --render
      The four 3D renders only: each side straight down, and each side tilted under a
      perspective projection so the connectors read. Written and opened.

  ki-release
      Everything: schematic, gerbers, layer plots, the board image, the renders, the STEP
      model, the bill of materials and the placement file. Publishes nothing.

  ki-release --check
      Report what a build would produce and stop. No files are written.

Publishing

  ki-release --publish
      Take what is already in the release directory and upload it as a draft release. It
      builds nothing, so review the files first and publish the same bytes you reviewed.
      Run it again after a rebuild and it refreshes that draft in place. The draft's URL
      is the last thing it prints.

  A draft creates no git tag: GitHub creates it only when you press Publish on the release
  page. So this command pushes nothing and can be undone by deleting the draft. Afterwards,
  git fetch --tags brings the new tag down.

  The release body is generated from the git log since the previous release, and can be
  edited on the page. It links the images by their eventual download URL, so they appear
  once the release is published and show as missing while it is still a draft.

Versions

  The revision is the board's own, from (rev "V1") in the title block, and it must agree
  between the board and the schematic. It is what is printed on the silkscreen, so it
  changes only when the physical board does.

  The tag adds a release serial to it: V1-r1, V1-r2 and so on, taken from the tags already
  published. Nothing to pass and nothing to remember.

  A revision is frozen once produced.md records that it was made:

      ki-release --produced JLCPCB 10

  which appends one line, dated today and naming the newest published release of this
  revision:

      - V1 produced 2026-10-05, JLCPCB, 10 pcs, from V1-r4

  After that, any change to the fabrication output is refused, because boards carrying that
  revision already exist. Before that, revise as freely as you like. Changes that leave the
  fabrication output alone - schematic work, libraries, Fab-layer notes - are always fine
  and need no new revision. Record a reorder by running --produced again.

  Whether the output changed is decided by hashing the gerbers and drill files with their
  timestamp lines removed, and comparing against the same hash recomputed from the previous
  release's tag. Nothing is cached, so the answer cannot go stale.

Requirements

  kicad-cli from the KiCad installation, ImageMagick's magick on PATH, and gh for
  publishing. Set KICAD_CLI or MAGICK to override the ones found automatically.
"""

# Rasterization is fixed at 400 dpi, and the board is scaled to fill the drawing sheet, so
# the pixel size of a panel follows from the sheet alone and not from how big the board is.
DPI = 400
PROBE_DPI = 100  # density of the throwaway plot that measures the board on its sheet
PAGE_FILL = 0.9  # fraction of the sheet the scaled board is fitted into
CROP_MARGIN = 0.02  # border kept around the outline, as a fraction of panel width
GUTTER = 0.04  # gap between the two panels, as a fraction of panel width

# KiCad's stock colors, hardcoded rather than read from a theme: the active theme is usually
# _builtin_default, which is compiled into KiCad and has no file to read.
BACKGROUND = "#001023"
COLORS = {"Edge.Cuts": ("#D0D2CD", 1.0),
    "F.Cu": ("#C83434", 0.6),
    "B.Cu": ("#4D7FC4", 0.6),
    "F.Silkscreen": ("#F2EDA1", 1.0),
    "B.Silkscreen": ("#E8B2A7", 1.0),
    "F.Mask": ("#D864FF", 0.4),
    "B.Mask": ("#02FFEE", 0.4),
    "F.Paste": ("#B4A09A", 1.0),
    "B.Paste": ("#00C2C2", 1.0),
    "F.Fab": ("#AFAFAF", 1.0),
    "B.Fab": ("#585D84", 1.0),
    "F.Courtyard": ("#FF26E2", 1.0),
    "B.Courtyard": ("#26E9FF", 1.0)}

# Bottom to top, as the editor draws it with a copper layer selected: silkscreen underneath,
# copper over it and semi-transparent, so a ground pour does not hide the labels it covers.
# The mask has a color above but is left out of the stack, its pad openings only add rings.
FRONT_STACK = ("F.Silkscreen", "F.Cu", "Edge.Cuts")
BACK_STACK = ("B.Silkscreen", "B.Cu", "Edge.Cuts")
COPPER = ("F.Cu", "B.Cu")

# What the fabricator receives. Pinned, so that Fab and Courtyard edits cannot register as
# changes to the manufactured board.
FAB_LAYERS = ("F.Cu", "B.Cu", "F.Mask", "B.Mask", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts")

# A page of the layer plot: the board body dark so that pale artwork reads on it, the page
# around and through the drill holes left white, and the layer named at the top.
SUBSTRATE = "#24402E"
PAGE = "white"
LABEL_COLOR = "#303030"
LABEL_DIVISOR = 24  # the page width over this gives the label point size, and its margin

# The Fab layers are drawings rather than artwork, so their pages carry no substrate, only
# the board outline for context. KiCad's own colors are chosen for a dark canvas and vanish
# on a white one, so these two pages are drawn in ink instead.
OUTLINE_ONLY = ("F.Fab", "B.Fab")
INK = "#303030"
OUTLINE_INK = "#909090"

# The cut runs along the edge of the body, so on its own page the body is faded and the cut
# drawn in a color nothing else uses. Otherwise the line merges into the boundary it defines.
CUT_LAYER = "Edge.Cuts"
CUT_INK = "#FF2020"
CUT_FADE = 0.35

# Every layer worth a page in the layer plot, in reading order.
PLOT_LAYERS = ("F.Cu", "B.Cu", "F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask",
    "F.Paste", "B.Paste", "F.Fab", "B.Fab", "F.Courtyard", "B.Courtyard", "Edge.Cuts")

RENDER_TILT_SIZE = (1600, 1200)
RENDER_FLAT_LONG_SIDE = 1600
TILT_ROTATION = "-30,0,25"  # X looks down at the board, Z spins it so two edges show
TILT_ZOOM = 0.62  # KiCad has no fit-to-board for renders, and 1.0 crops under perspective
FLAT_ZOOM = 0.9

# Lines carrying a timestamp, which differ on every export and must not reach the fab hash.
TIMESTAMP_LINES = ("%TF.CreationDate,", "G04 Created by", "; DRILL file",
    "; #@! TF.CreationDate,", chr(34) + "CreationDate" + chr(34))

PRODUCED_FILE = "produced.md"
PRODUCED_HEADER = """\
# Production history

Each line records one production run: the board revision, the date, who made it, how
many, and the release whose files were sent. A revision listed here is frozen - its
fabrication output may no longer change, because boards carrying it exist.

"""

REVISION = re.compile(r'\(rev\s+"([^"]*)"\)')
PRODUCED = re.compile(r"^\s*(?:[-*]\s+)?(\S+)\s+produced\b", re.MULTILINE)


def die(message, code=2):
    sys.stderr.write("[release] " + message + "\n")
    sys.exit(code)


def shown(path):
    return path.replace("\\", "/")


def version_key(text):
    """Sort "10.0" above "9.0", which a plain string sort gets backwards."""
    parts = [int(chunk) for chunk in re.split(r"[^0-9]+", text) if chunk]
    return parts or [0]


def find_kicad_cli():
    """The CLI ships with KiCad and is usually not on PATH. Prefer the newest install."""
    override = os.environ.get("KICAD_CLI")
    if override:
        return override
    on_path = shutil.which("kicad-cli")
    if on_path:
        return on_path
    found = []
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        root = os.path.join(base, "KiCad")
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            candidate = os.path.join(root, entry, "bin", "kicad-cli.exe")
            if os.path.isfile(candidate):
                found.append((version_key(entry), candidate))
    found.sort()
    return found[-1][1] if found else None


def find_magick():
    override = os.environ.get("MAGICK")
    if override:
        return override
    return shutil.which("magick")


KICAD_CLI = None
MAGICK = None


def run(command, quiet=True):
    """Run a command, and fail loudly with its own output when it does."""
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True)
    if result.returncode:
        sys.stderr.write(result.stdout or "")
        die("%s failed with status %d" % (os.path.basename(command[0]), result.returncode))
    if not quiet:
        sys.stdout.write(result.stdout or "")
    return result.stdout or ""


def git(*args, **kwargs):
    """Run git and return its output, or None when it fails and check is False."""
    check = kwargs.pop("check", True)
    result = subprocess.run(("git",) + args, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, universal_newlines=True)
    if result.returncode:
        if check:
            die("git %s failed: %s" % (args[0], result.stderr.strip()))
        return None
    return result.stdout.strip()


def fetch_tags():
    """Publishing a draft creates its tag on GitHub, so the local list lags until fetched."""
    git("fetch", "--tags", "--quiet", check=False)


def gh_json(*args):
    """Ask gh for JSON, or None when the object is not there."""
    result = subprocess.run(("gh",) + args, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, universal_newlines=True)
    if result.returncode or not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def kicad_version():
    return run([KICAD_CLI, "version"]).strip()


class Project(object):
    """The board, the schematic, and the names derived from them."""

    def __init__(self, directory):
        self.root = os.path.abspath(directory)
        boards = sorted(glob.glob(os.path.join(self.root, "*.kicad_pcb")))
        if not boards:
            die("no .kicad_pcb in %s" % shown(self.root))
        if len(boards) > 1:
            die("more than one .kicad_pcb in %s" % shown(self.root))
        self.pcb = boards[0]
        self.name = os.path.basename(self.pcb)[:-len(".kicad_pcb")]
        schematic = os.path.join(self.root, self.name + ".kicad_sch")
        self.sch = schematic if os.path.isfile(schematic) else None
        self.revision = self.read_revision()

    def read_revision(self):
        """The title block revision, which must agree between the two files."""
        found = {}
        for path in (self.pcb, self.sch):
            if not path:
                continue
            text = open(path, encoding="utf-8", errors="replace", newline="").read()
            match = REVISION.search(text)
            found[os.path.basename(path)] = match.group(1) if match else None
        values = set(found.values())
        if None in values:
            missing = [n for n, v in found.items() if v is None]
            die("no (rev \"...\") in %s - set it in File / Page Settings" % ", ".join(missing))
        if len(values) > 1:
            die("revision differs: " + ", ".join("%s says %r" % kv for kv in found.items()))
        return values.pop()

    def asset(self, tag, suffix):
        return "%s-%s-%s" % (self.name, tag, suffix)


def frozen_revisions(root):
    """Revisions that produced.md records as made, and so as unchangeable."""
    path = os.path.join(root, PRODUCED_FILE)
    if not os.path.isfile(path):
        return set()
    text = open(path, encoding="utf-8", errors="replace").read()
    return set(PRODUCED.findall(text))


def published_tags(revision):
    """Tags of released versions of this revision, newest serial last."""
    output = git("tag", "--list", "%s-r*" % revision, check=False) or ""
    numbered = []
    for tag in output.split():
        match = re.match(r"^%s-r(\d+)$" % re.escape(revision), tag)
        if match:
            numbered.append((int(match.group(1)), tag))
    numbered.sort()
    return [tag for _serial, tag in numbered]


def next_tag(revision):
    tags = published_tags(revision)
    serial = int(tags[-1].rsplit("-r", 1)[1]) + 1 if tags else 1
    return "%s-r%d" % (revision, serial)


def export_fab(pcb, directory):
    """Plot exactly what the fabricator gets: the pinned layer set, plus the drill files."""
    os.makedirs(directory, exist_ok=True)
    run([KICAD_CLI, "pcb", "export", "gerbers", "--layers", ",".join(FAB_LAYERS),
        "-o", directory + os.sep, pcb])
    run([KICAD_CLI, "pcb", "export", "drill", "-o", directory + os.sep, pcb])


def fab_hash(directory):
    """Hash the fabrication output with its timestamp lines removed, so it is repeatable."""
    digest = hashlib.sha256()
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        digest.update(name.encode("utf-8") + b"\n")
        with open(path, encoding="utf-8", errors="replace", newline="") as handle:
            for line in handle:
                stripped = line.lstrip()
                if any(stripped.startswith(prefix) for prefix in TIMESTAMP_LINES):
                    continue
                digest.update(line.replace("\r\n", "\n").encode("utf-8"))
    return digest.hexdigest()


def fab_hash_of_tag(project, tag):
    """The same hash, recomputed from the board as that tag left it."""
    relative = git("ls-files", "--full-name", project.pcb, check=False)
    if not relative:
        return None
    blob = git("show", "%s:%s" % (tag, relative), check=False)
    if blob is None:
        return None
    work = tempfile.mkdtemp(prefix="ki-release-")
    try:
        old = os.path.join(work, os.path.basename(project.pcb))
        with open(old, "w", encoding="utf-8", newline="") as handle:
            handle.write(blob)
        out = os.path.join(work, "fab")
        export_fab(old, out)
        return fab_hash(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def identify(path, fmt):
    return run([MAGICK, "identify", "-format", fmt, path]).strip()


def plot_layer(pcb, layer, out_pdf, scale, mirror, drill=None):
    """One layer, black on white, scaled up to fill the sheet."""
    if drill is None:
        drill = 2 if layer in COPPER else 0
    command = [KICAD_CLI, "pcb", "export", "pdf", "--mode-single", "--layers", layer,
        "--black-and-white", "--scale", "%.4f" % scale,
        "--drill-shape-opt", str(drill)]
    if mirror:
        command.append("--mirror")
    command += ["-o", out_pdf, pcb]
    run(command)


def tint(pdf, png, color, alpha, density):
    """Rasterize a black-on-white plot into artwork of one color on transparency."""
    run([MAGICK, "-density", str(density), pdf, "-colorspace", "gray", "-negate",
        "-alpha", "off",
        "(", "+clone", "-fill", color, "-colorize", "100", ")", "+swap",
        "-compose", "CopyOpacity", "-composite",
        "-channel", "A", "-evaluate", "multiply", "%.3f" % alpha, "+channel", png])


def board_scale(pcb, work):
    """Measure the board on its sheet, and return the scale that fits it to the sheet.

    Working in pixels of a throwaway plot avoids parsing the sheet size and avoids pcbnew:
    only the ratio matters, so the probe density cancels out.
    """
    probe = os.path.join(work, "probe.pdf")
    plot_layer(pcb, "Edge.Cuts", probe, 1.0, False)
    raster = os.path.join(work, "probe.png")
    run([MAGICK, "-density", str(PROBE_DPI), probe, "-colorspace", "gray", "-alpha", "off",
        raster])
    page = identify(raster, "%w %h").split()
    box = identify(raster, "%@").replace("+", " ").replace("x", " ").split()
    page_w, page_h = int(page[0]), int(page[1])
    board_w, board_h = int(box[0]), int(box[1])
    if not board_w or not board_h:
        die("the board outline is empty - is there anything on Edge.Cuts?")
    scale = min(page_w * PAGE_FILL / board_w, page_h * PAGE_FILL / board_h)
    return scale, float(board_w) / board_h


def side_image(pcb, stack, mirror, out_png, scale, work, prefix):
    """Composite one side of the board, cropped to its outline."""
    layers = []
    for layer in stack:
        pdf = os.path.join(work, "%s-%s.pdf" % (prefix, layer))
        png = os.path.join(work, "%s-%s.png" % (prefix, layer))
        plot_layer(pcb, layer, pdf, scale, mirror)
        color, alpha = COLORS[layer]
        tint(pdf, png, color, alpha, DPI)
        layers.append((layer, png))

    outline = dict(layers)["Edge.Cuts"]
    page = identify(outline, "%w %h").split()
    box = identify(outline, "%@").replace("+", " ").replace("x", " ").split()
    page_w, page_h = int(page[0]), int(page[1])
    width, height, left, top = (int(value) for value in box)
    margin = int(round(width * CROP_MARGIN))
    left = max(0, left - margin)
    top = max(0, top - margin)
    width = min(page_w - left, width + 2 * margin)
    height = min(page_h - top, height + 2 * margin)

    command = [MAGICK, "-size", "%dx%d" % (page_w, page_h), "xc:" + BACKGROUND]
    for _layer, png in layers:
        command += [png, "-compose", "over", "-composite"]
    command += ["-crop", "%dx%d+%d+%d" % (width, height, left, top), "+repage", out_png]
    run(command)


def build_board_image(project, outdir, tag, work):
    """The deliverable image: front on the left, back mirrored on the right."""
    scale, _aspect = board_scale(project.pcb, work)
    front = os.path.join(work, "front.png")
    back = os.path.join(work, "back.png")
    side_image(project.pcb, FRONT_STACK, False, front, scale, work, "f")
    side_image(project.pcb, BACK_STACK, True, back, scale, work, "b")

    width = int(identify(front, "%w"))
    gutter = int(round(width * GUTTER))
    out = os.path.join(outdir, project.asset(tag, "board.png"))
    run([MAGICK, front,
        "(", back, "-background", BACKGROUND, "-splice", "%dx0+0+0" % gutter, ")",
        "-background", BACKGROUND, "+append", out])
    return out, scale


def render(pcb, out_png, side, tilted, aspect):
    """One 3D view. Tilted views use a perspective projection so the connectors read."""
    if tilted:
        width, height = RENDER_TILT_SIZE
        zoom = TILT_ZOOM
    else:
        long_side = RENDER_FLAT_LONG_SIDE
        if aspect >= 1.0:
            width, height = long_side, int(round(long_side / aspect))
        else:
            width, height = int(round(long_side * aspect)), long_side
        zoom = FLAT_ZOOM
    # Quality stays at basic, and there is no --floor: both turn on the raytracer, whose
    # shadows fall across the board and read as features that are not there.
    command = [KICAD_CLI, "pcb", "render", "-o", out_png, "--side", side,
        "--zoom", "%.2f" % zoom, "--width", str(width), "--height", str(height),
        "--quality", "basic", "--background", "opaque"]
    if tilted:
        # The Z sign flips on the back, so the two tilts mirror instead of repeating.
        rotation = TILT_ROTATION
        if side == "bottom":
            x, y, z = rotation.split(",")
            rotation = "%s,%s,%s" % (x, y, -float(z))
        command += ["--perspective", "--rotate", rotation]
    command.append(pcb)
    run(command)


def build_renders(project, outdir, tag, work):
    _scale, aspect = board_scale(project.pcb, work)
    made = []
    for side in ("top", "bottom"):
        for tilted in (False, True):
            suffix = "3d-%s%s.png" % (side, "-tilt" if tilted else "")
            out = os.path.join(outdir, project.asset(tag, suffix))
            render(project.pcb, out, side, tilted, aspect)
            made.append(out)
    return made


def build_gerbers(project, outdir, tag, work):
    """The fabrication package, zipped, and its hash."""
    staging = os.path.join(work, "fab")
    export_fab(project.pcb, staging)
    digest = fab_hash(staging)
    out = os.path.join(outdir, project.asset(tag, "gerbers.zip"))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(os.listdir(staging)):
            archive.write(os.path.join(staging, name), name)
    return out, digest


def build_documents(project, outdir, tag, work, scale):
    """Schematic, layer plots, model and the two machine-readable lists."""
    made = []
    if project.sch:
        out = os.path.join(outdir, project.asset(tag, "schematic.pdf"))
        run([KICAD_CLI, "sch", "export", "pdf", "-o", out, project.sch])
        made.append(out)
        out = os.path.join(outdir, project.asset(tag, "bom.csv"))
        run([KICAD_CLI, "sch", "export", "bom", "-o", out, project.sch])
        made.append(out)

    made.append(build_layers_pdf(project, outdir, tag, work, scale))

    out = os.path.join(outdir, project.asset(tag, "model.step"))
    run([KICAD_CLI, "pcb", "export", "step", "--no-dnp", "-o", out, project.pcb])
    made.append(out)

    out = os.path.join(outdir, project.asset(tag, "pos.csv"))
    run([KICAD_CLI, "pcb", "export", "pos", "--format", "csv", "--units", "mm",
        "--side", "both", "-o", out, project.pcb])
    made.append(out)
    return made


def substrate(pcb, work, scale, mirror, name):
    """The board body as an image: opaque where the substrate is, holes already punched.

    Flooding the page corner of an outline plot that also carries the drill shapes leaves
    exactly the body standing, since nothing else encloses a region.
    """
    pdf = os.path.join(work, name + ".pdf")
    plot_layer(pcb, "Edge.Cuts", pdf, scale, mirror, drill=2)
    mask = os.path.join(work, name + "-mask.png")
    run([MAGICK, "-density", str(DPI), pdf, "-colorspace", "gray", "-alpha", "off",
        "-fuzz", "20%", "-fill", "black", "-draw", "color 0,0 floodfill",
        "-alpha", "off", "-threshold", "50%", mask])
    body = os.path.join(work, name + "-body.png")
    run([MAGICK, "-size", identify(mask, "%wx%h"), "xc:" + SUBSTRATE, mask,
        "-alpha", "off", "-compose", "CopyOpacity", "-composite", body])
    return body


def outline(pcb, work, scale, mirror, name):
    """The board outline alone, for the pages that carry no substrate under them."""
    pdf = os.path.join(work, name + "-outline.pdf")
    plot_layer(pcb, "Edge.Cuts", pdf, scale, mirror, drill=0)
    png = os.path.join(work, name + "-outline.png")
    tint(pdf, png, OUTLINE_INK, 1.0, DPI)
    return png


def faded(body, work, name):
    """The board body at reduced opacity, for the page that draws the cut along its edge."""
    out = os.path.join(work, name + "-faded.png")
    run([MAGICK, body, "-channel", "A", "-evaluate", "multiply", "%.3f" % CUT_FADE,
        "+channel", out])
    return out


def page_ink(layer):
    if layer in OUTLINE_ONLY:
        return INK
    if layer == CUT_LAYER:
        return CUT_INK
    return COLORS.get(layer, (LABEL_COLOR, 1.0))[0]


def layer_page(pcb, layer, body, out_png, scale, mirror, work):
    """One page: the board body, the layer over it in its own color, and the layer name."""
    pdf = os.path.join(work, "page-%s.pdf" % layer)
    plot_layer(pcb, layer, pdf, scale, mirror)
    art = os.path.join(work, "page-%s.png" % layer)
    ink = page_ink(layer)
    tint(pdf, art, ink, 1.0, DPI)
    size = identify(body, "%wx%h")
    width = int(size.split("x")[0])
    step = max(12, width // LABEL_DIVISOR)
    run([MAGICK, "-size", size, "xc:" + PAGE, body, "-compose", "over", "-composite",
        art, "-compose", "over", "-composite",
        "-gravity", "North", "-pointsize", str(step), "-fill", LABEL_COLOR,
        "-annotate", "+0+%d" % step, layer, out_png])


def build_layers_pdf(project, outdir, tag, work, scale):
    """One page per layer, back layers mirrored as if seen through the board."""
    bodies = {False: substrate(project.pcb, work, scale, False, "front"),
        True: substrate(project.pcb, work, scale, True, "back")}
    outlines = {False: outline(project.pcb, work, scale, False, "front"),
        True: outline(project.pcb, work, scale, True, "back")}
    fades = {False: faded(bodies[False], work, "front"),
        True: faded(bodies[True], work, "back")}
    pages = []
    for layer in PLOT_LAYERS:
        mirror = layer.startswith("B.")
        if layer in OUTLINE_ONLY:
            under = outlines[mirror]
        elif layer == CUT_LAYER:
            under = fades[mirror]
        else:
            under = bodies[mirror]
        page = os.path.join(work, "%02d-%s.png" % (len(pages), layer))
        layer_page(project.pcb, layer, under, page, scale, mirror, work)
        pages.append(page)
    out = os.path.join(outdir, project.asset(tag, "layers.pdf"))
    run([MAGICK] + pages + ["-units", "PixelsPerInch", "-density", str(DPI), out])
    return out


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def head_state():
    """The commit the build came from, and whether the tree had uncommitted work in it."""
    commit = git("rev-parse", "HEAD", check=False)
    status = git("status", "--porcelain", check=False)
    # Split on whitespace rather than slicing columns: git() strips its output, which eats
    # the leading space of an unstaged line and would shift every path along by one.
    modified = []
    for line in (status or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            modified.append(parts[1])
    return commit, modified


def write_build_json(project, outdir, tag, digest, scale, files):
    commit, modified = head_state()
    record = {"project": project.name,
        "tag": tag,
        "revision": project.revision,
        "commit": commit,
        "dirty": bool(modified),
        "modified": sorted(modified),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kicad": kicad_version(),
        "fab_hash": digest,
        "fab_layers": list(FAB_LAYERS),
        "image": {"dpi": DPI, "scale": round(scale, 4), "background": BACKGROUND},
        "render": {"rotate": TILT_ROTATION, "tilt_zoom": TILT_ZOOM, "flat_zoom": FLAT_ZOOM},
        "files": [{"name": os.path.basename(p), "bytes": os.path.getsize(p),
            "sha256": sha256_of(p)} for p in sorted(files)]}
    path = os.path.join(outdir, "build.json")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=2)
        handle.write("\n")
    return path, record


def report(project, tag, digest=None):
    """The status block: where the revision stands and what the next release would be."""
    print("Project: %s" % project.name)
    print("Revision: %s" % project.revision)
    tags = published_tags(project.revision)
    previous = tags[-1] if tags else None
    frozen = project.revision in frozen_revisions(project.root)
    if previous:
        print("Previous release: %s" % previous)
    else:
        print("Previous release: none for %s" % project.revision)
    if digest is not None and previous:
        was = fab_hash_of_tag(project, previous)
        if was is None:
            print("Fab output: cannot compare, %s has no board in it" % previous)
        elif was == digest:
            print("Fab output: unchanged since %s" % previous)
        elif frozen:
            die("the fabrication output changed, but produced.md records %s as made.\n"
                "          Boards carrying that revision exist. Bump the title block to the\n"
                "          next revision in both the board and the schematic." % project.revision)
        else:
            print("Fab output: changed since %s" % previous)
    if frozen:
        print("%s is frozen by produced.md - the fabrication output may no longer change"
            % project.revision)
    else:
        print("%s is not frozen - not in produced.md, the design may still change"
            % project.revision)
    commit, modified = head_state()
    if modified:
        print("Warning: %d uncommitted file(s), so the artifacts match no commit"
            % len(modified))
    print("Next tag: %s" % tag)


def do_produced(project, fab, quantity):
    """Record a production run, which freezes the revision against further fab changes."""
    if not quantity.isdigit() or int(quantity) < 1:
        die("quantity must be a positive whole number, not %r" % quantity)
    fetch_tags()
    tags = published_tags(project.revision)
    if not tags:
        die("nothing published for %s yet.\n"
            "          Publish a release first, so the record can name the files that were"
            " sent." % project.revision)
    tag = tags[-1]
    path = os.path.join(project.root, PRODUCED_FILE)
    line = "- %s produced %s, %s, %s pcs, from %s" % (project.revision,
        datetime.now().strftime("%Y-%m-%d"), fab, quantity, tag)
    fresh = not os.path.isfile(path)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        if fresh:
            handle.write(PRODUCED_HEADER)
        handle.write(line + "\n")
    print("%s %s:" % ("Created" if fresh else "Appended to", shown(path)))
    print("  " + line)
    print("\n%s is now frozen. Commit the file to record that." % project.revision)

    # Say so now rather than at the next build: the freeze dates from when the files were
    # sent, so a board already edited since then is in breach the moment this line lands.
    work = tempfile.mkdtemp(prefix="ki-release-")
    try:
        staging = os.path.join(work, "fab")
        export_fab(project.pcb, staging)
        current = fab_hash(staging)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    was = fab_hash_of_tag(project, tag)
    if was and was != current:
        print("\nWarning: the board already differs from %s in its fabrication output." % tag)
        print("Builds will refuse until the revision is bumped in board and schematic.")


def release_notes(project, tag, record, owner):
    """A body generated from the log, with the renders linked by their eventual URL."""
    tags = published_tags(project.revision)
    previous = tags[-1] if tags else None
    span = "%s..HEAD" % previous if previous else "HEAD"
    log = git("log", "--no-merges", "--pretty=format:- %s", span, check=False) or ""
    lines = ["Board revision `%s`." % project.revision]
    if previous:
        lines.append("Fabrication output %s since %s."
            % ("unchanged" if record["fab_hash"] == fab_hash_of_tag(project, previous)
                else "changed", previous))
    if record["dirty"]:
        lines.append("Built from uncommitted changes on top of `%s`." % record["commit"][:8])
    lines.append("")
    if owner:
        base = "https://github.com/%s/releases/download/%s" % (owner, tag)
        for side in ("top", "bottom"):
            name = project.asset(tag, "3d-%s-tilt.png" % side)
            lines.append("![%s](%s/%s)" % (side, base, name))
        lines.append("")
        lines.append("![board](%s/%s)" % (base, project.asset(tag, "board.png")))
        lines.append("")
    if log:
        lines.append("## Changes")
        lines.append("")
        lines.append(log)
    return "\n".join(lines) + "\n"


def open_path(path):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.call(["xdg-open" if sys.platform != "darwin" else "open", path])


def release_dir(root, tag):
    return os.path.join(root, "release", tag)


def do_build(project, tag, what):
    """Build one of the three sets into the release directory, and return the files made."""
    outdir = release_dir(project.root, tag)
    os.makedirs(outdir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="ki-release-")
    try:
        print("\nBuilding %s" % shown(outdir))
        if what == "png":
            made, _scale = build_board_image(project, outdir, tag, work)
            made = [made]
        elif what == "render":
            made = build_renders(project, outdir, tag, work)
        else:
            zipped, digest = build_gerbers(project, outdir, tag, work)
            image, scale = build_board_image(project, outdir, tag, work)
            made = [zipped, image]
            made += build_renders(project, outdir, tag, work)
            made += build_documents(project, outdir, tag, work, scale)
            path, _record = write_build_json(project, outdir, tag, digest, scale, made)
            made.append(path)
        for path in sorted(made):
            print("  %s  %d bytes" % (os.path.basename(path), os.path.getsize(path)))
        return made, outdir
    finally:
        shutil.rmtree(work, ignore_errors=True)


def do_publish(project):
    """Upload what was built, as a draft. Nothing is tagged and nothing is pushed."""
    if not shutil.which("gh"):
        die("gh is not on PATH - install the GitHub CLI to publish")
    root = os.path.join(project.root, "release")
    if not os.path.isdir(root):
        die("nothing built yet - run ki-release first")
    builds = [d for d in sorted(os.listdir(root))
        if os.path.isfile(os.path.join(root, d, "build.json"))]
    if not builds:
        die("no complete build in %s - run ki-release, not --png or --render" % shown(root))
    tag = builds[-1]
    outdir = os.path.join(root, tag)
    record = json.load(open(os.path.join(outdir, "build.json"), encoding="utf-8"))

    files = []
    for entry in record["files"]:
        path = os.path.join(outdir, entry["name"])
        if not os.path.isfile(path):
            die("%s is listed in build.json but missing - rebuild" % entry["name"])
        if sha256_of(path) != entry["sha256"]:
            die("%s changed since it was built - rebuild before publishing" % entry["name"])
        files.append(path)
    files.append(os.path.join(outdir, "build.json"))

    fetch_tags()
    if git("tag", "--list", tag, check=False):
        die("tag %s already exists - that release is published, build the next one" % tag)
    standing = gh_json("release", "view", tag, "--json", "isDraft,url,assets")
    if standing and not standing["isDraft"]:
        die("%s is already published on GitHub, and this build would replace it.\n"
            "          Run git fetch --tags, then rebuild to get the next serial." % tag)

    owner = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q",
        ".nameWithOwner"], stdout=subprocess.PIPE, universal_newlines=True)
    owner = owner.stdout.strip() if owner.returncode == 0 else ""
    notes = release_notes(project, tag, record, owner)
    notes_file = os.path.join(outdir, "notes.md")
    with open(notes_file, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(notes)

    title = "%s %s" % (project.name, tag)
    target = ["--target", record["commit"]] if record["commit"] else []
    origin = (record["commit"] or "?")[:8]
    if standing:
        print("Refreshing the %s draft, from commit %s." % (tag, origin))
        run(["gh", "release", "edit", tag, "--draft", "--title", title,
            "--notes-file", notes_file] + target)
        # Assets the build no longer produces would otherwise linger from the older draft.
        wanted = set(os.path.basename(path) for path in files)
        for asset in standing["assets"]:
            if asset["name"] not in wanted:
                run(["gh", "release", "delete-asset", tag, asset["name"], "--yes"])
        run(["gh", "release", "upload", tag, "--clobber"] + files)
        url = standing["url"]
    else:
        print("Publishing %s as a draft, from commit %s." % (tag, origin))
        output = run(["gh", "release", "create", tag, "--draft", "--title", title,
            "--notes-file", notes_file] + target + files)
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        url = lines[-1] if lines else ""
    print("Review it, then press Publish there to create the tag,")
    print("and run git fetch --tags afterwards.")
    if url:
        print("\n%s" % url)


def main():
    global KICAD_CLI, MAGICK
    parser = argparse.ArgumentParser(prog="ki-release", description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(prog, width=99))
    parser.add_argument("directory", nargs="?", default=".", metavar="DIR",
        help="the project directory, the current one by default")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--png", action="store_true",
        help="build only the board image, and open it")
    mode.add_argument("--render", action="store_true",
        help="build only the four 3D renders, and open them")
    mode.add_argument("--check", action="store_true",
        help="report the revision, the fab-output verdict and the next tag; build nothing")
    mode.add_argument("--produced", nargs=2, metavar=("FAB", "QUANTITY"),
        help="record a production run in produced.md, freezing this revision")
    mode.add_argument("--publish", action="store_true",
        help="upload what is already built as a draft release; build nothing")
    args = parser.parse_args()

    KICAD_CLI = find_kicad_cli()
    if not KICAD_CLI:
        die("no kicad-cli found; set KICAD_CLI to it")
    MAGICK = find_magick()
    if not MAGICK and not (args.publish or args.produced):
        die("ImageMagick's magick is not on PATH; set MAGICK to it")

    project = Project(args.directory)
    os.chdir(project.root)
    if git("rev-parse", "--git-dir", check=False) is None:
        die("%s is not inside a git repository" % shown(project.root))

    if args.produced:
        do_produced(project, args.produced[0], args.produced[1])
        return 0

    if args.publish:
        do_publish(project)
        return 0

    if not (args.png or args.render):
        fetch_tags()
    tag = next_tag(project.revision)
    if args.png or args.render:
        made, outdir = do_build(project, tag, "png" if args.png else "render")
        open_path(made[0] if len(made) == 1 else outdir)
        return 0

    # The freeze is checked before anything is built, so a refusal costs a second, not a
    # whole build. The export is repeated during the build, which is cheap enough.
    work = tempfile.mkdtemp(prefix="ki-release-")
    try:
        staging = os.path.join(work, "fab")
        export_fab(project.pcb, staging)
        report(project, tag, fab_hash(staging))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if args.check:
        print("\nNothing written.")
        return 0

    made, outdir = do_build(project, tag, "all")
    print("\n%d files. Nothing published - review them, then run ki-release --publish."
        % len(made))
    return 0


if __name__ == "__main__":
    sys.exit(main())
