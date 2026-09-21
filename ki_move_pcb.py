#!/usr/bin/env python3
"""Move a KiCad board on its sheet, and verify that a move changed nothing else."""
# Written with the help of Claude Opus 5.
# Wrappers on PATH: ki-move-pcb.cmd for cmd.exe, ki-move-pcb for cygwin and git-bash.
# Create them with ki_install.py.

import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile

# pcbnew is imported lazily, inside the centering path only: --cmp is purely textual and has
# to run under any Python, while --center needs KiCad's own interpreter.

DESCRIPTION = "Move a KiCad board on its drawing sheet, or check that a move only shifted it."

EPILOG = """\
Moving

  ki-move-pcb --center
      Put the board in the middle of the usable area: the sheet minus the frame margin,
      minus a strip along the bottom kept clear for the title block. The widths of both
      come from --margin and --title-block, and the result is rounded to the --snap grid.

  ki-move-pcb --shift DX DY
      Move by that many millimeters. Taken literally, with no snapping.

  ki-move-pcb --to X Y [--ref board|aux|grid]
      Move so that the reference point lands exactly on those millimeter coordinates.
      --ref chooses the point: the outline's top-left corner (board, the default), the
      drill/place origin (aux) or the grid origin (grid). No snapping here either.

  Any of these acts on --pcb, or on the only .kicad_pcb in the current directory. The whole
  board moves as one rigid body, origins included, so relative coordinates survive, and the
  result is checked as below before the file is written. Run with no arguments inside the
  PCB editor's scripting console to center the board open there:

      exec(open('ki_move_pcb.py').read())

Checking a move

  ki-move-pcb --cmp
      Compare the last save against the one before it, from KiCad's local history. Use
      --history COMMIT to reach an earlier snapshot.

  ki-move-pcb --cmp --git [COMMIT]
      Compare the board on disk against a project revision, HEAD by default.

  ki-move-pcb --cmp OLD.kicad_pcb [NEW.kicad_pcb]
      Compare two files. NEW defaults to the board --pcb names.

  Save the board before you start moving it. KiCad's local history commits on every save, so
  a save immediately beforehand makes the next snapshot contain the move and nothing else,
  which is both the smallest diff and the clearest answer from this check.

  The check is textual and needs no KiCad. Both files must have identical structure; then
  every number that changed must be a coordinate, X values must all shift by the same dx and
  Y values by the same dy. Anything else, such as a changed width, size, drill, pad offset,
  3D model placement or rotation angle, fails, because a rigid move cannot touch them.

  Coordinates nested inside a footprint are relative to it and correctly do not move; only
  the footprint's own position does.

Exit status is 0 when the check passes, 1 when it fails, 2 on a usage or environment error.
"""

MARGIN_MM = 10.0  # frame border offset (KiCad default)
TITLE_BLOCK_MM = 35.0  # reserved strip along the bottom edge
SNAP_MM = 1.0  # round final lower-left to this grid; 0 = off

# Keys whose arguments are absolute board coordinates, so a rigid move may change them.
# Deliberately absent: offset (relative to a pad) and xyz (relative to a footprint).
COORD_KEYS = frozenset(("at", "start", "end", "mid", "center", "xy"))

# Landscape millimeters; (paper "X" portrait) swaps them, (paper "User" w h) overrides.
PAPER_MM = {"A0": (1189, 841), "A1": (841, 594), "A2": (594, 420), "A3": (420, 297),
    "A4": (297, 210), "A5": (210, 148), "A": (279.4, 215.9), "B": (431.8, 279.4),
    "C": (558.8, 431.8), "D": (863.6, 558.8), "E": (1117.6, 863.6),
    "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9),
    "USLedger": (431.8, 279.4)}

ORIGIN = re.compile(r"\s*\((aux_axis_origin|grid_origin)\s+(-?[\d.]+)\s+(-?[\d.]+)\)")
TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\Z")
TOLERANCE = 1e-9
REPORT_LIMIT = 20


def die(message, code=2):
    sys.stderr.write("[move-pcb] " + message + "\n")
    sys.exit(code)


def note(message):
    sys.stderr.write("[move-pcb] " + message + "\n")


# --- textual comparison ---------------------------------------------------------------------

