#!/usr/bin/env python3
"""Run one of the ki tools for KiCad: ki VERB [ARGUMENTS]. See ki --help."""
# Written with the help of Claude Opus 5.
# Wrappers on PATH: ki.cmd for cmd.exe, and for git and Fork, which launch it by name; ki
# for cygwin and git-bash. Create them with ki install.

import importlib
import os
import stat
import sys

from ki_lib import die, exit_with, shown

# One line per tool: the verb, the module beside this file that implements it, and what
# it does. The module is imported only when its verb runs, so a tool that needs something
# this Python lacks, such as pcbnew, keeps every other verb working.
VERBS = (
    ("cut-signature", "ki_cut_signature",
        "cut the pixel signature out of a filled polygon in a footprint"),
    ("diff", "ki_diff", "run KiDiff on Windows: plain CLI, git diff driver, Fork diff tool"),
    ("draw-outline", "ki_draw_outline",
        "draw a connector on a schematic sheet, or make a footprint for one"),
    ("invert-logo", "ki_invert_logo", "invert a knocked-out logo footprint"),
    ("justify", "ki_justify", "change a text's justification without moving it"),
    ("move-pcb", "ki_move_pcb", "move a board on its sheet, or check that a move did no more"),
    ("png2fp", "ki_png2fp", "convert a PNG into a footprint of mask openings"),
    ("release", "ki_release", "build a board's release artifacts and publish a GitHub draft"),
    ("rename", "ki_rename", "rename a string in every KiCad file of the project"),
    ("install", None, "write the ki wrapper into a directory on PATH"),
)

SHELL = """#!/bin/bash
mapfile -t args < <(cygpath -w -- "$@" 2>/dev/null)
set -- "${args[@]}"
exec python "%s" "$@"
"""

BATCH = '@python "%s" %%*\n'

USAGE = """\
Usage: ki VERB [ARGUMENTS]
       ki VERB --help

Verbs
%s
Installing

  ki install DIRECTORY [--dry-run]

  Writes ki.cmd for cmd.exe, and for git and Fork, which launch it by name, and ki for
  cygwin and git-bash, both holding the absolute path of this script. Any directory on
  PATH will do. Re-run it after moving the scripts.

  Any python on PATH will do. A tool needing KiCad's own interpreter re-runs itself under it.
"""


def usage():
    width = max(len(verb) for verb, _module, _what in VERBS)
    lines = "".join("  %s  %s\n" % (verb.ljust(width), what) for verb, _module, what in VERBS)
    return USAGE % lines


def install(argv):
    """Write the two wrappers into the directory."""
    dry_run = "--dry-run" in argv or "-n" in argv
    rest = [arg for arg in argv if arg not in ("--dry-run", "-n")]
    if len(rest) != 1 or rest[0].startswith("-"):
        die("ki install takes the directory to write into, and --dry-run")
    directory = rest[0]
    if not os.path.isdir(directory):
        die("not a directory: %s" % shown(directory))

    here = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
    target = "%s/%s" % (here, os.path.basename(__file__))
    print("ki.cmd and ki in %s, running %s" % (shown(directory), shown(target)))
    if dry_run:
        print("Dry run, nothing written.")
        return 0

    with open(os.path.join(directory, "ki.cmd"), "w", encoding="utf-8",
            newline="\r\n") as handle:
        handle.write(BATCH % target)
    shell = os.path.join(directory, "ki")
    with open(shell, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(SHELL % target)
    os.chmod(shell, os.stat(shell).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    on_path = [entry.replace("\\", "/").rstrip("/").lower()
        for entry in os.environ.get("PATH", "").split(os.pathsep)]
    if os.path.abspath(directory).replace("\\", "/").rstrip("/").lower() not in on_path:
        print("Note: %s is not on PATH, so ki will not be found yet." % shown(directory))
    return 0


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(usage())
        return 0
    verb, rest = argv[0], argv[1:]
    modules = dict((name, module) for name, module, _what in VERBS)
    if verb not in modules:
        die("no verb %r; run ki --help for the list" % verb)
    if verb == "install":
        return install(rest)
    # The tool sees only its own arguments, as it would when run as a script, and names
    # itself "ki VERB" in its help and messages.
    sys.argv = ["ki " + verb] + rest
    return importlib.import_module(modules[verb]).main()


if __name__ == "__main__":
    exit_with(main)
