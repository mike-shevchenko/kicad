#!/usr/bin/env python3
"""Build the release artifacts for a KiCad board, and publish them as a GitHub draft."""
# Written with the help of Claude Opus 5.
# Run as "ki release" through the ki launcher, or directly as "python ki_release.py".

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from functools import partial

from ki_common_lib import (die, exit_with, form_end, help_formatter, kicad_cli, note, open_path,
    parallel, run, shown, version_key)
from ki_diff import compare as compare_boards
from ki_image_lib import (CUT_LAYER, DPI, INK, LABEL_COLOR, OUTLINE_INK, OUTLINE_ONLY, PAGE,
    Image, Plotter, board_scale, captioned, counterpart, crop_margin, default_drill, numpy,
    outline_box, page_size, panel_rect, placeholder_page, require_imaging, sibling_project,
    sided_order, solid, substrate, tint, write_pdf)

DESCRIPTION = "Build a KiCad board's release artifacts, and publish them as a GitHub draft."

EPILOG = """\
Building

  ki release --png
      The board image only: both sides side by side, front on the left, back mirrored as
      if you had flipped the board over. Written into the release directory and opened.

  Everything is at exactly 400 dpi of the board itself, close to a phone's screen, so small
  silkscreen can be judged there as it will read on the board, and prints at the board's own
  size: the board image, recording that resolution, the straight-down render in the same
  layout pixel for pixel, the tilted pair made to the same size, and the pages of the layer
  PDF and of the diff, each the board and its margin under a band carrying the caption.

  ki release --render
      The 3D renders only, as two images: both sides straight down, laid out as the board
      image is and at its scale, and both sides tilted under a perspective projection so the
      connectors read, side by side and cropped to the board. Written and opened.

  ki release
      Everything: schematic, gerbers, layer plots, the board image, the renders, the STEP
      model, the bill of materials, the placement file, and the board compared with the
      previous release's layer by layer, as ki diff draws it, with the two silkscreen pages
      of that comparison as images too, for the release page. Publishes nothing.

  ki release --check
      Report what a build would produce and stop. No files are written.

Publishing

  ki release --publish
      Take what is already in the release directory and upload it as a draft release. It
      builds nothing, so review the files first and publish the same bytes you reviewed.
      Run it again after a rebuild and it refreshes that draft in place. It ends by printing
      the draft's page, to review it, and its edit page, to publish it.

  A draft creates no git tag: GitHub creates it only when the draft is published. The draft's
  own page has no button for that; its edit page does, reached by the pencil icon on the
  draft, with Publish release at the bottom. From the command line the same is
  gh release edit TAG --draft=false. So this command pushes nothing and can be undone by
  deleting the draft. Afterwards, git fetch --tags brings the new tag down.

  A build made from uncommitted changes is refused: the release would target a commit that does
  not hold the board its files show, and the source archive GitHub attaches to the release,
  the project as the tag has it, would not match them. Commit, rebuild, then publish.

  The build writes the release text into notes.md beside the files, from the git log since
  the previous release, so it can be reviewed and edited with them; --publish uploads it as
  it finds it, and it can be edited on the page afterwards too. It links the images by their
  eventual download URL, so they appear once the release is published and show as missing
  while it is still a draft, and in a local preview. The repository in those links comes from
  the origin remote; when that is not on GitHub they read OWNER/REPO, and the build warns.

Versions

  The revision is the board's own, from (rev "V1") in the title block, and it must agree
  between the board and the schematic. It is what is printed on the silkscreen, so it
  changes only when the physical board does.

  The tag adds a release serial to it: V1-r1, V1-r2 and so on, taken from the tags already
  published. Nothing to pass and nothing to remember.

  A revision is frozen once produced.md records that it was made:

      ki release --produced JLCPCB 10

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

  kicad-cli from the KiCad installation, and gh for publishing. Set KICAD_CLI to override
  the one found automatically. Everything else is Python:

      python -m pip install --user pypdfium2 pillow numpy

  Use python -m pip rather than a bare pip, which may belong to a different interpreter.
  scipy is not required, but one step runs far quicker when it is there.
"""

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
    "B.Courtyard": ("#26E9FF", 1.0),
    "F.Adhesive": ("#840084", 1.0),
    "B.Adhesive": ("#000084", 1.0),
    "User.Drawings": ("#C2C2C2", 1.0),
    "User.Comments": ("#5994DC", 1.0),
    "User.Eco1": ("#B4DBD2", 1.0),
    "User.Eco2": ("#D8C852", 1.0),
    "Margin": ("#FF26E2", 1.0)}