def scan(text):
    """Split a board into a structural skeleton and the numbers hanging off it.

    Returns (skeleton, lines, numbers), where numbers are (key, argument index, value, line)
    and the skeleton carries '#' in place of each number, so two boards can be compared for
    structure first and for values second.
    """
    skeleton, lines, numbers = [], [], []
    stack = []  # one [head, argument count] frame per open list
    line, pos = 1, 0
    for match in TOKEN.finditer(text):
        line += text.count("\n", pos, match.start())
        pos = match.end()
        token = match.group()
        if token == "(":
            stack.append([None, 0])
            skeleton.append("(")
        elif token == ")":
            if stack:
                stack.pop()
            skeleton.append(")")
        elif stack and stack[-1][0] is None:
            stack[-1][0] = token  # first atom of a list is its key
            skeleton.append(token)
        else:
            if stack:
                stack[-1][1] += 1
            if NUMBER.match(token):
                key = stack[-1][0] if stack else ""
                index = stack[-1][1] if stack else 0
                numbers.append((key, index, float(token), line))
                skeleton.append("#")
            else:
                skeleton.append(token)
        lines.append(line)
        line += token.count("\n")
    return skeleton, lines, numbers


def structural_diff(old, new):
    """First structural difference as a message, or None when the skeletons match."""
    sk_a, ln_a, _ = old
    sk_b, ln_b, _ = new
    for i in range(min(len(sk_a), len(sk_b))):
        if sk_a[i] != sk_b[i]:
            return ("structure differs: old line %d has %r, new line %d has %r"
                % (ln_a[i], sk_a[i], ln_b[i], sk_b[i]))
    if len(sk_a) != len(sk_b):
        longer, at = ("new", ln_b[len(sk_a)]) if len(sk_b) > len(sk_a) else ("old",
            ln_a[len(sk_b)])
        return "structure differs: the %s file has extra content from line %d" % (longer, at)
    return None


def split_origins(text):
    """Lift the origins out of the text, defaulting to 0,0 when absent.

    KiCad omits (aux_axis_origin ...) and (grid_origin ...) entirely once they sit at 0,0,
    so an origin moved exactly onto the corner would otherwise read as a structural change.
    """
    found = {}
    for match in ORIGIN.finditer(text):
        found[match.group(1)] = (float(match.group(2)), float(match.group(3)))
    for key in ("aux_axis_origin", "grid_origin"):
        found.setdefault(key, (0.0, 0.0))
    return ORIGIN.sub("", text), found


def compare(old_text, new_text):
    """Check that new_text is old_text moved rigidly. Returns (dx, dy, problems, changed)."""
    old_body, old_origins = split_origins(old_text)
    new_body, new_origins = split_origins(new_text)
    old, new = scan(old_body), scan(new_body)
    broken = structural_diff(old, new)
    if broken:
        return None, None, [broken], {}

    dx = dy = None
    problems, changed = [], {}
    for (key, index, before, _), (_, _, after, line) in zip(old[2], new[2]):
        delta = after - before
        if abs(delta) <= TOLERANCE:
            continue
        changed[key] = changed.get(key, 0) + 1
        if key not in COORD_KEYS:
            problems.append("line %d: '%s' is not a coordinate, yet changed by %+g"
                % (line, key, delta))
            continue
        if index not in (1, 2):
            problems.append("line %d: '%s' argument %d is not X or Y, yet changed by %+g"
                % (line, key, index, delta))
            continue
        axis, seen = ("X", dx) if index == 1 else ("Y", dy)
        if seen is None:
            if index == 1:
                dx = delta
            else:
                dy = delta
        elif abs(delta - seen) > TOLERANCE:
            problems.append("line %d: '%s' moved %s by %+g, but %+g elsewhere"
                % (line, key, axis, delta, seen))

    dx, dy = dx or 0.0, dy or 0.0
    for key in sorted(old_origins):
        was, now = old_origins[key], new_origins[key]
        moved = (now[0] - was[0], now[1] - was[1])
        if abs(moved[0]) <= TOLERANCE and abs(moved[1]) <= TOLERANCE:
            continue
        changed[key] = 2
        if abs(moved[0] - dx) > TOLERANCE or abs(moved[1] - dy) > TOLERANCE:
            problems.append("'%s' moved by %+g, %+g, but the board by %+g, %+g"
                % (key, moved[0], moved[1], dx, dy))
    return dx, dy, problems, changed


