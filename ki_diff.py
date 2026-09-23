#!/usr/bin/env python3
"""Windows launcher for KiDiff: CLI passthrough, git diff driver, Fork diff tool. See --help."""
# Written with the help of Claude Opus 5.
# Run as "ki diff" through the ki launcher, or directly as "python ki_diff.py". git and Fork
# launch ki.cmd by name.

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile

from ki_lib import die, ensure_kicad_python, exit_with, help_formatter, note, shown

DESCRIPTION = "Run KiDiff on Windows: plain CLI, a git external-diff driver, or a Fork diff tool."

EPILOG = """\
Why this exists

KiDiff (https://github.com/INTI-CMNB/KiDiff) targets Linux. Three things break on Windows,
and this launcher works around all of them without modifying the installed KiDiff:

  * kicad-diff.py imports pcbnew, so it only runs under KiCad's bundled Python, not a
    system one. Start this script with any python; if pcbnew is missing it re-runs itself
    under the newest Program Files\\KiCad\\*\\bin\\python.exe, or $KICAD_PYTHON.
  * KiDiff writes layers.csv through csv.writer on a stream opened without newline='', so
    rows end \\r\\r\\n. Reading that back yields a blank row and an IndexError, meaning the
    second diff of any board crashes. Caches are normalized here before and after a run.
  * Without --output_dir KiDiff writes into a mkdtemp() that it deletes on exit, relying on
    xdg-open to have grabbed the file first. On Windows there is no xdg-open, so the diff is
    generated and discarded. Every mode here sets an explicit output directory.

Schematics cannot work from Windows at all: KiDiff shells out to KiAuto's eeschema_do,
which drives eeschema through xvfb and xdotool.

Modes

  ki diff <kicad-diff.py args>            Pass everything through to kicad-diff.py.
  ki diff --fork OLD NEW                  Diff two files and open the PDF. For Fork.
  ki diff --git-diff <7 git args>         git external-diff driver. Called by git.
  ki diff --fork --resolution N OLD NEW   The same, rendered at N DPI.
  ... --all-layers                        Either of those, with unchanged layers too.
  ki diff --selftest                      Check that KiDiff and its dependencies resolve.
  ki diff --clean [cache|pdfs|all]        List, or remove, what previous runs left behind.
  ki diff                                 Print this help.
  ki diff -- <args>                       Pass <args> through verbatim, flags and all.

Run with --help or no arguments for this text. Anything else is handed to kicad-diff.py, so
"ki diff -- --help" reaches KiDiff's own help and "ki diff -- --version" its version.

Setting up git

  echo *.kicad_pcb diff=kicad_diff >> .gitattributes
  git config --local diff.kicad_diff.command "C:/programs/ki.cmd diff --git-diff"

  git diff uses the driver; git show and git log -p need --ext-diff; --no-ext-diff disables
  it. PDFs land in <repo>/.git/kicad-git-cache/ next to the render cache, named
  <board>_<oldsha8>_to_<newsha8>.pdf, with 'worktree' for uncommitted changes.

  Only layers that actually changed become pages, so a re-save that moves no copper yields a
  single "no differences" sheet. Append --all-layers to the command above for every layer.

Cleaning up

  Everything lands in <repo>/.git/kicad-git-cache, so deleting or re-cloning the repository
  leaves nothing behind; only a diff of files outside any repository falls back to %TEMP%.
  Each area holds KiDiff's render cache as one subdirectory per file hash, and the diffs as
  .pdf files beside them. The cache is the bulk of it and is regenerated on demand.

  ki diff --clean          Report both areas with counts and sizes. Removes nothing.
  ki diff --clean cache    Drop the render caches, keep every PDF.
  ki diff --clean pdfs     Drop the PDFs, keep the caches so redraws stay fast.
  ki diff --clean all      Drop both, and the directories themselves.

  Run it inside a repository to include that repository's cache; outside one, only the temp
  area is touched. Nothing here ever deletes a file git is tracking: both areas live outside
  the working tree, under .git/ and %TEMP%.

Setting up Fork

  Preferences -> Integration -> Diff Tools -> add one:
      Title:        KiDiff (PCB)
      Path:         C:\\programs\\ki.cmd
      Arguments:    diff --fork $LOCAL $REMOTE

  Register one entry per resolution you want, each with its own title:

      Arguments:    diff --fork --resolution 400 $LOCAL $REMOTE

  --all-layers before $LOCAL keeps the layers that did not change. The resolution is part of
  the PDF name, so entries at different DPI do not overwrite each other, and they share one
  set of plots in the cache: changing DPI re-rasterizes but never re-plots.

  Fork lists every registered tool in its External Diff submenu, so pick this one on
  .kicad_pcb files. $LOCAL is the left/older side; swap the two if the colors come out
  reversed. The PDF opens in the default viewer and is written to the same place the git
  driver uses, so removing the repository removes it too.
"""

