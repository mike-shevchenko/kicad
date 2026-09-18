#!/usr/bin/env python3
"""Rename a string in every KiCad file of the current project. See --help."""
# Written with the help of Claude Opus 5.

import argparse
import ast
import os
import re
import shutil
import sys
from collections import Counter

DESCRIPTION = "Rename a string in every KiCad file of the current directory."

EPILOG = """\
What is searched

Every .kicad_sch, .kicad_pcb, .kicad_mod, .kicad_pro, .kicad_prl and .kicad_dru file in
the current directory. Only the contents of quoted strings are considered, which in these
formats is where every name lives: references, net names, labels, text on Silkscreen, Fab
and User layers, field names and their values, variable references such as ${Function},
title-block comments and project settings. Renaming a field renames the places that read
it, so a field and its ${...} references stay together.

What counts as a match

An occurrence counts only when neither neighbouring character is a letter or a digit, in
any language. An underscore does not separate: DE2 is found in X_DE2, DE2_Y and X_DE2_Y,
but not in DE22 or CLAUDE2, and D1 is not found in LED1.

A string equal to SAMPLE is an exact match, and so is a net written with a sheet path in
front of it: /DE2 and /sheet/DE2 are exact matches for DE2. Every other string that
contains SAMPLE is listed as a containing string, numbered for exclusion. Both kinds are
rewritten, so that list is the thing to read before applying the change.

If REPLACEMENT is already in use anywhere, the run is blocked: renaming would merge the
two names. Exclude each such string to say that this is intended.

Excluding

An exclusion names a whole string as printed, never a part of one.

  -e N ...            by number, from either list, with or without the printed full stop;
                      the easiest way, and it needs no quoting
  -e STRING ...       by value; a Python literal is accepted too, so a string printed as
                      '-12|DE2' can be passed as "'-12|DE2'" when the shell allows it
  -E FILE             one string per line, taken verbatim; use this for strings that are
                      awkward to quote, such as those with quotes or non-ASCII inside

Strings are printed the way Python writes them, which no shell accepts as it stands, so
a number or -E FILE is the reliable route.

Other options

  -v                  log every occurrence, with its file, line and why it was taken or
                      passed over
  -f                  write the changes; each file is copied to .BAK alongside first

Examples

  kicad_rename.py J9 J1
  kicad_rename.py J9 J1 -v
  kicad_rename.py J9 J1 -e 3 4 7
  kicad_rename.py J9 J1 -e 3 4 7 -f
"""

SUFFIXES = (".kicad_sch", ".kicad_pcb", ".kicad_mod", ".kicad_pro", ".kicad_prl",
            ".kicad_dru")

STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
LETTER_OR_DIGIT = re.compile(r"[^\W_]", re.UNICODE)


def kind_of(char):
    return "digit" if char.isdigit() else "letter"


def project_files():
    return [n for n in sorted(os.listdir("."))
            if n.endswith(SUFFIXES) and not n.endswith(".BAK")]


def occurrences(value, sample):
    """(offsets that count, reason the others did not)."""
    good, reason, start = [], None, 0
    while True:
        i = value.find(sample, start)
        if i < 0:
            return good, reason
        before = value[i - 1] if i else ""
        after = value[i + len(sample):i + len(sample) + 1]
        if before and LETTER_OR_DIGIT.match(before):
            reason = reason or f"preceded by {kind_of(before)} {before!r}"
        elif after and LETTER_OR_DIGIT.match(after):
            reason = reason or f"followed by {kind_of(after)} {after!r}"
        else:
            good.append(i)
        start = i + 1


def is_exact(value, sample):
    return value == sample or (value.startswith("/") and value.split("/")[-1] == sample)


def survey(files, sample, verbose=False, excluded=()):
    """({exact value: Counter(file)}, {containing value: Counter(file)})."""
    equal, contains = {}, {}
    for path in files:
        text = open(path, encoding="utf-8", errors="replace").read()
        for m in STRING.finditer(text):
            value = m.group(1)
            if sample not in value:
                continue
            where = f"  {path}:{text.count(chr(10), 0, m.start()) + 1}:"
            if is_exact(value, sample):
                equal.setdefault(value, Counter())[path] += 1
                if verbose:
                    note = " excluded by -e" if value in excluded else ""
                    print(f"{where} exact match {value!r}{note}")
                continue
            good, reason = occurrences(value, sample)
            if good:
                contains.setdefault(value, Counter())[path] += 1
                if verbose:
                    note = " excluded by -e" if value in excluded else ""
                    n = len(good)
                    print(f"{where} {value!r} contains {sample!r} at {n} "
                          f"place{'' if n == 1 else 's'}{note}")
            elif verbose:
                print(f"{where} not a match: {sample!r} in {value!r} is {reason}")
    return equal, contains


def total_of(counters):
    return sum(sum(c.values()) for c in counters.values())


