"""What every ki tool shares: reporting errors, running things, and finding KiCad."""
# Written with the help of Claude Opus 5.

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

RELAUNCH_FLAG = "KI_UNDER_KICAD_PYTHON"

# Work that is subprocesses and C code releasing the GIL fills the cores from threads alone.
# kicad-cli runs are held to fewer lanes than that, since each loads the board and, for the
# 3D renders and the STEP model, its 3D models as well.
WORKERS = os.cpu_count() or 4
KICAD_LANES = threading.Semaphore(8)

# One line of a board's own (layers ...) block: the id, the name the file stores, the kind,
# and optionally a second name. KiCad renamed several layers and keeps the old name as the
# stored one, so "F.SilkS" arrives carrying "F.Silkscreen" alongside it.
LAYER_LINE = re.compile(r'\(\s*\d+\s+"([^"]+)"\s+\w+(?:\s+"([^"]+)")?\s*\)')


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


class TextParser(argparse.ArgumentParser):
    """A parser for a tool taking free text, which may start with a dash, as `-12V` does.
    argparse takes such text for an option, and its complaint then names some other argument
    as missing, so the error says what happened and how to pass the text."""

    def error(self, message):
        args = sys.argv[1:]
        if "--" in args:
            args = args[:args.index("--")]
        strays = [arg for arg in args if arg.startswith("-") and len(arg) > 1
            and arg.split("=")[0] not in self._option_string_actions]
        if strays:
            message += ("\n%r starts with a dash, so it was taken for an option. To pass it as"
                " text, put `--` in front of it, after any options." % strays[0])
        argparse.ArgumentParser.error(self, message)


def shown(path):
    """Display form. Windows accepts forward slashes, and they survive cygwin and git-bash
    without escaping, so printed paths can be pasted straight back into a shell."""
    return path.replace("\\", "/")


def version_key(text):
    """Sort "10.0" above "9.0", which a plain string sort gets backwards."""
    parts = [int(chunk) for chunk in re.split(r"[^0-9]+", text) if chunk]
    return parts or [0]


def parallel(jobs):
    """Run the jobs on worker threads, and return their results in the same order.

    A failed job is raised here once the others have finished: a kicad-cli process cannot
    be stopped once started, and a job cut short would leave half-written files behind.
    """
    jobs = list(jobs)
    if len(jobs) <= 1:
        return [job() for job in jobs]
    with ThreadPoolExecutor(max_workers=min(len(jobs), WORKERS)) as pool:
        futures = [pool.submit(job) for job in jobs]
        return [future.result() for future in futures]


def run(command, quiet=True):
    """Run a command, and fail loudly with its own output when it does."""
    with KICAD_LANES:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True)
    if result.returncode:
        sys.stderr.write(result.stdout or "")
        die("%s failed with status %d" % (os.path.basename(command[0]), result.returncode))
    if not quiet:
        sys.stdout.write(result.stdout or "")
    return result.stdout or ""


def open_path(path):
    """Show a file or directory the way a double-click would."""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.call(["xdg-open" if sys.platform != "darwin" else "open", path])


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


KICAD_CLI = None


def kicad_cli():
    """kicad-cli's path, looked up once, for every command line that starts with it."""
    global KICAD_CLI
    if KICAD_CLI is None:
        KICAD_CLI = find_kicad_cli()
        if not KICAD_CLI:
            die("no kicad-cli found; set KICAD_CLI to it")
    return KICAD_CLI


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


def board_layers(pcb):
    """Every layer the board enables, in the order it writes them, as (stored, shown).

    That order is KiCad's own, so a document follows the Board Setup list without any tool
    having to hold an opinion about which layers exist or how they rank.
    """
    text = open(pcb, encoding="utf-8", errors="replace").read()
    block = re.search(r"\n\t\(layers\n(.*?)\n\t\)\n", text, re.S)
    if not block:
        die("no (layers ...) block in %s" % shown(pcb))
    found = []
    for line in LAYER_LINE.finditer(block.group(1)):
        stored, shown_as = line.group(1), line.group(2)
        found.append((stored, shown_as or stored))
    if not found:
        die("the (layers ...) block of %s lists nothing" % shown(pcb))
    return found


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