def report(dx, dy, problems, changed, label):
    """Print the verdict. Returns the process exit status."""
    if changed:
        summary = ", ".join("%s(%d)" % (k, n) for k, n in sorted(changed.items()))
        print("Changed coordinates: %s." % summary)
    if problems:
        print("%s: FAILED, this is not a rigid move." % label)
        for line in problems[:REPORT_LIMIT]:
            print("  " + line)
        if len(problems) > REPORT_LIMIT:
            print("  ... and %d more." % (len(problems) - REPORT_LIMIT))
        return 1
    if dx == 0.0 and dy == 0.0:
        print("%s: identical, nothing moved." % label)
    else:
        print("%s: rigid move by %+.6g, %+.6g mm, nothing else changed." % (label, dx, dy))
    return 0


# --- reference versions ---------------------------------------------------------------------

def git_out(cwd, args):
    try:
        done = subprocess.run(["git"] + args, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
    except OSError:
        die("git is not on PATH, needed for --history and --git")
    if done.returncode:
        die("git %s failed: %s" % (" ".join(args), done.stderr.decode("utf-8", "replace").strip()))
    return done.stdout


def read_text(path):
    if not os.path.isfile(path):
        die("not a file: %s" % path)
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def default_board():
    hits = sorted(glob.glob("*.kicad_pcb"))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        die("no .kicad_pcb here; name one explicitly")
    die("several .kicad_pcb here (%s); name one explicitly" % ", ".join(hits))


def from_history(board, commit):
    """Last saved snapshot against an earlier one, from KiCad's local history repository."""
    project = os.path.dirname(os.path.abspath(board)) or "."
    history = os.path.join(project, ".history")
    if not os.path.isdir(os.path.join(history, ".git")):
        die("no KiCad local history at %s (File > Local History enables it)" % history)
    name = os.path.basename(board)
    new = git_out(history, ["show", "HEAD:" + name]).decode("utf-8", "replace")
    old = git_out(history, ["show", "%s:%s" % (commit, name)]).decode("utf-8", "replace")
    if read_text(board) != new:
        note("the board on disk differs from its last snapshot; comparing snapshots, so "
            "unsaved changes are not included")
    return old, new, "History %s -> HEAD" % commit


def from_git(board, commit):
    """A project revision against the board as it currently sits on disk."""
    project = os.path.dirname(os.path.abspath(board)) or "."
    root = git_out(project, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip()
    relative = os.path.relpath(os.path.abspath(board), root).replace("\\", "/")
    old = git_out(root, ["show", "%s:%s" % (commit, relative)]).decode("utf-8", "replace")
    return old, read_text(board), "Git %s -> working tree" % commit


# --- centering ------------------------------------------------------------------------------

def ensure_kicad_python():
    """Re-run under KiCad's Python when pcbnew is missing, so any python on PATH will do."""
    try:
        import pcbnew  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("KICAD_PCB_POSITION_RELAUNCHED"):
        die("pcbnew is still missing under %s; is the KiCad install complete?" % sys.executable)
    override = os.environ.get("KICAD_PYTHON")
    found = [override] if override else []
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        root = os.path.join(base, "KiCad")
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root), key=lambda t: [int(p) for p in
            re.findall(r"\d+", t)] or [0]):
            found.append(os.path.join(root, entry, "bin", "python.exe"))
    target = next((p for p in reversed(found) if p and os.path.isfile(p)), None)
    if not target:
        die("no KiCad Python found; set KICAD_PYTHON to its python.exe")
    environment = dict(os.environ, KICAD_PCB_POSITION_RELAUNCHED="1")
    # subprocess, not execv: on Windows exec detaches and loses the exit status.
    sys.exit(subprocess.call([target, os.path.realpath(__file__)] + sys.argv[1:],
        env=environment))


def page_size_iu(board, path):
    """Page width/height in internal units.

    KiCad 10 exposes PAGE_INFO to Python as an opaque object with no size getters, so the
    older GetWidthIU/GetWidthMils shims no longer work. The sheet is declared in the file
    itself, which every version writes, so read it from there instead.
    """
    import pcbnew
    match = re.search(r'\(paper\s+"([^"]+)"((?:\s+[\w.]+)*)\s*\)', read_text(path))
    if not match:
        raise RuntimeError("no (paper ...) declaration in %s" % path)
    name, rest = match.group(1), match.group(2).split()
    if name == "User":
        if len(rest) < 2:
            raise RuntimeError("malformed (paper \"User\" ...) declaration")
        width_mm, height_mm = float(rest[0]), float(rest[1])
    else:
        if name not in PAPER_MM:
            raise RuntimeError("unknown paper size %r; use a standard size or User" % name)
        width_mm, height_mm = PAPER_MM[name]
    if "portrait" in rest:
        width_mm, height_mm = height_mm, width_mm
    return pcbnew.FromMM(width_mm), pcbnew.FromMM(height_mm)


