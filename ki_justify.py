#!/usr/bin/env python3
"""Change a text or field justification without moving what you see on the board."""
# Written with the help of Claude Opus 5.
# Wrappers on PATH: ki-justify.cmd for cmd.exe, ki-justify for cygwin and git-bash.
# Create them with ki_install.py.

import argparse
import glob
import os
import re
import subprocess
import sys

DESCRIPTION = "Rejustify a text or field, shifting it so its position on the board is kept."

EPILOG = """\
Usage

  ki-justify TEXT JUSTIFY JUSTIFY [FILE]

  TEXT     the exact contents of the text or field, matched whole, never as a substring
  JUSTIFY  the wanted justification, one word per axis
  FILE     a .kicad_pcb or a .kicad_mod; the only .kicad_pcb here by default

  The two words give the justification to end up with: left, center or right horizontally,
  top, center or bottom vertically. Only left/right and top/bottom name an axis, so "center"
  takes whichever axis the other word leaves free. Order carries no meaning:

      ki-justify J2 center left       left, vertically centered
      ki-justify J2 left center       the same
      ki-justify J2 bottom left       left and bottom
      ki-justify J2 center center     centered both ways

  Two words for the same axis are refused. A justification of center on both axes with no
  mirroring is what KiCad expresses by leaving the form out, so this writes it out the same
  way, by deleting it rather than spelling out the defaults.

Why the position is kept

  Justification decides which point of the text sits on its anchor, so changing it slides
  the text sideways by half its width, or vertically by half its height. The anchor is moved
  back by the same amount, leaving the drawing unchanged.

  The width is measured by KiCad rather than guessed: its stroke font is proportional, an
  'I' being about 0.63 of the text size against 1.30 for a 'W', so counting characters would
  be wrong by up to half the width. A field reading ${REFERENCE} is measured as what it
  renders to, not as the variable.

  A text inside a footprint stores its anchor relative to that footprint, turned by the
  footprint's own rotation and flip, so the move is asked of KiCad rather than added to the
  stored numbers, which would come out rotated, or doubled for a footprint at 180.

  The new anchor is rounded to the --snap grid, 0.01 mm by default, which leaves the text up
  to half that off its exact spot. Pass --snap 0 to place it exactly and accept a coordinate
  with six decimals. An axis that did not move keeps its original text either way, so a
  coordinate already off the grid is not quietly pulled onto it.

  Only the two lines that must change are rewritten, so the diff stays small and the file's
  own line endings, formatting and ordering survive untouched.

When more than one matches

  Nothing is written, and every match is listed with its kind, layer and position, so you
  can tell them apart. Board texts, footprint fields and footprint texts are all searched.
"""

HORIZONTAL = ("left", "center", "right")
VERTICAL = ("top", "center", "bottom")
RELAUNCH_FLAG = "KI_JUSTIFY_UNDER_KICAD_PYTHON"
SNAP_MM = 0.01  # grid the new anchor is rounded to

# The three shapes a piece of text takes in these files. The captured group is the content.
HOLDERS = (re.compile(r'\(gr_text\s+"((?:[^"\\]|\\.)*)"'),
    re.compile(r'\(fp_text\s+\w+\s+"((?:[^"\\]|\\.)*)"'),
    re.compile(r'\(property\s+"(?:[^"\\]|\\.)*"\s+"((?:[^"\\]|\\.)*)"'))

AT = re.compile(r'\(at\s+(-?[\d.]+)\s+(-?[\d.]+)((?:\s+-?[\d.]+)?)\s*\)')
JUSTIFY = re.compile(r'\(justify([^)]*)\)')
EFFECTS = re.compile(r'\(effects\b')


def die(message, code=2):
    sys.stderr.write("[justify] " + message + "\n")
    sys.exit(code)


def shown(path):
    return path.replace("\\", "/")


def version_key(text):
    """Sort "10.0" above "9.0", which a plain string sort gets backwards."""
    parts = [int(chunk) for chunk in re.split(r"[^0-9]+", text) if chunk]
    return parts or [0]


def find_kicad_python():
    """KiCad's bundled interpreter is the only one carrying pcbnew. Prefer the newest."""
    override = os.environ.get("KICAD_PYTHON")
    if override:
        return override if os.path.isfile(override) else None
    roots = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "KiCad"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "KiCad")]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            candidate = os.path.join(root, entry, "bin", "python.exe")
            if os.path.isfile(candidate):
                found.append((version_key(entry), candidate))
    found.sort()
    return found[-1][1] if found else None


def ensure_kicad_python():
    """Re-run under KiCad's Python when pcbnew is missing, so any python on PATH will do."""
    try:
        import pcbnew  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get(RELAUNCH_FLAG):
        die("pcbnew is still not importable under %s, KiCad's own Python." % sys.executable)
    target = find_kicad_python()
    if not target:
        die("no KiCad Python found; set KICAD_PYTHON to its python.exe")
    # subprocess, not execv: on Windows exec detaches and loses the exit status.
    sys.exit(subprocess.call([target, os.path.realpath(__file__)] + sys.argv[1:],
        env=dict(os.environ, **{RELAUNCH_FLAG: "1"})))