PCB_EXT = ".kicad_pcb"
SCH_EXT = ".kicad_sch"
CACHE_NAME = "kicad-git-cache"
MODES = ("--fork", "--git-diff", "--selftest", "--clean")


def find_kicad_diff():
    """Locate kicad-diff.py as installed by "pip install ki-diff"."""
    candidates = []
    for scheme in ("nt_user", None):
        try:
            base = sysconfig.get_path("scripts", scheme) if scheme else \
                sysconfig.get_path("scripts")
        except (KeyError, ValueError):
            continue
        if base:
            candidates.append(os.path.join(base, "kicad-diff.py"))
    found = shutil.which("kicad-diff.py")
    if found:
        candidates.append(found)
    for path in candidates:
        if os.path.isfile(path):
            return path
    die("kicad-diff.py not found. Install it with:\n"
        '           "%s" -m pip install --user ki-diff' % sys.executable)


def heal_layer_cache(cache_dir):
    """Rewrite layers.csv files whose rows end \\r\\r\\n (see --help)."""
    if not cache_dir or not os.path.isdir(cache_dir):
        return 0
    healed = 0
    for root, _dirs, files in os.walk(cache_dir):
        for name in files:
            if name != "layers.csv":
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
                if b"\r\r\n" in data:
                    with open(path, "wb") as handle:
                        handle.write(data.replace(b"\r\r\n", b"\r\n"))
                    healed += 1
            except OSError:
                pass
    return healed


def run_kicad_diff(args, cache_dir=None):
    """Invoke kicad-diff.py under this interpreter, healing the cache either side."""
    heal_layer_cache(cache_dir)
    command = [sys.executable, find_kicad_diff()] + list(args)
    code = subprocess.call(command)
    heal_layer_cache(cache_dir)
    return code


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]", "_", text) or "diff"


def wanted_ext(*names):
    for name in names:
        if name and name.lower().endswith(SCH_EXT):
            return SCH_EXT
    return PCB_EXT


def as_kicad_file(path, ext, workdir, label):
    """KiDiff decides PCB vs schematic from the extension, but git and Fork hand us
    temporary files that may not carry one. Copy to a correctly named file when needed."""
    if path.lower().endswith(ext):
        return path
    target = os.path.join(workdir, label + ext)
    shutil.copy2(path, target)
    return target


def reject_schematics(ext):
    if ext == SCH_EXT:
        die("schematic diffs need KiAuto (eeschema_do), which is Linux-only. PCBs only here.")


