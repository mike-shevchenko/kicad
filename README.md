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

## Comparing boards: `ki diff`

`ki diff OLD.kicad_pcb NEW.kicad_pcb` writes a PDF with one page per layer that differs, the
board filling the page: what only the old version had in red, what only the new one has in
cyan, the rest faint, and the drill holes compared the same way on every page. A summary page
at the end names the layers that changed. A diff of one small change takes a couple of
seconds, since a layer whose plots are identical is settled without rendering anything.

It also serves as git's diff driver for boards; the setup is two lines:

  echo *.kicad_pcb diff=kicad_diff >> .gitattributes
  git config --local diff.kicad_diff.command "C:/programs/ki.cmd diff --git-diff"

And as an external diff tool in Fork, under Preferences, Integration, Diff Tools:

  Title:      KiCad PCB
  Path:       C:\programs\ki.cmd
  Arguments:  diff $REMOTE $LOCAL

Fork hands the old version over as `$REMOTE`, hence the order. Run `ki diff --help` for the
rest: where the PDFs go, `--all-layers`, and `--clean`.
