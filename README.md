# kicad

Helper scripts and useful stuff for KiCad.

## Installation

Install the wrappers into any directory already on PATH, so each script can be run by its
dashed name without extension from any of cmd.exe, cygwin, and git-bash:

  python ki_install.py C:/programs

That will create `ki-*` and `ki-*.cmd` wrappers for every `ki_*.py` script here, containing
absolute paths to the scripts, so, re-run the installer when moving the scripts.

You can also run the scripts directly as `python ki_*.py ...` - the wrappers are optional.

NOTE: Some scripts need some dependencies - they report if any of them is missing.
