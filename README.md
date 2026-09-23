# kicad

Helper scripts and useful stuff for KiCad.

## Installation

Every tool runs through one launcher, as `ki VERB [ARGUMENTS]`; `ki --help` lists the verbs.
Install its wrappers into any directory already on PATH, so `ki` works from any of cmd.exe,
cygwin and git-bash:

  python ki.py install C:/programs

That writes `ki.cmd` and `ki`, containing the absolute path of `ki.py`, so re-run it after
moving the scripts.

The tools can also be run directly, as `python ki_VERB.py ...` - the launcher is optional.

NOTE: Some tools need some dependencies - they report if any of them is missing.