def board_items(board):
    """Every movable object on the board."""
    for group in (board.GetFootprints(), board.GetTracks(), board.GetDrawings(),
        board.Zones()):
        for item in group:
            yield item


def snap(value, step_iu):
    if step_iu <= 0:
        return value
    return int(round(float(value) / step_iu) * step_iu)


def board_outline(board):
    bbox = board.GetBoardEdgesBoundingBox()
    if bbox.GetWidth() <= 0 or bbox.GetHeight() <= 0:
        raise RuntimeError("no Edge.Cuts outline found")
    return bbox


def reference_point(board, ref):
    """The point --to positions: the outline's top-left corner, or one of the origins."""
    import pcbnew
    if ref == "board":
        bbox = board_outline(board)
        return bbox.GetLeft(), bbox.GetTop()
    settings = board.GetDesignSettings()
    getter = "GetAuxOrigin" if ref == "aux" else "GetGridOrigin"
    if not hasattr(settings, getter):
        raise RuntimeError("this pcbnew build cannot read the %s origin" % ref)
    origin = getattr(settings, getter)()
    return origin.x, origin.y


def center_delta(board, path, margin_mm, title_mm, snap_mm):
    import pcbnew
    bbox = board_outline(board)
    page_w, page_h = page_size_iu(board, path)
    margin = pcbnew.FromMM(margin_mm)
    title = pcbnew.FromMM(title_mm)

    area_w = page_w - 2 * margin
    area_h = page_h - 2 * margin - title
    if area_w <= bbox.GetWidth() or area_h <= bbox.GetHeight():
        raise RuntimeError("board does not fit the usable area; use a larger sheet")

    target_x = margin + (area_w - bbox.GetWidth()) // 2
    target_y = margin + (area_h - bbox.GetHeight()) // 2
    step = pcbnew.FromMM(snap_mm)
    return pcbnew.VECTOR2I(snap(target_x - bbox.GetLeft(), step),
        snap(target_y - bbox.GetTop(), step))


def shift_delta(dx_mm, dy_mm):
    import pcbnew
    return pcbnew.VECTOR2I(pcbnew.FromMM(dx_mm), pcbnew.FromMM(dy_mm))


def to_delta(board, x_mm, y_mm, ref):
    """Move so that the reference point lands exactly on the given coordinates."""
    import pcbnew
    at_x, at_y = reference_point(board, ref)
    return pcbnew.VECTOR2I(pcbnew.FromMM(x_mm) - at_x, pcbnew.FromMM(y_mm) - at_y)


def apply_delta(board, delta):
    """Move every object, and the origins with them, so relative coordinates survive."""
    import pcbnew
    for item in board_items(board):
        item.Move(delta)
    settings = board.GetDesignSettings()
    for get, set_ in (("GetAuxOrigin", "SetAuxOrigin"), ("GetGridOrigin", "SetGridOrigin")):
        if hasattr(settings, get) and hasattr(settings, set_):
            origin = getattr(settings, get)()
            getattr(settings, set_)(pcbnew.VECTOR2I(origin.x + delta.x, origin.y + delta.y))
    return delta


def move_file(path, output, choose_delta):
    """Move a board on disk, then prove textually that only a shift happened."""
    import pcbnew
    board = pcbnew.LoadBoard(path)
    with tempfile.TemporaryDirectory(prefix="move-pcb-") as work:
        # KiCad rewrites the whole file on save, so compare against a re-serialized copy of
        # the input rather than the input itself; otherwise formatting swamps the real diff.
        baseline = os.path.join(work, "baseline.kicad_pcb")
        board.Save(baseline)
        delta = apply_delta(board, choose_delta(board, path))
        board.Save(output or path)
        print("Moved by %+.3f, %+.3f mm." % (pcbnew.ToMM(delta.x), pcbnew.ToMM(delta.y)))
        dx, dy, problems, changed = compare(read_text(baseline), read_text(output or path))
    return report(dx, dy, problems, changed, "Self-check")


