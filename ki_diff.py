#!/usr/bin/env python3
"""Compare two versions of a board, layer by layer, into a PDF: for git, for Fork, or by hand."""
# Written with the help of Claude Opus 5.
# Run as "ki diff" through the ki launcher, or directly as "python ki_diff.py". git and Fork
# launch ki.cmd by name.

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from functools import partial

from ki_common_lib import die, exit_with, help_formatter, note, open_path, parallel, shown
from ki_image_lib import (CUT_LAYER, DPI, INK, OUTLINE_INK, OUTLINE_ONLY, PAGE, SUBSTRATE,
    Image, ImageChops, Plotter, artwork, body_mask, board_scale, captioned, counterpart,
    default_drill, dimmed, outline_box, page_size, panel_rect, placeholder_page, require_imaging,
    sharp, sibling_project, sided_order, text_page, write_pdf)

DESCRIPTION = "Compare two versions of a board, layer by layer, into a PDF."

EPILOG = """\
Modes

  ki diff OLD NEW                 Two board files. The PDF is written and opened.
  ki diff --git-diff <7 args>     git's external diff driver. The PDF is written, its path printed.
  ki diff --clean                 Remove what earlier runs left behind.

  --all-layers gives every layer a page, changed or not. -o FILE names the PDF, and --no-open
  leaves it closed. Schematics are not compared, only boards.

What a page shows

  One page per layer that differs, drawn the way ki release draws it: the board at 400 dpi of
  its own size, the page no larger than the board of either version with its margin and a
  band for the caption, so it prints at the board's size and reads on a phone as it will on
  the board; the new version's substrate dark, back layers mirrored, the holes of both
  versions punched through. The Fab pages are drawn as the layer PDF draws them, in ink on
  white around the outlines of both versions, with no substrate or holes.
  Artwork present in both versions is drawn faint; what only the old version had is red, what
  only the new one has is cyan. A moved item is therefore a red copy and a cyan copy. The holes
  are compared the same way on every page: a hole both versions have is white, one only the old
  version had is tinted red, one only the new one has is tinted cyan.

  A layer whose other side changed gets a page reading UNCHANGED, so that a two-page view keeps
  front and back facing. The last page sums up what was compared and which layers changed.

  Both versions are plotted by the same kicad-cli at the same scale and compared pixel by pixel
  at 400 dpi, so geometry that did not change produces no difference at all. A layer whose two
  plots are the same bytes is settled without a pixel being rendered, which is what makes a
  diff of one small change take a couple of seconds. Zone fills are
  stored in the file, so a version saved with a stale fill shows a pour difference; that
  difference is real in the file, even though nobody drew it.

Setting up git

  echo *.kicad_pcb diff=kicad_diff >> .gitattributes
  git config --local diff.kicad_diff.command "C:/programs/ki.cmd diff --git-diff"

  git diff uses the driver; git show and git log -p need --ext-diff; --no-ext-diff disables
  it. PDFs land in <repo>/.git/ki-diff/, named <board>_<oldsha8>_to_<newsha8>.pdf, with
  'worktree' for uncommitted changes. Append --all-layers to the command above for every layer.

Setting up Fork

  Preferences -> Integration -> Diff Tools -> add one:
      Title:        KiCad PCB
      Path:         C:\\programs\\ki.cmd
      Arguments:    diff $REMOTE $LOCAL

  Fork lists every registered tool in its External Diff submenu, so pick this one on
  .kicad_pcb files. The old version goes first, and Fork hands it over as $REMOTE: with the
  placeholders the other way round, what was removed comes out cyan instead of red. The PDF
  opens in the default viewer and is written to the same place the git driver uses, so
  removing the repository removes it too.

Where the PDFs go

  Inside a repository, <repo>/.git/ki-diff/; outside one, ki-diff under the temp directory.
  The board's scale on its sheet is remembered there too, which saves one kicad-cli run per
  diff. Nothing else is kept: a diff takes a few seconds, so ki diff --clean can always be run.

Requirements

  kicad-cli from the KiCad installation. Set KICAD_CLI to override the one found
  automatically. Everything else is Python:

      python -m pip install --user pypdfium2 pillow numpy

  Use python -m pip rather than a bare pip, which may belong to a different interpreter.
  scipy is not required, but one step runs far quicker when it is there.
"""

PCB_EXT = ".kicad_pcb"
SCH_EXT = ".kicad_sch"
AREA = "ki-diff"  # the directory the PDFs go into, under .git or under the temp directory