def report(sample, equal, contains, excluded=(), first=1, number_exact=False,
           clash=False):
    """Print one survey; returns its values in the order they were numbered."""
    if not equal and not contains:
        if clash:
            print(f"\n{sample!r} is not used anywhere - no clash.")
        return []

    print(f"\n{sample!r} is already in use:" if clash else f"\n{sample!r}:")
    numbered = []

    def line(value, counter, i=None):
        mark = " (excluded)" if value in excluded else ""
        tag = f"{i}. " if i else ""
        n = sum(counter.values())
        print(f"    {tag}{value!r} at {n} place{'' if n == 1 else 's'}.{mark}")

    if number_exact and len(equal) == 1:        # the one string is the name itself
        numbered.append(next(iter(equal)))
        print(f"  {first}. Equal strings: {total_of(equal)}.")
    elif number_exact:
        print(f"  Equal strings: {total_of(equal)}.")
        for value in sorted(equal):
            numbered.append(value)
            line(value, equal[value], first + len(numbered) - 1)
    else:
        print(f"  Equal strings: {total_of(equal)}.")
    print(f"  Containing strings: {total_of(contains)}.")
    for value in sorted(contains):
        numbered.append(value)
        line(value, contains[value], first + len(numbered) - 1)
    return numbered


def resolve_exclusions(items, path, order):
    out = set()
    for item in items:
        if item.rstrip(".").isdigit():          # a number copied with its full stop
            n = int(item.rstrip("."))
            if not 1 <= n <= len(order):
                sys.exit(f"-e {n}: there is no string with that number")
            out.add(order[n - 1])
        elif item[:1] in "'\"":
            try:
                out.add(ast.literal_eval(item))
            except (ValueError, SyntaxError):
                out.add(item)
        else:
            out.add(item)
    if path:
        for line in open(path, encoding="utf-8").read().splitlines():
            if line.strip():
                out.add(line)
    return out


def main():
    ap = argparse.ArgumentParser(
        description=DESCRIPTION, epilog=EPILOG,
        formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(prog, width=99))
    ap.add_argument("sample", metavar="SAMPLE", help="the string to rename")
    ap.add_argument("replacement", metavar="REPLACEMENT", help="what to rename it to")
    ap.add_argument("-e", "--exclude", action="append", nargs="+", default=[],
                    metavar="N|STRING", help="leave these whole strings alone")
    ap.add_argument("-E", "--exclude-file", metavar="FILE",
                    help="a file of strings to leave alone, one per line")
    ap.add_argument("-v", action="store_true", dest="verbose",
                    help="log every occurrence and why it was taken or passed over")
    ap.add_argument("-f", action="store_true", dest="force",
                    help="write the changes instead of only reporting them")
    if len(sys.argv) == 1:
        ap.print_help()
        return
    args = ap.parse_args()
    wanted = [item for group in args.exclude for item in group]

    files = project_files()
    if not files:
        sys.exit("no KiCad files in the current directory")
    print("Files:")
    for path in files:
        print(f"  {path}")

    equal, contains = survey(files, args.sample)
    new_equal, new_contains = survey(files, args.replacement)
    order = sorted(contains) + sorted(new_equal) + sorted(new_contains)
    excluded = resolve_exclusions(wanted, args.exclude_file, order)

    if args.verbose:
        print(f"\nOccurrences of {args.sample!r}:")
        survey(files, args.sample, True, excluded)
        print(f"\nOccurrences of {args.replacement!r}:")
        survey(files, args.replacement, True, excluded)

    report(args.sample, equal, contains, excluded)
    clashes = report(args.replacement, new_equal, new_contains, excluded,
                     first=len(contains) + 1, number_exact=True, clash=True)

    changing = total_of(equal) + sum(sum(c.values()) for value, c in contains.items()
                                     if value not in excluded)
    if excluded:
        print("\nExcluded:")
        for value in sorted(excluded):
            print(f"  {value!r}")

    blocking = [v for v in clashes if v not in excluded]
    if blocking:
        print(f"\nRenaming would merge {args.sample!r} into {len(blocking)} string(s) "
              f"already using {args.replacement!r}.")
        print("  Exclude each of them with -e, by number or by value, to say that this "
              "is intended.")
        if args.force:
            sys.exit("refusing to write")
    if not args.force:
        print(f"\n{changing} string(s) would change. "
              f"Nothing written, repeat with -f to apply.")
        return
    if not changing:
        print("\nNothing to change.")
        return

    def replace(value):
        if value in excluded:
            return value
        if is_exact(value, args.sample):
            return value[:-len(args.sample)] + args.replacement
        good, _reason = occurrences(value, args.sample)
        out, last = [], 0
        for i in good:
            out.append(value[last:i] + args.replacement)
            last = i + len(args.sample)
        out.append(value[last:])
        return "".join(out)

    for path in files:
        text = open(path, encoding="utf-8", errors="replace").read()
        new = STRING.sub(lambda m: '"' + replace(m.group(1)) + '"', text)
        if new == text:
            continue
        shutil.copyfile(path, path + ".BAK")
        open(path, "w", encoding="utf-8", newline="\n").write(new)
        print(f"Written: {path}.")


if __name__ == "__main__":
    main()