def default_board():
    hits = sorted(glob.glob("*.kicad_pcb"))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        die("no .kicad_pcb here; name one, or run inside the project directory")
    die("several .kicad_pcb here (%s); name the one you mean" % ", ".join(hits))


def target_pair(words):
    """The wanted (horizontal, vertical) justification from two words, in either order.

    Only left/right and top/bottom name an axis; "center" takes whichever is left over, so
    "center left" and "left center" both mean left and vertically centered, and "center
    center" means centered on both. That is also what KiCad means by omitting the form.
    """
    unknown = [w for w in words if w not in set(HORIZONTAL) | set(VERTICAL)]
    if unknown:
        die("unknown justification %s; use %s or %s"
            % (" and ".join(repr(w) for w in unknown), "/".join(HORIZONTAL),
                "/".join(VERTICAL)))
    horizontal = [w for w in words if w in HORIZONTAL and w != "center"]
    vertical = [w for w in words if w in VERTICAL and w != "center"]
    if len(horizontal) > 1:
        die("%s are both horizontal; give one per axis" % " and ".join(map(repr, horizontal)))
    if len(vertical) > 1:
        die("%s are both vertical; give one per axis" % " and ".join(map(repr, vertical)))
    return (horizontal[0] if horizontal else "center",
        vertical[0] if vertical else "center")


def form_end(text, start):
    """Index just past the closing paren of the form starting at `start`."""
    depth, i, in_string = 0, start, False
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    die("unbalanced parentheses near offset %d" % start)


def find_holders(text, wanted):
    """Every text-bearing form whose content is exactly `wanted`, as (start, end) spans."""
    spans = []
    for pattern in HOLDERS:
        for match in pattern.finditer(text):
            if match.group(1) != wanted:
                continue
            start = text.rindex("(", 0, match.end(1))
            start = match.start()
            spans.append((start, form_end(text, start)))
    return sorted(set(spans))


def describe(text, span):
    """A short line naming what a matched form is and where it sits."""
    body = text[span[0]:span[1]]
    kind = body[1:body.index(" ")] if " " in body[:40] else "?"
    at = AT.search(body)
    layer = re.search(r'\(layer\s+"([^"]*)"', body)
    line = text.count("\n", 0, span[0]) + 1
    return ("line %d: %s at %s, %s on %s"
        % (line, kind, at.group(1) if at else "?", at.group(2) if at else "?",
            layer.group(1) if layer else "?"))


def measure(path, wanted, horizontal, vertical):
    """The anchor the file should carry after rejustifying, plus how it moved on the board.

    The move is worked out in board coordinates, but a text inside a footprint stores its
    anchor relative to that footprint, turned by the footprint's own rotation and flip. So
    KiCad is asked for the resulting relative position rather than the shift being added to
    the stored numbers, which would come out rotated, or doubled for a footprint at 180.
    """
    import pcbnew
    if path.lower().endswith(".kicad_mod"):
        container = pcbnew.FootprintLoad(os.path.dirname(os.path.abspath(path)),
            os.path.splitext(os.path.basename(path))[0])
        if container is None:
            die("cannot read the footprint %s" % shown(path))
        footprints, drawings = [container], []
    else:
        container = pcbnew.LoadBoard(path)
        footprints, drawings = list(container.GetFootprints()), list(container.GetDrawings())

    items = [d for d in drawings if "TEXT" in d.GetClass()]
    for footprint in footprints:
        items += list(footprint.GetFields())
        items += [g for g in footprint.GraphicalItems() if "TEXT" in g.GetClass()]
    items = [i for i in items if i.GetText() == wanted]
    if not items:
        die("KiCad does not see a text reading %r in %s" % (wanted, shown(path)), code=1)

    item = items[0]
    horizontal_values = {"left": pcbnew.GR_TEXT_H_ALIGN_LEFT,
        "center": pcbnew.GR_TEXT_H_ALIGN_CENTER, "right": pcbnew.GR_TEXT_H_ALIGN_RIGHT}
    vertical_values = {"top": pcbnew.GR_TEXT_V_ALIGN_TOP,
        "center": pcbnew.GR_TEXT_V_ALIGN_CENTER, "bottom": pcbnew.GR_TEXT_V_ALIGN_BOTTOM}
    was = (justify_word(horizontal_values, item.GetHorizJustify()),
        justify_word(vertical_values, item.GetVertJustify()))
    before = item.GetBoundingBox().GetCenter()
    item.SetHorizJustify(horizontal_values[horizontal])
    item.SetVertJustify(vertical_values[vertical])
    after = item.GetBoundingBox().GetCenter()
    shift = pcbnew.VECTOR2I(before.x - after.x, before.y - after.y)

    here = item.GetPosition()
    item.SetPosition(pcbnew.VECTOR2I(here.x + shift.x, here.y + shift.y))
    inside = hasattr(item, "GetFPRelativePosition") and item.GetParentFootprint() is not None
    anchor = item.GetFPRelativePosition() if inside else item.GetPosition()
    return (pcbnew.ToMM(anchor.x), pcbnew.ToMM(anchor.y)), was, \
        (pcbnew.ToMM(shift.x), pcbnew.ToMM(shift.y))


