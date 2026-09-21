#!/usr/bin/env python3
"""Create shell and cmd wrappers for the ki_*.py scripts sitting beside this one."""
# Written with the help of Claude Opus 5.

import argparse
import glob
import os
import stat
import sys

DESCRIPTION = "Install ki-* wrappers for every ki_*.py script in this directory."

SHELL = """#!/bin/bash
mapfile -t args < <(cygpath -w -- "$@" 2>/dev/null)
set -- "${args[@]}"
exec python "%s" "$@"
"""

BATCH = '@python "%s" %%*\n'

SAMPLE_SCRIPT = "<dir>/ki_<name>.py"


def sample(template):
    """One wrapper's contents, indented for the help, so a template is written out once."""
    return "".join("    " + line for line in (template % SAMPLE_SCRIPT).splitlines(True))


# The markers are filled from the templates above, so the help cannot drift from what is
# actually written. Plain .replace, because the wrappers contain % and {}, which would fight
# % formatting and str.format respectively.
EPILOG = """\
Usage

  python ki_install.py C:/programs

Any directory on PATH will do. Each ki_*.py beside this script gets a pair of wrappers named
after it: the .py is dropped and underscores become dashes.

  ki-<name>.cmd
<<BATCH>>

  ki-<name>
<<SHELL>>

Why two of them

  cmd.exe resolves a bare command name through PATHEXT, so it can only run the .cmd. cygwin
  and git-bash ignore PATHEXT and need a file named exactly as typed, so they get the
  extensionless one, which first rewrites absolute POSIX paths for the native Python.

  Anything launching a program through CreateProcess must name the .cmd explicitly, since an
  extensionless file cannot be executed that way at all, even given its full path. Of these
  scripts only ki-diff is launched like that, by Fork and by git's diff driver.

  Any python on PATH will do. A script needing KiCad's own interpreter re-runs itself under it.

Re-run this after moving the scripts, since the wrappers hold absolute paths.
""".replace("<<BATCH>>\n", sample(BATCH)).replace("<<SHELL>>\n", sample(SHELL))


def command_name(script):
    """The command a script is wrapped as: the .py dropped, underscores turned into dashes."""
    return script[:-len(".py")].replace("_", "-")


def main():
    parser = argparse.ArgumentParser(
        prog="ki_install.py", description=DESCRIPTION, epilog=EPILOG,
        formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(prog, width=99))
    parser.add_argument("directory", help="where to write the wrappers, e.g. C:/programs")
    parser.add_argument("-n", "--dry-run", action="store_true",
        help="report what would be written, and write nothing")
    args = parser.parse_args()

    if not os.path.isdir(args.directory):
        sys.exit("not a directory: %s" % args.directory)

    here = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
    myself = os.path.basename(__file__)
    scripts = [os.path.basename(p) for p in sorted(glob.glob(os.path.join(here, "ki_*.py")))
        if os.path.basename(p) != myself]
    if not scripts:
        sys.exit("no ki_*.py scripts beside %s" % myself)

    for script in scripts:
        command = command_name(script)
        target = "%s/%s" % (here, script)
        print("%s = %s, %s.cmd" % (script, command, command))
        if args.dry_run:
            continue
        batch = os.path.join(args.directory, command + ".cmd")
        shell = os.path.join(args.directory, command)
        with open(batch, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write(BATCH % target)
        with open(shell, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(SHELL % target)
        os.chmod(shell, os.stat(shell).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if args.dry_run:
        print("Dry run, nothing written.")
        return 0

    print("Installed %d wrapper pairs in %s." % (len(scripts), args.directory))
    on_path = [p.replace("\\", "/").rstrip("/").lower()
        for p in os.environ.get("PATH", "").split(os.pathsep)]
    if os.path.abspath(args.directory).replace("\\", "/").rstrip("/").lower() not in on_path:
        print("Note: %s is not on PATH, so the commands will not be found yet."
            % args.directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