def git_dir():
    try:
        result = subprocess.run(["git", "rev-parse", "--absolute-git-dir"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True)
        if result.returncode == 0 and result.stdout.strip():
            # git answers with forward slashes; normalize so printed paths are not mixed.
            return os.path.normpath(result.stdout.strip())
    except OSError:
        pass
    fallback = os.path.join(os.getcwd(), ".git")
    return fallback if os.path.isdir(fallback) else None


def mode_git_diff(argv):
    """git external-diff driver: path old-file old-hex old-mode new-file new-hex new-mode."""
    parser = argparse.ArgumentParser(prog="ki diff --git-diff", add_help=False)
    parser.add_argument("--resolution", type=int, default=150)
    parser.add_argument("--all-layers", action="store_true")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    # Declared, not passed through: git appends its own arguments after these, so anything
    # the parser does not consume would shift the positionals it expects.
    options, rest = parser.parse_known_args(argv)
    if len(rest) < 7:
        die("git passes 7 arguments to a diff driver; got %d. Is this being run by hand?"
            % len(rest))
    name, old_file, old_hex, _old_mode, new_file, new_hex, _new_mode = rest[:7]

    for path in (old_file, new_file):
        if not os.path.isfile(path):
            die("not a file: %s" % path)

    ext = wanted_ext(name)
    reject_schematics(ext)

    # git uses an all-zero hash for the not-yet-committed working-tree file.
    is_worktree = not int(new_hex, 16)
    out_name = "%s_%s_to_%s.pdf" % (safe_name(os.path.basename(name)), old_hex[:8],
        "worktree" if is_worktree else new_hex[:8])

    cache = git_dir()
    if cache:
        cache = os.path.join(cache, CACHE_NAME)
        try:
            os.makedirs(cache, exist_ok=True)
        except OSError:
            note("cannot create %s; running without a cache" % cache)
            cache = None
    else:
        note("not inside a git repository; running without a cache")

    with tempfile.TemporaryDirectory(prefix="ki-diff-") as workdir:
        args = ["--resolution", str(options.resolution), "--old_file_hash", old_hex]
        if not options.all_layers:
            args.append("--only_different")
        if not is_worktree:
            args += ["--new_file_hash", new_hex]
        if options.verbose:
            args.append("-" + "v" * options.verbose)
        if cache:
            args += ["--cache_dir", cache, "--output_dir", cache, "--output_name", out_name]
        if os.path.isfile(".kicad-git-diff"):
            args += ["--exclude", ".kicad-git-diff"]
        args += [as_kicad_file(old_file, ext, workdir, "old"),
            as_kicad_file(new_file, ext, workdir, "new")]
        code = run_kicad_diff(args, cache)

    if code == 0 and cache:
        # A diff driver's stdout is shown in git's own output.
        print("[ki] PDF: " + shown(os.path.join(cache, out_name)))
    return code


def mode_fork(argv):
    """Fork external diff tool: options, two file paths, then open the resulting PDF."""
    all_layers, resolution = False, None
    while argv and argv[0].startswith("-"):
        if argv[0] == "--all-layers":
            all_layers, argv = True, argv[1:]
        elif argv[0] == "--resolution" and len(argv) > 1:
            resolution, argv = argv[1], argv[2:]
            if not resolution.isdigit():
                die("--resolution wants a number of DPI, got %r" % resolution)
        else:
            die("--fork takes --all-layers and --resolution N before the two file paths;"
                " got %r" % argv[0])
    if len(argv) < 2:
        die("--fork needs two file paths; got %d. In Fork, pass its two file placeholders."
            % len(argv))
    old_file, new_file = argv[0], argv[1]
    for path in (old_file, new_file):
        if not os.path.isfile(path):
            die("not a file: %s" % path)

    ext = wanted_ext(old_file, new_file)
    reject_schematics(ext)

    # Same directory as the git driver uses, so deleting or re-cloning the repository takes
    # every artefact with it. Only a diff of files outside any repository falls back to temp.
    repo = git_dir()
    out_dir = (os.path.join(repo, CACHE_NAME) if repo
        else os.path.join(tempfile.gettempdir(), "ki-diff"))
    os.makedirs(out_dir, exist_ok=True)

    key = hashlib.sha1((os.path.abspath(old_file) + "|" +
        os.path.abspath(new_file)).encode("utf-8")).hexdigest()[:8]
    # The resolution is part of the name, so Fork entries at different DPI keep their
    # own PDFs instead of overwriting one another.
    tag = "" if resolution is None else "_r" + resolution
    out_name = "%s_%s%s.pdf" % (safe_name(os.path.basename(new_file))[:60], key, tag)

    # Diffing the same pair twice is normal here, and the viewer opened last time still holds
    # that PDF, so ImageMagick cannot overwrite it and the stale file would be shown as new.
    target = os.path.join(out_dir, out_name)
    if os.path.isfile(target):
        try:
            os.remove(target)
        except OSError:
            out_name = "%s_%d.pdf" % (out_name[:-4], os.getpid())

    with tempfile.TemporaryDirectory(prefix="ki-diff-") as workdir:
        args = ["--cache_dir", out_dir, "--output_dir", out_dir, "--output_name", out_name]
        if not all_layers:
            args.append("--only_different")
        if resolution is not None:
            args += ["--resolution", resolution]
        args += [as_kicad_file(old_file, ext, workdir, "old"),
            as_kicad_file(new_file, ext, workdir, "new")]
        code = run_kicad_diff(args, out_dir)

    pdf = os.path.join(out_dir, out_name)
    if code == 0 and os.path.isfile(pdf):
        note("PDF: " + shown(pdf))
        try:
            os.startfile(pdf)
        except OSError as error:
            note("could not open the viewer (%s); open it yourself" % error)
    return code


def mode_selftest():
    ok = True
    print("interpreter: %s" % sys.executable)
    try:
        import pcbnew
        print("pcbnew: %s" % pcbnew.GetBuildVersion())
    except ImportError:
        print("pcbnew: MISSING - run this under KiCad's Python")
        ok = False
    print("kicad-diff.py: %s" % find_kicad_diff())

    # KiDiff uses ImageMagick 7's "magick" when present and only falls back to the v6 name
    # "convert", which on Windows is easily shadowed: Embarcadero, GnuWin and others ship
    # an unrelated convert.exe. Report that rather than letting a bare which() imply a pass.
    for tool in ("magick", "compare", "identify"):
        found = shutil.which(tool)
        print("%s: %s" % (tool, found or "MISSING - install ImageMagick 7"))
        if not found:
            ok = False
    convert = shutil.which("convert")
    if convert and "imagemagick" not in convert.lower():
        print("convert: %s" % convert)
        print("  not ImageMagick - harmless while magick exists, fatal without")

    raster = shutil.which("pdftoppm") or shutil.which("gs")
    print("pdftoppm/gs: %s" % (raster or "MISSING - install poppler or Ghostscript"))
    if not raster:
        ok = False
    return 0 if ok else 1


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def plural(count, word):
    return "%d %s" % (count, word if count == 1 else word + "s")


def human(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%d %s" % (value, unit) if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%d B" % size


def clean_places():
    """The two directories this tool writes into."""
    places = []
    repo = git_dir()
    if repo:
        places.append(("repo", os.path.join(repo, CACHE_NAME)))
    else:
        note("not inside a git repository, so only the temp area is considered")
    places.append(("temp", os.path.join(tempfile.gettempdir(), "ki-diff")))
    return places


def mode_clean(argv):
    """List or remove render caches and generated PDFs.

    In both places the same rule holds: KiDiff's render cache is subdirectories (named by
    blob or file hash), and the diffs this tool produces are .pdf files beside them. So
    caches can be dropped without touching a diff you still want to read.
    """
    what = argv[0] if argv else "list"
    if what not in ("list", "cache", "pdfs", "all"):
        die('--clean takes "cache", "pdfs" or "all"; pass nothing to list. Got "%s".' % what)

    found = freed = gone_caches = gone_pdfs = 0
    failures = []
    for label, path in clean_places():
        if not os.path.isdir(path):
            print("%s  %s  (absent)" % (label, shown(path)))
            continue
        caches, pdfs = [], []
        for entry in sorted(os.listdir(path)):
            full = os.path.join(path, entry)
            if os.path.isdir(full):
                caches.append(full)
            elif entry.lower().endswith(".pdf"):
                pdfs.append(full)
        found += len(caches) + len(pdfs)
        print("%s  %s" % (label, shown(path)))
        print("  caches: %d (%s)" % (len(caches), human(sum(dir_size(c) for c in caches))))
        print("  diffs: %d (%s)" % (len(pdfs),
            human(sum(os.path.getsize(p) for p in pdfs))))
        if what == "list":
            continue
        doomed = [(c, True) for c in caches if what in ("cache", "all")] + \
            [(p, False) for p in pdfs if what in ("pdfs", "all")]
        for victim, is_cache in doomed:
            size = dir_size(victim) if is_cache else os.path.getsize(victim)
            try:
                shutil.rmtree(victim) if is_cache else os.remove(victim)
                freed += size
                if is_cache:
                    gone_caches += 1
                else:
                    gone_pdfs += 1
            except OSError as error:
                # Collected, not reported here: the report goes to stdout, so interleaving
                # stderr mid-loop scrambles the order as soon as output is redirected.
                failures.append((shown(victim), error.strerror or error))
        if what == "all":
            try:
                os.rmdir(path)
            except OSError:
                pass

    if what != "list":
        parts = [plural(n, word) for n, word in
            ((gone_caches, "cache"), (gone_pdfs, "diff")) if n]
        if parts:
            print("Removed %s, freeing %s." % (" and ".join(parts), human(freed)))
        else:
            print("Nothing removed.")
        if failures:
            sys.stdout.flush()
            sys.stderr.write("[ki] %s could not be removed:\n"
                % plural(len(failures), "item"))
            for victim, reason in failures:
                sys.stderr.write("    %s (%s)\n" % (victim, reason))
        return 1 if failures else 0
    elif found:
        print("To clean, run any of:")
        for target in ("cache", "pdfs", "all"):
            print("  ki diff --clean %s" % target)
    return 0


def mode_passthrough(argv):
    cache = None
    for index, arg in enumerate(argv):
        if arg == "--cache_dir" and index + 1 < len(argv):
            cache = argv[index + 1]
        elif arg.startswith("--cache_dir="):
            cache = arg.split("=", 1)[1]
    # --only_cache and the informational flags produce no diff, so the warning is just noise.
    quiet = {"--version", "-h", "--help", "--only_cache"}
    produces_diff = not any(a in quiet for a in argv)
    if produces_diff and not any(a == "--output_dir" or a.startswith("--output_dir=")
        for a in argv):
        note("no --output_dir: KiDiff writes the PDF into a temporary directory and deletes "
            "it on exit, so the diff will be lost.")
    return run_kicad_diff(argv, cache)


def print_help():
    argparse.ArgumentParser(
        prog="ki diff",
        usage="ki diff [--fork OLD NEW | --git-diff <git args> | --selftest | <KiDiff args>]",
        description=DESCRIPTION, epilog=EPILOG,
        formatter_class=help_formatter).print_help()


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print_help()
        return 0
    if argv[0] == "--clean":
        return mode_clean(argv[1:])  # no pcbnew needed, so no relaunch
    ensure_kicad_python(__file__)
    if argv[0] == "--git-diff":
        return mode_git_diff(argv[1:])
    if argv[0] == "--fork":
        return mode_fork(argv[1:])
    if argv[0] == "--selftest":
        return mode_selftest()
    if argv[0] == "--":
        return mode_passthrough(argv[1:])
    if not argv[0].startswith("-") and not os.path.isfile(argv[0]) and "--" + argv[0] in MODES:
        die("did you mean --%s? A bare word is passed to kicad-diff.py as a file name."
            % argv[0])
    return mode_passthrough(argv)


if __name__ == "__main__":
    exit_with(main)