def justify_word(values, value):
    for word, candidate in values.items():
        if candidate == value:
            return word
    return str(value)


def number(value):
    """KiCad's own style: a plain integer where it can be, no trailing zeros."""
    text = "%.6f" % value
    text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def snapped(value, step):
    return round(value / step) * step if step > 0 else value


def rewrite(body, horizontal, vertical, anchor, step):
    """The form with its anchor replaced and its justification set on both axes.

    An axis that did not move keeps its original text, so a coordinate that was already off
    the grid is not quietly pulled onto it by a change to the other axis.
    """
    at = AT.search(body)
    if not at:
        die("the matched form has no (at ...) to move")
    written, error = [], []
    for old_text, wanted in zip((at.group(1), at.group(2)), anchor):
        old = float(old_text)
        if abs(wanted - old) <= 1e-9:
            written.append(old_text)
            error.append(0.0)
            continue
        value = snapped(wanted, step)
        written.append(number(value))
        error.append(value - wanted)
    moved = "(at %s %s%s)" % (written[0], written[1], at.group(3))
    body = body[:at.start()] + moved + body[at.end():]

    justify = JUSTIFY.search(body)
    # KiCad omits the whole form when nothing but the defaults is left, so this does too.
    keep = [w for w in (horizontal, vertical) if w != "center"]
    if justify and "mirror" in justify.group(1).split():
        keep.append("mirror")

    if justify and keep:
        return (body[:justify.start()] + "(justify %s)" % " ".join(keep)
            + body[justify.end():]), error
    if justify:
        return drop_line(body, justify.start(), justify.end()), error
    if not keep:
        return body, error

    effects = EFFECTS.search(body)
    if not effects:
        die("the matched form has no (effects ...) to hold a justification")
    # Insert on its own line above the closing paren, indented like the other children.
    line_start = form_end(body, effects.start()) - 1
    while line_start > 0 and body[line_start - 1] in " \t":
        line_start -= 1
    child = re.search(r"\n([ \t]*)", body[effects.start():])
    eol = "\r\n" if "\r\n" in body else "\n"
    return (body[:line_start] + (child.group(1) if child else "")
        + "(justify %s)" % " ".join(keep) + eol + body[line_start:]), error


def drop_line(body, start, end):
    """Remove a form together with its indentation and the newline it sat on."""
    while start > 0 and body[start - 1] in " \t":
        start -= 1
    if body.startswith("\r\n", end):
        end += 2
    elif body.startswith("\n", end):
        end += 1
    return body[:start] + body[end:]


def main():
    parser = argparse.ArgumentParser(
        prog="ki-justify", description=DESCRIPTION, epilog=EPILOG,
        formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(prog, width=99))
    parser.add_argument("text", metavar="TEXT", help="exact contents of the text or field")
    parser.add_argument("justify", nargs=2, metavar="JUSTIFY",
        help="the wanted justification, one word per axis, in either order")
    parser.add_argument("file", nargs="?", metavar="FILE",
        help="a .kicad_pcb or .kicad_mod; the only .kicad_pcb here by default")
    parser.add_argument("--snap", type=float, default=SNAP_MM, metavar="MM",
        help="round the new anchor to this grid, 0 to place it exactly, default %g" % SNAP_MM)
    args = parser.parse_args()
    if args.snap < 0:
        die("--snap cannot be negative")

    horizontal, vertical = target_pair([w.lower() for w in args.justify])
    path = args.file or default_board()
    if not os.path.isfile(path):
        die("not a file: %s" % shown(path))

    raw = open(path, "rb").read()
    text = raw.decode("utf-8", "replace")
    spans = find_holders(text, args.text)
    if not spans:
        die("no text or field reads exactly %r in %s" % (args.text, shown(path)), code=1)
    if len(spans) > 1:
        print("%d items read %r, so nothing was changed:" % (len(spans), args.text))
        for span in spans:
            print("  " + describe(text, span))
        return 1

    ensure_kicad_python()
    anchor, was, shift = measure(path, args.text, horizontal, vertical)
    if (horizontal, vertical) == was:
        print("%r is already justified %s %s; nothing changed" % (args.text, was[0], was[1]))
        return 0

    body = text[spans[0][0]:spans[0][1]]
    new_body, error = rewrite(body, horizontal, vertical, anchor, args.snap)
    out = text[:spans[0][0]] + new_body + text[spans[0][1]:]
    open(path, "wb").write(out.encode("utf-8"))
    print("%r: %s %s -> %s %s, anchor moved %+.4f, %+.4f mm."
        % (args.text, was[0], was[1], horizontal, vertical, shift[0], shift[1]))
    off = max(abs(e) for e in error)
    if off:
        print("Snapped to the %g mm grid, leaving it %.4f mm off the exact spot."
            % (args.snap, off))
    return 0


if __name__ == "__main__":
    sys.exit(main())
