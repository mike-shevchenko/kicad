"""What the ki tools share: reporting errors, finding KiCad, and reading its files."""
# Written with the help of Claude Opus 5.

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys

RELAUNCH_FLAG = "KI_UNDER_KICAD_PYTHON"


class Failure(Exception):
    """A reported error. Raised rather than exiting, so it unwinds a worker thread too."""

    def __init__(self, message, code):
        Exception.__init__(self, message)
        self.code = code


def die(message, code=2):
    raise Failure(message, code)


def note(message):
    sys.stderr.write("[ki] " + message + "\n")


def exit_with(main):
    """Every tool's entry point: run main, and report a Failure the way die() promises."""
    try:
        sys.exit(main())
    except Failure as failure:
        sys.stderr.write("[ki] %s\n" % failure)
        sys.exit(failure.code)


def help_formatter(prog):
    """argparse help at the 99-column width the epilogs are written for."""
    return argparse.RawDescriptionHelpFormatter(prog, width=99)


def shown(path):
    """Display form. Windows accepts forward slashes, and they survive cygwin and git-bash
    without escaping, so printed paths can be pasted straight back into a shell."""
    return path.replace("\\", "/")


def version_key(text):
    """Sort "10.0" above "9.0", which a plain string sort gets backwards."""
    parts = [int(chunk) for chunk in re.split(r"[^0-9]+", text) if chunk]
    return parts or [0]


def kicad_binaries(name):
    """Every Program Files\\KiCad\\<version>\\bin\\<name> installed, newest version last."""
    found = []
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        root = os.path.join(base, "KiCad")
        if not os.path.isdir(root):
            continue
        for entry in os.listdir(root):
            candidate = os.path.join(root, entry, "bin", name)
            if os.path.isfile(candidate):
                found.append((version_key(entry), candidate))
    return [candidate for _version, candidate in sorted(found)]


def find_kicad_python():
    """KiCad's bundled interpreter is the only one carrying pcbnew. Prefer the newest."""
    override = os.environ.get("KICAD_PYTHON")
    if override:
        return override if os.path.isfile(override) else None
    found = kicad_binaries("python.exe")
    return found[-1] if found else None


def find_kicad_cli():
    """The CLI ships with KiCad and is usually not on PATH. Prefer the newest install."""
    override = os.environ.get("KICAD_CLI")
    if override:
        return override
    on_path = shutil.which("kicad-cli")
    if on_path:
        return on_path
    found = kicad_binaries("kicad-cli.exe")
    return found[-1] if found else None


def ensure_kicad_python(script):
    """Re-run the script under KiCad's Python when pcbnew is missing, so any python on PATH
    will do. The arguments go along unchanged, which is right whether the script was started
    directly or through the ki launcher, since that leaves the script its own arguments."""
    try:
        import pcbnew  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get(RELAUNCH_FLAG):
        die("pcbnew is still not importable under %s, which is KiCad's own Python. Is the "
            "KiCad installation complete?" % sys.executable)
    target = find_kicad_python()
    if not target:
        override = os.environ.get("KICAD_PYTHON")
        if override:
            die("KICAD_PYTHON is set to %s, which is not a file." % override)
        die("no KiCad Python found under Program Files\\KiCad\\*\\bin\\python.exe. "
            "Set KICAD_PYTHON to point at it.")
    # subprocess rather than os.execv: on Windows exec detaches, which would hand the caller
    # a premature exit code and unordered output.
    sys.exit(subprocess.call([target, os.path.realpath(script)] + sys.argv[1:],
        env=dict(os.environ, **{RELAUNCH_FLAG: "1"})))


def default_board():
    """The one .kicad_pcb in the current directory, for a tool given no board to act on."""
    hits = sorted(glob.glob("*.kicad_pcb"))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        die("no .kicad_pcb here; name one, or run inside the project directory")
    die("several .kicad_pcb here (%s); name the one you mean" % ", ".join(hits))


def form_end(text, start):
    """Index just past the closing paren of the s-expression form starting at `start`.

    Parentheses inside quoted strings are ignored, so a text item containing one does not
    throw the count off.
    """
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
    die("unbalanced parentheses in the form starting at line %d"
        % (text.count("\n", 0, start) + 1))