# Red for what the old version had, cyan for what the new one has, the rest faint: the two
# colors are told apart by every eye, and both stand off the dark green of the substrate. A
# hole is white, so a hole of one version only carries its color halfway toward white.
OLD_COLOR = "#FF3B3B"
NEW_COLOR = "#40C8FF"
OLD_HOLE = "#FF9D9D"
NEW_HOLE = "#A0E4FF"
SAME_COLOR = "white"
SAME_ALPHA = 0.35

# A diff is looked at once and thrown away, so its pages deflate at 6 rather than 9: a tenth
# larger, in a third of the time.
DEFLATE_LEVEL = 6
UNCHANGED_WORD = "UNCHANGED"
SUMMARY_TITLE = "Summary"


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]", "_", text) or "diff"


def git_dir():
    """The .git directory of the repository around the current directory, or None."""
    try:
        result = subprocess.run(["git", "rev-parse", "--absolute-git-dir"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        if result.returncode == 0 and result.stdout.strip():
            return os.path.normpath(result.stdout.strip())
    except OSError:
        pass
    fallback = os.path.join(os.getcwd(), ".git")
    return fallback if os.path.isdir(fallback) else None


def areas():
    """Where PDFs are kept: the repository's area when inside one, and the temp area."""
    places = []
    repo = git_dir()
    if repo:
        places.append(("repo", os.path.join(repo, AREA)))
    places.append(("temp", os.path.join(tempfile.gettempdir(), AREA)))
    return places


def output_area():
    """The area a new PDF goes into, created if need be."""
    path = areas()[0][1]
    os.makedirs(path, exist_ok=True)
    return path


def reject_schematic(*paths):
    for path in paths:
        if path.lower().endswith(SCH_EXT):
            die("only boards are compared, not schematics: %s" % shown(path))


def version_plots(plotter):
    """Every plot one version contributes: its outline both ways, and each of its layers."""
    wanted = [(CUT_LAYER, mirror, drill) for mirror in (False, True) for drill in (2, 0)]
    for name in plotter.names:
        wanted.append((name, name.startswith("B."), default_drill(name)))
    return wanted


def plots_equal(old_pdf, new_pdf):
    """Whether two plots are the same drawing: kicad-cli writes byte-identical PDFs for
    identical layers, the creation timestamp aside."""
    stamp = re.compile(rb"/CreationDate \(D:[^)]*\)")
    with open(old_pdf, "rb") as handle:
        old = stamp.sub(b"", handle.read())
    with open(new_pdf, "rb") as handle:
        new = stamp.sub(b"", handle.read())
    return old == new


def plot_key(name):
    return name, name.startswith("B."), default_drill(name)


def layer_mask(plotter, name):
    """The layer's artwork as a hard mask, or None when the version has no such layer."""
    if name not in plotter.stored:
        return None
    return sharp(plotter.raster(*plot_key(name)))


def version_masks(old, new, name):
    """The layer as (old, new) masks, an absent side standing in as empty."""
    masks = [layer_mask(old, name), layer_mask(new, name)]
    for index in (0, 1):
        if masks[index] is None:
            masks[index] = Image.new("L", masks[1 - index].size, 0)
    if masks[0].size != masks[1].size:
        die("the two versions use different sheet sizes, so their plots cannot be compared")
    return masks


def holes(plotter, mirror):
    """The drill holes alone: the outline plot carrying them, less the one without."""
    with_drills = sharp(plotter.raster(CUT_LAYER, mirror, 2))
    without = sharp(plotter.raster(CUT_LAYER, mirror, 0))
    return ImageChops.subtract(with_drills, without)


def drills_differ(old, new, mirror):
    return not plots_equal(old.one(CUT_LAYER, mirror, 2), new.one(CUT_LAYER, mirror, 2))


def body(new, old, mirror):
    """What every page has under its artwork: the new version's substrate with the holes of
    both versions punched, and a hole of one version only tinted toward its color, so the
    drills are compared on every page without a drawing of their own.

    Pasted, not alpha-composited: a flat color through a mask is the simpler operation,
    and the page is RGB throughout, which is a quarter less memory to push around.
    """
    mask = body_mask(new, mirror)
    under = Image.new("RGB", mask.size, PAGE)
    if not drills_differ(old, new, mirror):
        under.paste(SUBSTRATE, mask=mask)
        return under
    old_holes, new_holes = holes(old, mirror), holes(new, mirror)
    both = ImageChops.multiply(old_holes, new_holes)
    under.paste(SUBSTRATE, mask=ImageChops.subtract(mask, old_holes))
    under.paste(OLD_HOLE, mask=ImageChops.subtract(old_holes, both))
    under.paste(NEW_HOLE, mask=ImageChops.subtract(new_holes, both))
    return under


def outline_under(new, old, mirror):
    """What a Fab page has under its drawing: white, with the outlines of both versions, as
    the layer PDF draws them. Antialiased, since without it a thin outline breaks up."""
    lines = [artwork(plotter.one(CUT_LAYER, mirror, 0), plotter.density)
        for plotter in (old, new)]
    under = Image.new("RGB", lines[0].size, PAGE)
    under.paste(OUTLINE_INK, mask=ImageChops.lighter(*lines))
    return under


def diff_page(name, old, new, under, rect):
    """One page: the body, the unchanged artwork faint, the old-only red, the new-only cyan.
    On a Fab page the unchanged drawing is faint ink, as faint white would vanish on white."""
    both = ImageChops.multiply(old, new)
    page = under.copy()
    page.paste(INK if name in OUTLINE_ONLY else SAME_COLOR, mask=dimmed(both, SAME_ALPHA))
    page.paste(OLD_COLOR, mask=ImageChops.subtract(old, both))
    page.paste(NEW_COLOR, mask=ImageChops.subtract(new, both))
    return captioned(page.crop(rect), name)


def page_rect(old, new, mirror, size):
    """The crop of a page: the outlines of both versions with the margin, so that nothing of
    either is left out when the outline itself changed."""
    a, b = outline_box(old, mirror), outline_box(new, mirror)
    return panel_rect((min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])),
        size)