# Bottom to top, as the editor draws it with a copper layer selected: the mask's openings
# lowest, the silkscreen over them, and the copper on top and semi-transparent, so a ground
# pour does not hide the labels it covers.
FRONT_STACK = ("F.Mask", "F.Silkscreen", "F.Cu", "Edge.Cuts")
BACK_STACK = ("B.Mask", "B.Silkscreen", "B.Cu", "Edge.Cuts")

# What the fabricator receives. Pinned, so that Fab and Courtyard edits cannot register as
# changes to the manufactured board.
FAB_LAYERS = ("F.Cu", "B.Cu", "F.Mask", "B.Mask", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts")

# An empty layer still gets a page when the other side of the board has one, so that a
# two-page view keeps showing a front and its back together rather than drifting apart.
BLANK_WORD = "BLANK"


# The cut runs along the edge of the body, so on its own page the body is faded and the cut
# drawn in a color nothing else uses. Otherwise the line merges into the boundary it defines.
CUT_INK = "#FF2020"
CUT_FADE = 0.35

# The tilted pair is made to the board image's pixel size. KiCad has no fit-to-board under
# perspective, and frames the view by the image's height alone, whatever its width: the board
# takes the same pixels and keeps its offset from the center. So a small probe render tells
# the height that brings the pair to the target a little oversize, and the width that just
# holds the board; the pair is then cropped to what was drawn, scaled down and centered.
TILT_ROTATION = "-30,0,25"  # X looks down at the board, Z spins it so two edges show
TILT_ZOOM = 0.62  # low enough that no board reaches the top or bottom of the view
TILT_PROBE = (960, 480)  # KiCad rounds render sizes down to multiples of 16
TILT_OVERSIZE = 1.05  # rendered this much larger than shown, and as much wider than the board

# The straight-down renders are brought to the board image's scale by the outline. A render of
# the board without its 3D models has a silhouette that is nothing but the outline; made at a
# quarter of the size it gives the outline's size to a pixel, though not its position, which
# shifts by a few pixels between sizes. When the real render's silhouette has that size,
# nothing overhangs and the silhouette is the outline; otherwise the model-free render is made
# again at full size. Rendering costs by the pixel, so nothing is rendered larger than needed:
# KiCad fits the board to the view, filling about 96% of it at zoom 1, so the board image's
# size over FLAT_ZOOM * FLAT_FIT leaves a little to scale down, and the zoom keeps a
# twentieth of the view free on each side for parts that overhang the outline.
FLAT_ZOOM = 0.9
FLAT_FIT = 0.95
FLAT_PROBE = 2  # the model-free render is made at this fraction of the size, as its divisor
FLAT_TOLERANCE = 4  # pixels by which the silhouette may exceed the outline without overhang
MODEL_FORM = re.compile(r"\(model\s")

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

NOTES_FILE = "notes.md"

# Layers of the diff that the release notes show as images, both sides of each in one image
# laid out as the board image is: the silkscreen, where a change on a typical board is seen
# at a glance, and the copper. Each is an asset named after its layers.
DIFF_IMAGES = (("silkscreen", ("F.Silkscreen", "B.Silkscreen")), ("copper", ("F.Cu", "B.Cu")))
REPO_PLACEHOLDER = "OWNER/REPO"
# The owner/name of a GitHub remote, in its ssh, scp-like or https form.
GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")

# A release tag of any revision, V1-r3 or V2-r1; the diff compares against the newest one.
RELEASE_TAG = re.compile(r"^\S+-r\d+$")
PRODUCED = re.compile(r"^\s*(?:[-*]\s+)?(\S+)\s+produced\b", re.MULTILINE)


def git(*args, **kwargs):
    """Run git and return its output, or None when it fails and check is False."""
    check = kwargs.pop("check", True)
    try:
        result = subprocess.run(("git",) + args, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True)
    except OSError as error:
        die("git cannot be run: %s" % (error.strerror or error))
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
    return run([kicad_cli(), "version"]).strip()


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


def previous_release():
    """The newest published release of any revision, so that the first release of V2
    compares with the last of V1. Ordered by the date of what it tags, then by serial."""
    output = git("for-each-ref", "--format=%(creatordate:unix) %(refname:short)",
        "refs/tags", check=False) or ""
    found = []
    for line in output.splitlines():
        stamp, _space, tag = line.partition(" ")
        if RELEASE_TAG.match(tag):
            found.append((int(stamp), version_key(tag), tag))
    return max(found)[2] if found else None


def next_tag(revision):
    tags = published_tags(revision)
    serial = int(tags[-1].rsplit("-r", 1)[1]) + 1 if tags else 1
    return "%s-r%d" % (revision, serial)


def export_fab(pcb, directory):
    """Plot exactly what the fabricator gets: the pinned layer set, plus the drill files."""
    os.makedirs(directory, exist_ok=True)
    run([kicad_cli(), "pcb", "export", "gerbers", "--layers", ",".join(FAB_LAYERS),
        "-o", directory + os.sep, pcb])
    run([kicad_cli(), "pcb", "export", "drill", "-o", directory + os.sep, pcb])


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


def board_at_tag(project, tag, directory):
    """The board as that tag left it, written into the directory, or None when the tag has
    no board under this name."""
    relative = git("ls-files", "--full-name", project.pcb, check=False)
    if not relative:
        return None
    blob = git("show", "%s:%s" % (tag, relative), check=False)
    if blob is None:
        return None
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, os.path.basename(project.pcb))
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(blob)
    return path