def center_open_board():
    """The scripting-console path: act on the board already open in the editor."""
    import pcbnew
    board = pcbnew.GetBoard()
    delta = apply_delta(board, center_delta(board, board.GetFileName(), MARGIN_MM,
        TITLE_BLOCK_MM, SNAP_MM))
    pcbnew.Refresh()
    print("Moved by %+.3f, %+.3f mm." % (pcbnew.ToMM(delta.x), pcbnew.ToMM(delta.y)))
    return 0


# --- entry point ----------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="ki-move-pcb", description=DESCRIPTION, epilog=EPILOG,
        formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(prog, width=99))
    parser.add_argument("--pcb", metavar="FILE.kicad_pcb",
        help="the board to act on; default: the only .kicad_pcb in the current directory")
    parser.add_argument("--center", action="store_true",
        help="move the board to the middle of the usable area of its sheet")
    parser.add_argument("--shift", nargs=2, type=float, metavar=("DX", "DY"),
        help="move by this distance in millimeters")
    parser.add_argument("--to", nargs=2, type=float, metavar=("X", "Y"),
        help="move so that the reference point lands on these millimeter coordinates")
    parser.add_argument("--ref", choices=("board", "aux", "grid"), default="board",
        help="what --to positions: the outline's top-left corner (board, the default), the"
             " drill/place origin (aux) or the grid origin (grid)")
    parser.add_argument("--cmp", nargs="*", metavar="FILE.kicad_pcb",
        help="check that only a move happened; with no file, compares the last two saves")
    parser.add_argument("--history", nargs="?", const="HEAD~1", metavar="COMMIT",
        help="with --cmp: compare the last save against this one in KiCad's local history,"
             " default HEAD~1")
    parser.add_argument("--git", nargs="?", const="HEAD", metavar="COMMIT",
        help="with --cmp: compare the board on disk against this project revision,"
             " default HEAD")
    parser.add_argument("-o", "--output", metavar="FILE.kicad_pcb",
        help="write the moved board here instead of over the original")
    parser.add_argument("--margin", type=float, default=MARGIN_MM, metavar="MM",
        help="with --center: width of the sheet frame to keep clear, default %g" % MARGIN_MM)
    parser.add_argument("--title-block", type=float, default=TITLE_BLOCK_MM, metavar="MM",
        help="with --center: strip along the bottom to keep clear for the title block,"
             " default %g" % TITLE_BLOCK_MM)
    parser.add_argument("--snap", type=float, default=SNAP_MM, metavar="MM",
        help="with --center: round the result to this grid, 0 to place it exactly,"
             " default %g" % SNAP_MM)
    return parser


def run_compare(args):
    paths = args.cmp
    if args.history is not None and args.git is not None:
        die("--history and --git are alternatives; pick one")
    if paths and (args.history is not None or args.git is not None):
        die("give either file names or --history/--git, not both")
    if len(paths) > 2:
        die("--cmp takes at most two files, got %d" % len(paths))
    # The board is resolved only where it is actually used, so naming two files outright
    # works in a directory holding several boards.
    if args.git is not None:
        old, new, label = from_git(args.pcb or default_board(), args.git)
    elif paths:
        other = paths[1] if len(paths) > 1 else (args.pcb or default_board())
        old, new, label = read_text(paths[0]), read_text(other), "%s = %s" % (paths[0], other)
    else:
        old, new, label = from_history(args.pcb or default_board(), args.history or "HEAD~1")
    return report(*compare(old, new), label=label)


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.cmp is not None:
        return run_compare(args)

    modes = [m for m in ("center", "shift", "to") if getattr(args, m)]
    if len(modes) > 1:
        die("--center, --shift and --to are alternatives; pick one")
    if modes:
        ensure_kicad_python()
        board = args.pcb or default_board()
        if args.shift:
            choose = lambda _board, _path: shift_delta(*args.shift)  # noqa: E731
        elif args.to:
            choose = lambda b, _path: to_delta(b, args.to[0], args.to[1], args.ref)  # noqa: E731
        else:
            choose = lambda b, path: center_delta(b, path, args.margin,  # noqa: E731
                args.title_block, args.snap)
        return move_file(board, args.output, choose)
    try:  # no arguments: the scripting console, if a board is open
        import pcbnew
        if pcbnew.GetBoard() is not None:
            return center_open_board()
    except ImportError:
        pass
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