def summary_lines(labels, names, changed, old, new):
    same = [name for name in names if name not in changed]
    only_old = [name for name in old.names if name not in new.names]
    only_new = [name for name in new.names if name not in old.names]
    lines = ["Old: " + labels[0], "New: " + labels[1], "",
        "Changed layers: " + (", ".join(changed) or "none"),
        "Unchanged layers: " + (", ".join(same) or "none")]
    if only_old:
        lines.append("Only the old version has: " + ", ".join(only_old))
    if only_new:
        lines.append("Only the new version has: " + ", ".join(only_new))
    return lines


def compare(old_file, new_file, labels, out, all_layers, work, remember=None):
    """Write the PDF, and return the names of the layers that differ.

    remember names a directory in which the board's scale on its sheet is kept between runs.
    """
    scale, _aspect = board_scale(new_file, work, remember)
    # Both versions get the new one's project file, if it has one: the old side usually
    # comes from git without one, and text variables resolved on one side only would show
    # as changes nobody made. No antialiasing: with every pixel wholly one thing, the three
    # colors meet edge to edge, and no fringe pixel has to be judged for or against a change.
    project = sibling_project(new_file)
    old = Plotter(old_file, os.path.join(work, "old"), scale, antialias=False, project=project)
    new = Plotter(new_file, os.path.join(work, "new"), scale, antialias=False, project=project)
    parallel([partial(old.plot, version_plots(old)), partial(new.plot, version_plots(new))])

    # The new version's layers in its own order, then any the old one had and it lost. A
    # layer whose two plots are the same bytes is settled without rendering either.
    names = new.names + [name for name in old.names if name not in new.names]
    same = [name for name in names if name in old.stored and name in new.stored
        and plots_equal(old.one(*plot_key(name)), new.one(*plot_key(name)))]
    unsettled = [name for name in names if name not in same]
    if all_layers:
        unsettled = names
    # The bodies first in the batch, since they take longest: they label the regions of
    # the outline plot while the layers behind them are still being rasterized.
    results = parallel([partial(body, new, old, False), partial(body, new, old, True),
        partial(outline_under, new, old, False), partial(outline_under, new, old, True)]
        + [partial(version_masks, old, new, name) for name in unsettled])
    bodies = {False: results[0], True: results[1]}
    outlines = {False: results[2], True: results[3]}
    masks = dict(zip(unsettled, results[4:]))
    rects = dict((mirror, page_rect(old, new, mirror, bodies[mirror].size))
        for mirror in (False, True))
    changed = [name for name in unsettled if name not in same
        and ImageChops.difference(*masks[name]).getbbox()]

    jobs = []
    for name in sided_order(names):
        mirror = name.startswith("B.")
        if name in changed or all_layers:
            under = outlines[mirror] if name in OUTLINE_ONLY else bodies[mirror]
            jobs.append(partial(diff_page, name, masks[name][0], masks[name][1], under,
                rects[mirror]))
        elif counterpart(name) in changed:
            jobs.append(partial(placeholder_page, name, UNCHANGED_WORD,
                page_size(rects[mirror])))
    jobs.append(partial(text_page, SUMMARY_TITLE, summary_lines(labels, names, changed, old,
        new), page_size(rects[False])))
    write_pdf(parallel(jobs), out, DPI, DEFLATE_LEVEL)
    return changed