def fab_hash_of_tag(project, tag):
    """The same hash, recomputed from the board as that tag left it."""
    work = tempfile.mkdtemp(prefix="ki-release-")
    try:
        old = board_at_tag(project, tag, work)
        if old is None:
            return None
        out = os.path.join(work, "fab")
        export_fab(old, out)
        return fab_hash(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def side_plots(stack, mirror):
    """The plots one side of the board image is made of."""
    return [(name, mirror, default_drill(name)) for name in stack]


def board_image_size(plotter):
    """The board image's size in pixels, which follows from the outlines alone."""
    sizes = []
    for mirror in (False, True):
        size = plotter.raster(CUT_LAYER, mirror, default_drill(CUT_LAYER)).size
        rect = panel_rect(outline_box(plotter, mirror), size)
        sizes.append((rect[2] - rect[0], rect[3] - rect[1]))
    front, back = sizes
    return front[0] + int(round(front[0] * GUTTER)) + back[0], max(front[1], back[1])


def save_png(image, path):
    """Save an image at DPI of the board, recording it, so that it prints at natural size."""
    image.save(path, dpi=(DPI, DPI))
    return path


def side_by_side(front, back):
    """Two panels on the background, front on the left, a gutter between them, the shorter
    one centered vertically."""
    gutter = int(round(front.width * GUTTER))
    height = max(front.height, back.height)
    canvas = Image.new("RGB", (front.width + gutter + back.width, height), BACKGROUND)
    canvas.paste(front, (0, (height - front.height) // 2))
    canvas.paste(back, (front.width + gutter, (height - back.height) // 2))
    return canvas


def side_image(plotter, stack, mirror):
    """One side of the board composited and cropped to its outline."""
    plotter.plot(side_plots(stack, mirror))
    drawn = []
    for name in stack:
        color, alpha = COLORS[name]
        drawn.append(tint(plotter.raster(name, mirror, default_drill(name)), color, alpha))

    page = Image.new("RGBA", drawn[0].size, BACKGROUND)
    for layer in drawn:
        page = Image.alpha_composite(page, layer)

    return page.crop(panel_rect(outline_box(plotter, mirror), page.size)).convert("RGB")


def build_board_image(project, outdir, tag, plotter):
    """The deliverable image: front on the left, back mirrored on the right."""
    front, back = parallel([partial(side_image, plotter, FRONT_STACK, False),
        partial(side_image, plotter, BACK_STACK, True)])
    out = os.path.join(outdir, project.asset(tag, "pcb.png"))
    return save_png(side_by_side(front, back), out)


def render(pcb, out_png, side, size, zoom, background, rotation=None):
    """One 3D view, orthographic unless rotated, which also takes a perspective projection."""
    # Quality stays at basic, and there is no --floor: both turn on the raytracer, whose
    # shadows fall across the board and read as features that are not there.
    command = [kicad_cli(), "pcb", "render", "-o", out_png, "--side", side,
        "--zoom", "%.2f" % zoom, "--width", str(size[0]), "--height", str(size[1]),
        "--quality", "basic", "--background", background]
    if rotation:
        command += ["--perspective", "--rotate", rotation]
    run(command + [pcb])


def tilt_rotation(side):
    """The Z sign flips on the back, so the two tilts mirror instead of repeating."""
    x, y, z = TILT_ROTATION.split(",")
    return TILT_ROTATION if side == "top" else "%s,%s,%s" % (x, y, -float(z))


def probe_tilted(pcb, side, work):
    """A small tilted render: its size, and the box of what it drew."""
    rendered = os.path.join(work, "tilt-probe-%s.png" % side)
    render(pcb, rendered, side, TILT_PROBE, TILT_ZOOM, "transparent", tilt_rotation(side))
    image = Image.open(rendered).convert("RGBA")
    box = solid_box(image)
    if not box:
        die("the tilted %s render of the board came out empty" % side)
    return image.size, box


def render_tilted(pcb, side, size, work):
    """A side under a perspective projection, so the connectors read, cropped to what it drew
    with the board image's margin, on the board image's background."""
    rendered = os.path.join(work, "tilt-%s.png" % side)
    render(pcb, rendered, side, size, TILT_ZOOM, "transparent", tilt_rotation(side))
    image = Image.open(rendered).convert("RGBA")
    box = solid_box(image)
    if not box:
        die("the tilted %s render of the board came out empty" % side)
    if box[0] == 0 or box[1] == 0 or box[2] == image.width or box[3] == image.height:
        note("the tilted %s view reaches the render's edge, and may be cut off" % side)
    margin = crop_margin(box)
    # Cropping past the render's edge fills with transparency, as wanted here.
    view = image.crop((box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin))
    return Image.alpha_composite(Image.new("RGBA", view.size, BACKGROUND), view).convert("RGB")


def without_models(pcb, directory):
    """A copy of the board with every 3D model taken out of its footprints."""
    text = open(pcb, encoding="utf-8", errors="replace", newline="").read()
    pieces, last = [], 0
    for match in MODEL_FORM.finditer(text):
        if match.start() < last:
            continue
        pieces.append(text[last:match.start()])
        last = form_end(text, match.start())
    pieces.append(text[last:])
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, os.path.basename(pcb))
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(pieces))
    project = sibling_project(pcb)
    if project:
        shutil.copyfile(project, os.path.splitext(path)[0] + ".kicad_pro")
    return path


def solid_box(image):
    """The box of what a render drew, on its transparent background."""
    return image.getchannel("A").point(lambda value: 255 if value > 128 else 0).getbbox()


def coverage_box(image, factor=1):
    """The box of what a render drew, to a fraction of a pixel, times the factor, or None.
    The renderer antialiases, so an edge pixel's opacity says how much of it is covered."""
    alpha = numpy.asarray(image.getchannel("A"), dtype=float) / 255.0

    def span(profile):
        covered = numpy.nonzero(profile > 0)[0]
        if not len(covered):
            return None
        first, last = covered[0], covered[-1]
        return (first + 1.0 - profile[first]) * factor, (last + profile[last]) * factor

    across, down = span(alpha.max(axis=0)), span(alpha.max(axis=1))
    if across is None:
        return None
    return across[0], down[0], across[1], down[1]


def flat_view(project, bare, side, box, work):
    """One side straight down, scaled so its outline matches the box, the outline's box in
    the plot; returns the scaled render and where the outline's corner sits in it. The board
    itself is rendered in place, since its 3D models may be found relative to it."""
    probe = (int((box[2] - box[0]) / (FLAT_ZOOM * FLAT_FIT * FLAT_PROBE)) + 1,
        int((box[3] - box[1]) / (FLAT_ZOOM * FLAT_FIT * FLAT_PROBE)) + 1)
    size = (probe[0] * FLAT_PROBE, probe[1] * FLAT_PROBE)
    full_png = os.path.join(work, "flat-%s.png" % side)
    bare_png = os.path.join(work, "flat-%s-bare.png" % side)
    parallel([partial(render, project.pcb, full_png, side, size, FLAT_ZOOM, "transparent"),
        partial(render, bare, bare_png, side, probe, FLAT_ZOOM, "transparent")])
    full = Image.open(full_png).convert("RGBA")
    guess = coverage_box(Image.open(bare_png).convert("RGBA"), FLAT_PROBE)
    drawn = coverage_box(full)
    if not guess or not drawn:
        die("the %s render of the board came out empty" % side)
    if (drawn[2] - drawn[0] - (guess[2] - guess[0]) <= FLAT_TOLERANCE
            and drawn[3] - drawn[1] - (guess[3] - guess[1]) <= FLAT_TOLERANCE):
        outline = drawn
    else:
        render(bare, bare_png, side, size, FLAT_ZOOM, "transparent")
        outline = coverage_box(Image.open(bare_png).convert("RGBA"))
    if drawn[0] < 1 or drawn[1] < 1 or drawn[2] > full.width - 1 or drawn[3] > full.height - 1:
        note("a part overhangs the %s view's edge, and is cut off in the render" % side)
    scale_x = (box[2] - box[0]) / (outline[2] - outline[0])
    scale_y = (box[3] - box[1]) / (outline[3] - outline[1])
    scaled = full.resize((int(round(full.width * scale_x)), int(round(full.height * scale_y))),
        Image.LANCZOS)
    return scaled, (int(round(outline[0] * scale_x)), int(round(outline[1] * scale_y)))


def build_flat_renders(project, outdir, tag, plotter, work):
    """Both sides straight down, in one image laid out as the board image is: at its scale,
    with its margin around the outline and its gutter, the back seen through the board. The
    panels grow only where a part overhangs the outline further than the margin reaches."""
    directory = os.path.join(work, "flat")
    bare = without_models(project.pcb, os.path.join(directory, "bare"))
    boxes = [outline_box(plotter, False), outline_box(plotter, True)]
    views = parallel(partial(flat_view, project, bare, side, box, directory)
        for side, box in zip(("top", "bottom"), boxes))

    # How far each render reaches past its outline, never less than the board image's
    # margin; top and bottom are shared, so the two outlines stay level.
    reach = []
    for (image, origin), box in zip(views, boxes):
        width, height = box[2] - box[0], box[3] - box[1]
        drawn = solid_box(image) or (origin[0], origin[1], origin[0] + width,
            origin[1] + height)
        margin = crop_margin(box)
        reach.append([max(margin, origin[0] - drawn[0]), max(margin, origin[1] - drawn[1]),
            max(margin, drawn[2] - origin[0] - width), max(margin, drawn[3] - origin[1] - height)])
    top = max(reach[0][1], reach[1][1])
    bottom = max(reach[0][3], reach[1][3])

    panels = []
    for (image, origin), box, (left, _top, right, _bottom) in zip(views, boxes, reach):
        width, height = box[2] - box[0], box[3] - box[1]
        # Cropping past the render's edge fills with transparency, as wanted here.
        view = image.crop((origin[0] - left, origin[1] - top, origin[0] + width + right,
            origin[1] + height + bottom))
        panel = Image.alpha_composite(Image.new("RGBA", view.size, BACKGROUND), view)
        panels.append(panel.convert("RGB"))
    out = os.path.join(outdir, project.asset(tag, "3d.png"))
    return save_png(side_by_side(*panels), out)


def build_tilted_renders(project, outdir, tag, plotter, work):
    """Both sides tilted, side by side, at the board image's size in pixels."""
    sides = ("top", "bottom")
    probes = parallel(partial(probe_tilted, project.pcb, side, work) for side in sides)
    target = board_image_size(plotter)

    # The pair as the probes would come out of the crop, margins and gutter included, and how
    # much taller the renders must be for it to reach the target, with a little to spare.
    widths = [box[2] - box[0] + 2 * crop_margin(box) for _size, box in probes]
    heights = [box[3] - box[1] + 2 * crop_margin(box) for _size, box in probes]
    pair = (widths[0] + int(round(widths[0] * GUTTER)) + widths[1], max(heights))
    grow = min(float(target[0]) / pair[0], float(target[1]) / pair[1]) * TILT_OVERSIZE
    jobs = []
    for side, (size, box) in zip(sides, probes):
        reach = max(size[0] / 2.0 - box[0], box[2] - size[0] / 2.0) * grow
        render_size = (int(2 * reach * TILT_OVERSIZE) + 16, int(size[1] * grow) + 16)
        jobs.append(partial(render_tilted, project.pcb, side, render_size, work))

    image = side_by_side(*parallel(jobs))
    shrink = min(float(target[0]) / image.width, float(target[1]) / image.height)
    image = image.resize((int(round(image.width * shrink)), int(round(image.height * shrink))),
        Image.LANCZOS)
    canvas = Image.new("RGB", target, BACKGROUND)
    canvas.paste(image, ((target[0] - image.width) // 2, (target[1] - image.height) // 2))
    out = os.path.join(outdir, project.asset(tag, "3d-tilted.png"))
    return save_png(canvas, out)


def build_renders(project, outdir, tag, plotter, work):
    """The two straight-down views in one image, and the two tilted ones in another."""
    return parallel([partial(build_flat_renders, project, outdir, tag, plotter, work),
        partial(build_tilted_renders, project, outdir, tag, plotter, work)])


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


def kicad_export(project, outdir, tag, suffix, arguments, source):
    """One kicad-cli export straight into the release directory."""
    out = os.path.join(outdir, project.asset(tag, suffix))
    run([kicad_cli()] + arguments + ["-o", out, source])
    return out


def build_diff(project, outdir, tag, work, previous):
    """The board compared with the previous release's, as ki diff draws it: the PDF, then
    the silkscreen and the copper as images; or None when that release has no board under
    this name."""
    directory = os.path.join(work, "diff")
    old = board_at_tag(project, previous, os.path.join(directory, "previous"))
    if old is None:
        print("  no board of this name in %s, so no diff against it" % previous)
        return None
    commit, modified = head_state()
    was = git("rev-parse", previous + "^{commit}", check=False) or "?"
    labels = ("%s, from %s" % (previous, was[:8]), "%s, this release, from %s%s"
        % (tag, (commit or "?")[:8], " with uncommitted changes" if modified else ""))
    out = os.path.join(outdir, project.asset(tag, "diff-from-%s.pdf" % previous))
    _changed, kept = compare_boards(old, project.pcb, labels, out, False, directory,
        keep=[layer for _suffix, layers in DIFF_IMAGES for layer in layers],
        keep_background=BACKGROUND)
    made = [out]
    for suffix, (front, back) in DIFF_IMAGES:
        if front in kept and back in kept:
            made.append(save_png(side_by_side(kept[front], kept[back]), os.path.join(outdir,
                project.asset(tag, "diff-from-%s-%s.png" % (previous, suffix)))))
    return made


def build_artwork(project, outdir, tag, plotter):
    """The board image and the layer document, which are made from the same plots."""
    plotter.plot(side_plots(FRONT_STACK, False) + side_plots(BACK_STACK, True)
        + document_plots(plotter))
    return parallel([partial(build_board_image, project, outdir, tag, plotter),
        partial(build_layers_pdf, project, outdir, tag, plotter)])


def outline(plotter, mirror):
    """The board outline alone, for the pages that carry no substrate under them."""
    return tint(plotter.raster(CUT_LAYER, mirror, 0), OUTLINE_INK, 1.0)


def faded(body):
    """The board body at reduced opacity, for the page that draws the cut along its edge."""
    dim = body.copy()
    dim.putalpha(body.getchannel("A").point(lambda value: int(value * CUT_FADE)))
    return dim


def page_ink(layer):
    if layer in OUTLINE_ONLY:
        return INK
    if layer == CUT_LAYER:
        return CUT_INK
    return COLORS.get(layer, (LABEL_COLOR, 1.0))[0]


def layer_page(name, mask, under, rect, out_png):
    """One page: the board body, the layer over it in its own color, and the layer name."""
    page = Image.new("RGBA", under.size, PAGE)
    page = Image.alpha_composite(page, under)
    page = Image.alpha_composite(page, solid(mask, page_ink(name)))
    captioned(page.crop(rect), name).save(out_png)


def document_plots(plotter):
    """Every plot the layer document is made of: the two board bodies, the two outlines
    and each layer the board declares, back layers mirrored."""
    wanted = [(CUT_LAYER, mirror, drill) for mirror in (False, True) for drill in (2, 0)]
    for _stored, name in plotter.layers:
        wanted.append((name, name.startswith("B."), default_drill(name)))
    return wanted


def build_layers_pdf(project, outdir, tag, plotter):
    """One page per layer, back layers mirrored as if seen through the board."""
    plotter.plot(document_plots(plotter))
    front, back, front_outline, back_outline = parallel([partial(substrate, plotter, False),
        partial(substrate, plotter, True), partial(outline, plotter, False),
        partial(outline, plotter, True)])
    bodies = {False: front, True: back}
    outlines = {False: front_outline, True: back_outline}
    fades = {False: faded(bodies[False]), True: faded(bodies[True])}
    rects = dict((mirror, panel_rect(outline_box(plotter, mirror), bodies[mirror].size))
        for mirror in (False, True))
    layers = sided_order(plotter.names)
    # Rasterized and judged before any page is built, because whether an empty layer
    # deserves a placeholder depends on a layer that may come later.
    masks = dict(zip(layers, parallel(
        partial(plotter.raster, name, name.startswith("B."), default_drill(name))
        for name in layers)))
    bare = dict((name, not masks[name].getbbox()) for name in layers)
    if all(bare.values()):
        die("every layer plotted empty at scale %.4f, which a board with an outline cannot do"
            % plotter.scale)

    pages, jobs, dropped, placed = [], [], [], []
    for name in layers:
        mirror = name.startswith("B.")
        page = os.path.join(plotter.work, "%02d-%s.png" % (len(pages), name))
        if bare[name]:
            if bare.get(counterpart(name), True):
                dropped.append(name)
                continue
            placed.append(name)
            jobs.append(partial(placeholder_page, name, BLANK_WORD, page_size(rects[mirror]),
                page))
            pages.append(page)
            continue
        if name in OUTLINE_ONLY:
            under = outlines[mirror]
        elif name == CUT_LAYER:
            under = fades[mirror]
        else:
            under = bodies[mirror]
        jobs.append(partial(layer_page, name, masks[name], under, rects[mirror], page))
        pages.append(page)
    parallel(jobs)
    if dropped:
        print("  nothing on %s, so no page for %s"
            % (", ".join(dropped), "them" if len(dropped) > 1 else "it"))
    if placed:
        print("  nothing on %s, kept blank to face the other side" % ", ".join(placed))
    out = os.path.join(outdir, project.asset(tag, "layers.pdf"))
    write_pdf(pages, out, DPI)
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


def write_build_json(project, outdir, tag, digest, scale, files, diff_from):
    commit, modified = head_state()
    record = {"project": project.name,
        "tag": tag,
        "diff_from": diff_from,
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
    """The status block: where the revision stands and what the next release would be.
    Returns whether the fabrication output changed since the previous release of this
    revision, or None when there is none to compare with."""
    print("Project: %s" % project.name)
    print("Revision: %s" % project.revision)
    tags = published_tags(project.revision)
    previous = tags[-1] if tags else None
    frozen = project.revision in frozen_revisions(project.root)
    if previous:
        print("Previous release: %s" % previous)
    else:
        print("Previous release: none for %s" % project.revision)
    against = previous_release()
    print("Diff: against %s" % against if against else "Diff: none, nothing released before")
    changed = None
    if digest is not None and previous:
        was = fab_hash_of_tag(project, previous)
        if was is not None:
            changed = was != digest
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
        print("Warning: %d uncommitted file(s), so the artifacts match no commit, and"
            " --publish will refuse them" % len(modified))
    print("Next tag: %s" % tag)
    return changed


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


def github_repo():
    """The owner/name of the origin remote when it is on GitHub, or None."""
    url = git("remote", "get-url", "origin", check=False) or ""
    match = GITHUB_REMOTE.search(url)
    return "%s/%s" % match.groups() if match else None


def release_notes(project, tag, record, repo, fab_changed):
    """A body generated from the log, with the renders linked by their eventual URL."""
    tags = published_tags(project.revision)
    previous = tags[-1] if tags else None
    # The changes run from the release the diff compares with when this revision has none of
    # its own yet, so that the first release of a revision still tells its story.
    since = previous or record.get("diff_from")
    log = git("log", "--no-merges", "--pretty=format:- %s", "%s..HEAD" % since if since
        else "HEAD", check=False) or ""
    lines = ["PCB revision %s." % project.revision, ""]
    if previous and fab_changed is not None:
        lines.append("Fabrication files changed since %s, see the difference below." % previous
            if fab_changed else "Fabrication files identical to %s." % previous)
        lines.append("")
    # GitHub makes this archive of the tagged commit itself, once the release is published;
    # publishing refuses uncommitted builds, so it holds the board the images show.
    lines.append("KiCad project: [%s.zip](https://github.com/%s/archive/refs/tags/%s.zip)"
        % (tag, repo, tag))
    lines.append("")
    base = "https://github.com/%s/releases/download/%s" % (repo, tag)
    lines.append("![tilted](%s/%s)" % (base, project.asset(tag, "3d-tilted.png")))
    lines.append("")
    lines.append("![3d](%s/%s)" % (base, project.asset(tag, "3d.png")))
    lines.append("")
    lines.append("![pcb](%s/%s)" % (base, project.asset(tag, "pcb.png")))
    lines.append("")
    diff_from = record.get("diff_from")
    if log or diff_from:
        lines.append("## Changes since %s" % since if since else "## Changes")
        lines.append("")
    if diff_from:
        built = set(entry["name"] for entry in record["files"])
        diff = project.asset(tag, "diff-from-%s.pdf" % diff_from)
        lines.append("### Changes in PCB: [%s](%s/%s)" % (diff, base, diff))
        lines.append("")
        for suffix, _layers in DIFF_IMAGES:
            name = project.asset(tag, "diff-from-%s-%s.png" % (diff_from, suffix))
            if name in built:
                lines.append("![%s changes](%s/%s)" % (suffix, base, name))
                lines.append("")
        lines.append("### Changes in project")
        lines.append("")
    if log:
        lines.append(log)
    elif diff_from:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def release_dir(root, tag):
    return os.path.join(root, "release", tag)


def write_notes(project, outdir, tag, record, fab_changed):
    """The release text, into notes.md beside the files it links."""
    repo = github_repo()
    if not repo:
        print("Warning: the origin remote is not on GitHub, so %s links to %s; put the"
            " repository there before publishing" % (NOTES_FILE, REPO_PLACEHOLDER))
    path = os.path.join(outdir, NOTES_FILE)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(release_notes(project, tag, record, repo or REPO_PLACEHOLDER, fab_changed))
    return path


def do_build(project, tag, what, fab_changed=None):
    """Build one of the three sets into the release directory, and return the files made.
    fab_changed is the verdict of the status block, for the release text."""
    outdir = release_dir(project.root, tag)
    os.makedirs(outdir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="ki-release-")
    try:
        print("\nBuilding %s" % shown(outdir))
        scale, _aspect = board_scale(project.pcb, work)
        plotter = Plotter(project.pcb, work, scale)
        if what == "png":
            made = [build_board_image(project, outdir, tag, plotter)]
        elif what == "render":
            made = build_renders(project, outdir, tag, plotter, work)
        else:
            # Every stage at once. The STEP export is the longest single step, and the
            # artwork chain the longest run of Python; everything else hides behind them.
            jobs = [partial(build_gerbers, project, outdir, tag, work),
                partial(build_artwork, project, outdir, tag, plotter),
                partial(build_renders, project, outdir, tag, plotter, work),
                partial(kicad_export, project, outdir, tag, "model.step",
                    ["pcb", "export", "step", "--no-dnp"], project.pcb),
                partial(kicad_export, project, outdir, tag, "pos.csv",
                    ["pcb", "export", "pos", "--format", "csv", "--units", "mm",
                    "--side", "both"], project.pcb)]
            if project.sch:
                jobs += [partial(kicad_export, project, outdir, tag, "schematic.pdf",
                        ["sch", "export", "pdf"], project.sch),
                    partial(kicad_export, project, outdir, tag, "bom.csv",
                        ["sch", "export", "bom"], project.sch)]
            previous = previous_release()
            if previous:
                jobs.append(partial(build_diff, project, outdir, tag, work, previous))
            results = parallel(jobs)
            diffs = results.pop() if previous else None
            (zipped, digest), (image, layers), renders = results[:3]
            made = [zipped, image] + renders + results[5:] + [layers] + results[3:5]
            if diffs:
                made += diffs
            path, record = write_build_json(project, outdir, tag, digest, scale, made,
                previous if diffs else None)
            made += [path, write_notes(project, outdir, tag, record, fab_changed)]
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
        die("nothing built yet - run ki release first")
    builds = [d for d in sorted(os.listdir(root))
        if os.path.isfile(os.path.join(root, d, "build.json"))]
    if not builds:
        die("no complete build in %s - run ki release, not --png or --render" % shown(root))
    tag = builds[-1]
    outdir = os.path.join(root, tag)
    record = json.load(open(os.path.join(outdir, "build.json"), encoding="utf-8"))
    if record["dirty"]:
        die("%s was built from uncommitted changes, so no commit holds the board its files"
            " show:\n          %s\n          Commit them, rebuild, then publish."
            % (tag, "\n          ".join(record["modified"])))

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

    # The release text is taken as the build left it, or as it was edited since.
    notes_file = os.path.join(outdir, NOTES_FILE)
    if os.path.isfile(notes_file):
        notes = ["--notes-file", notes_file]
    else:
        print("Warning: no %s in %s - ki release writes it with the build. Publishing without"
            " a release text." % (NOTES_FILE, shown(outdir)))
        notes = None

    title = "%s %s" % (project.name, tag)
    target = ["--target", record["commit"]] if record["commit"] else []
    origin = (record["commit"] or "?")[:8]
    if standing:
        print("Refreshing the %s draft, from commit %s." % (tag, origin))
        run(["gh", "release", "edit", tag, "--draft", "--title", title] + (notes or [])
            + target)
        # Assets the build no longer produces would otherwise linger from the older draft.
        wanted = set(os.path.basename(path) for path in files)
        for asset in standing["assets"]:
            if asset["name"] not in wanted:
                run(["gh", "release", "delete-asset", tag, asset["name"], "--yes"])
        run(["gh", "release", "upload", tag, "--clobber"] + files)
        url = standing["url"]
    else:
        print("Publishing %s as a draft, from commit %s." % (tag, origin))
        output = run(["gh", "release", "create", tag, "--draft", "--title", title]
            + (notes or ["--notes", ""]) + target + files)
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        url = lines[-1] if lines else ""
    # The draft's page has no Publish button, only its edit page does, one path segment away.
    if url:
        print("\nReview the draft at\n  %s" % url)
        print("then publish it from its edit page, with Publish release at the bottom:")
        print("  %s" % url.replace("/releases/tag/", "/releases/edit/"))
    else:
        print("\nReview the draft on the repository's releases page, then publish it from its")
        print("edit page, the pencil icon, with Publish release at the bottom.")
    print("Or run: gh release edit %s --draft=false" % tag)
    print("GitHub creates the tag on publishing; run git fetch --tags afterwards.")


def main():
    parser = argparse.ArgumentParser(prog="ki release", description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=help_formatter)
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

    kicad_cli()
    if not (args.publish or args.produced):
        require_imaging()

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
        fab_changed = report(project, tag, fab_hash(staging))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if args.check:
        print("\nNothing written.")
        return 0

    made, outdir = do_build(project, tag, "all", fab_changed)
    print("\n%d files. Nothing published - review them, then run ki release --publish."
        % len(made))
    return 0


if __name__ == "__main__":
    exit_with(main)
