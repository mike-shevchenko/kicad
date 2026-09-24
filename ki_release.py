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

from ki_common_lib import (die, exit_with, help_formatter, kicad_cli, open_path, parallel, run,
    shown, version_key)
from ki_diff import compare as compare_boards
from ki_image_lib import (CUT_LAYER, DPI, LABEL_COLOR, PAGE, Image, Plotter, board_scale,
    caption, counterpart, default_drill, placeholder_page, require_imaging, sharp, sided_order,
    solid, substrate, tint, write_pdf)

DESCRIPTION = "Build a KiCad board's release artifacts, and publish them as a GitHub draft."

EPILOG = """\
Building

  ki release --png
      The board image only: both sides side by side, front on the left, back mirrored as
      if you had flipped the board over. Written into the release directory and opened.

  ki release --render
      The four 3D renders only: each side straight down, and each side tilted under a
      perspective projection so the connectors read. Written and opened.

  ki release
      Everything: schematic, gerbers, layer plots, the board image, the renders, the STEP
      model, the bill of materials, the placement file, and the board compared with the
      previous release's layer by layer, as ki diff draws it. Publishes nothing.

  ki release --check
      Report what a build would produce and stop. No files are written.

Publishing

  ki release --publish
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
    "B.Courtyard": ("#26E9FF", 1.0),
    "F.Adhesive": ("#840084", 1.0),
    "B.Adhesive": ("#000084", 1.0),
    "User.Drawings": ("#C2C2C2", 1.0),
    "User.Comments": ("#5994DC", 1.0),
    "User.Eco1": ("#B4DBD2", 1.0),
    "User.Eco2": ("#D8C852", 1.0),
    "Margin": ("#FF26E2", 1.0)}

# Bottom to top, as the editor draws it with a copper layer selected: silkscreen underneath,
# copper over it and semi-transparent, so a ground pour does not hide the labels it covers.
# The mask has a color above but is left out of the stack, its pad openings only add rings.
FRONT_STACK = ("F.Silkscreen", "F.Cu", "Edge.Cuts")
BACK_STACK = ("B.Silkscreen", "B.Cu", "Edge.Cuts")

# What the fabricator receives. Pinned, so that Fab and Courtyard edits cannot register as
# changes to the manufactured board.
FAB_LAYERS = ("F.Cu", "B.Cu", "F.Mask", "B.Mask", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts")

# An empty layer still gets a page when the other side of the board has one, so that a
# two-page view keeps showing a front and its back together rather than drifting apart.
BLANK_WORD = "BLANK"

# The Fab layers are drawings rather than artwork, so their pages carry no substrate, only
# the board outline for context. KiCad's own colors are chosen for a dark canvas and vanish
# on a white one, so these two pages are drawn in ink instead.
OUTLINE_ONLY = ("F.Fab", "B.Fab")
INK = "#303030"
OUTLINE_INK = "#909090"

# The cut runs along the edge of the body, so on its own page the body is faded and the cut
# drawn in a color nothing else uses. Otherwise the line merges into the boundary it defines.
CUT_INK = "#FF2020"
CUT_FADE = 0.35

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

# A release tag of any revision, V1-r3 or V2-r1; the diff compares against the newest one.
RELEASE_TAG = re.compile(r"^\S+-r\d+$")
PRODUCED = re.compile(r"^\s*(?:[-*]\s+)?(\S+)\s+produced\b", re.MULTILINE)


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


def side_image(plotter, stack, mirror):
    """One side of the board composited and cropped to its outline."""
    plotter.plot(side_plots(stack, mirror))
    drawn, edge = [], None
    for name in stack:
        color, alpha = COLORS[name]
        drawn.append(tint(plotter.raster(name, mirror, default_drill(name)), color, alpha))
        if name == "Edge.Cuts":
            edge = drawn[-1]

    page = Image.new("RGBA", drawn[0].size, BACKGROUND)
    for layer in drawn:
        page = Image.alpha_composite(page, layer)

    box = sharp(edge.getchannel("A")).getbbox()
    if not box:
        die("the board outline plotted empty at scale %.4f, so the %s side cannot be cropped"
            % (plotter.scale, "back" if mirror else "front"))
    margin = int(round((box[2] - box[0]) * CROP_MARGIN))
    return page.crop((max(0, box[0] - margin), max(0, box[1] - margin),
        min(page.width, box[2] + margin), min(page.height, box[3] + margin))).convert("RGB")


def build_board_image(project, outdir, tag, plotter):
    """The deliverable image: front on the left, back mirrored on the right."""
    front, back = parallel([partial(side_image, plotter, FRONT_STACK, False),
        partial(side_image, plotter, BACK_STACK, True)])

    gutter = int(round(front.width * GUTTER))
    canvas = Image.new("RGB", (front.width + gutter + back.width,
        max(front.height, back.height)), BACKGROUND)
    canvas.paste(front, (0, 0))
    canvas.paste(back, (front.width + gutter, 0))
    out = os.path.join(outdir, project.asset(tag, "board.png"))
    canvas.save(out)
    return out


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
    command = [kicad_cli(), "pcb", "render", "-o", out_png, "--side", side,
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


def build_renders(project, outdir, tag, aspect):
    made, jobs = [], []
    for side in ("top", "bottom"):
        for tilted in (False, True):
            suffix = "3d-%s%s.png" % (side, "-tilt" if tilted else "")
            out = os.path.join(outdir, project.asset(tag, suffix))
            made.append(out)
            jobs.append(partial(render, project.pcb, out, side, tilted, aspect))
    parallel(jobs)
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


def kicad_export(project, outdir, tag, suffix, arguments, source):
    """One kicad-cli export straight into the release directory."""
    out = os.path.join(outdir, project.asset(tag, suffix))
    run([kicad_cli()] + arguments + ["-o", out, source])
    return out


def build_diff(project, outdir, tag, work, previous):
    """The board compared with the previous release's, as ki diff draws it, or None when
    that release has no board under this name."""
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
    compare_boards(old, project.pcb, labels, out, False, directory)
    return out


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


def layer_page(name, mask, under, out_png):
    """One page: the board body, the layer over it in its own color, and the layer name."""
    page = Image.new("RGBA", under.size, PAGE)
    page = Image.alpha_composite(page, under)
    page = Image.alpha_composite(page, solid(mask, page_ink(name)))

    caption(page, name)
    page.convert("RGB").save(out_png)


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
            jobs.append(partial(placeholder_page, name, BLANK_WORD, bodies[mirror].size, page))
            pages.append(page)
            continue
        if name in OUTLINE_ONLY:
            under = outlines[mirror]
        elif name == CUT_LAYER:
            under = fades[mirror]
        else:
            under = bodies[mirror]
        jobs.append(partial(layer_page, name, masks[name], under, page))
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
    against = previous_release()
    print("Diff: against %s" % against if against else "Diff: none, nothing released before")
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
        if record.get("diff_from"):
            diff = project.asset(tag, "diff-from-%s.pdf" % record["diff_from"])
            lines.append("The board compared with %s, layer by layer: [%s](%s/%s)"
                % (record["diff_from"], diff, base, diff))
            lines.append("")
    if log:
        lines.append("## Changes")
        lines.append("")
        lines.append(log)
    return "\n".join(lines) + "\n"


def release_dir(root, tag):
    return os.path.join(root, "release", tag)


def do_build(project, tag, what):
    """Build one of the three sets into the release directory, and return the files made."""
    outdir = release_dir(project.root, tag)
    os.makedirs(outdir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="ki-release-")
    try:
        print("\nBuilding %s" % shown(outdir))
        scale, aspect = board_scale(project.pcb, work)
        plotter = Plotter(project.pcb, work, scale)
        if what == "png":
            made = [build_board_image(project, outdir, tag, plotter)]
        elif what == "render":
            made = build_renders(project, outdir, tag, aspect)
        else:
            # Every stage at once. The STEP export is the longest single step, and the
            # artwork chain the longest run of Python; everything else hides behind them.
            jobs = [partial(build_gerbers, project, outdir, tag, work),
                partial(build_artwork, project, outdir, tag, plotter),
                partial(build_renders, project, outdir, tag, aspect),
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
            diff = results.pop() if previous else None
            (zipped, digest), (image, layers), renders = results[:3]
            made = [zipped, image] + renders + results[5:] + [layers] + results[3:5]
            if diff:
                made.append(diff)
            path, _record = write_build_json(project, outdir, tag, digest, scale, made,
                previous if diff else None)
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
        die("nothing built yet - run ki release first")
    builds = [d for d in sorted(os.listdir(root))
        if os.path.isfile(os.path.join(root, d, "build.json"))]
    if not builds:
        die("no complete build in %s - run ki release, not --png or --render" % shown(root))
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
        report(project, tag, fab_hash(staging))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if args.check:
        print("\nNothing written.")
        return 0

    made, outdir = do_build(project, tag, "all")
    print("\n%d files. Nothing published - review them, then run ki release --publish."
        % len(made))
    return 0


if __name__ == "__main__":
    exit_with(main)