def run_compare(old_file, new_file, labels, out, all_layers):
    work = tempfile.mkdtemp(prefix="ki-diff-")
    try:
        return compare(old_file, new_file, labels, out, all_layers, work, output_area())
    finally:
        shutil.rmtree(work, ignore_errors=True)


def report(changed):
    if changed:
        print("[ki] changed: " + ", ".join(changed))
    else:
        print("[ki] no layer differs")


def mode_git_diff(args):
    """git external-diff driver: path old-file old-hex old-mode new-file new-hex new-mode."""
    if len(args.files) != 7:
        die("git passes 7 arguments to a diff driver; got %d. Is this being run by hand?"
            % len(args.files))
    name, old_file, old_hex, _old_mode, new_file, new_hex, _new_mode = args.files
    reject_schematic(name)
    # git uses an all-zero hash for the not-yet-committed working-tree file.
    is_worktree = not int(new_hex, 16)
    new_label = "working tree" if is_worktree else new_hex[:8]
    out = args.output or os.path.join(output_area(), "%s_%s_to_%s.pdf"
        % (safe_name(os.path.basename(name)), old_hex[:8],
        "worktree" if is_worktree else new_hex[:8]))
    changed = run_compare(old_file, new_file, ("%s at %s" % (name, old_hex[:8]),
        "%s, %s" % (name, new_label)), out, args.all_layers)
    # A diff driver's stdout is shown in git's own output.
    report(changed)
    print("[ki] PDF: " + shown(out))
    return 0


def mode_files(args):
    """Two files, from Fork or by hand: compare, then open the PDF."""
    if len(args.files) != 2:
        die("two board files are compared; got %d" % len(args.files))
    old_file, new_file = args.files
    reject_schematic(old_file, new_file)
    if args.output:
        out = args.output
    else:
        key = hashlib.sha1((os.path.abspath(old_file) + "|" + os.path.abspath(new_file))
            .encode("utf-8")).hexdigest()[:8]
        out = os.path.join(output_area(), "%s_%s.pdf"
            % (safe_name(os.path.basename(new_file))[:60], key))
        # Diffing the same pair twice is normal, and a viewer still holding the last PDF
        # keeps it from being overwritten, so that case gets a fresh name.
        if os.path.isfile(out):
            try:
                os.remove(out)
            except OSError:
                out = "%s_%d.pdf" % (out[:-4], os.getpid())
    changed = run_compare(old_file, new_file, (shown(os.path.abspath(old_file)),
        shown(os.path.abspath(new_file))), out, args.all_layers)
    report(changed)
    note("PDF: " + shown(out))
    if not args.no_open:
        open_path(out)
    return 0


def mode_clean():
    """Empty both areas, and remove them."""
    removed, freed = 0, 0
    for label, path in areas():
        if not os.path.isdir(path):
            print("%s  %s  (absent)" % (label, shown(path)))
            continue
        print("%s  %s" % (label, shown(path)))
        for entry in sorted(os.listdir(path)):
            full = os.path.join(path, entry)
            if not os.path.isfile(full):
                continue
            size = os.path.getsize(full)
            try:
                os.remove(full)
            except OSError as error:
                note("%s could not be removed (%s)" % (shown(full), error.strerror or error))
                continue
            removed += 1
            freed += size
        try:
            os.rmdir(path)
        except OSError:
            pass
    print("Removed %d file(s), %.1f MB." % (removed, freed / (1024.0 * 1024.0)))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="ki diff", description=DESCRIPTION, epilog=EPILOG,
        formatter_class=help_formatter)
    parser.add_argument("files", nargs="*", metavar="FILE",
        help="the old and the new board, or with --git-diff what git passes")
    parser.add_argument("--git-diff", action="store_true",
        help="act as git's external diff driver")
    parser.add_argument("--clean", action="store_true",
        help="remove what earlier runs left behind")
    parser.add_argument("--all-layers", action="store_true",
        help="a page for every layer, not only the ones that differ")
    parser.add_argument("-o", "--output", metavar="PDF", help="where to write the PDF")
    parser.add_argument("--no-open", action="store_true",
        help="do not open the PDF once written")
    args = parser.parse_args()
    if args.clean:
        return mode_clean()
    if not args.files:
        parser.print_help()
        return 0
    require_imaging()
    if args.git_diff:
        return mode_git_diff(args)
    return mode_files(args)


if __name__ == "__main__":
    exit_with(main)
