"""What the ki tools that draw share: plotting a board's layers, and making pages of them."""
# Written with the help of Claude Opus 5.

import hashlib
import os
import re
import shutil
import textwrap
import threading
import zlib
from functools import partial

from ki_common_lib import board_layers, die, form_end, kicad_cli, parallel, run, shown

# The imaging is pure Python. pypdfium2 ships its renderer inside the wheel, so nothing has
# to be installed outside pip. scipy is optional and only makes one step quicker. A tool
# imports this module for its --help too, so a missing library is reported, not raised.
try:
    import numpy
    import pypdfium2
    from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps
except ImportError as problem:
    IMAGING_PROBLEM = problem
    numpy = pypdfium2 = Image = ImageChops = ImageDraw = ImageFont = ImageOps = None
else:
    IMAGING_PROBLEM = None

# Every image is at 400 dpi of the board itself, close to a phone's screen, so that small
# silkscreen reads there as it will on the board. The plot is scaled to fill the sheet, since
# kicad-cli centers a plot only when its scale is not 1, and a board off the frame or larger
# than the sheet would otherwise be cut off; rasterizing at 400 over the scale undoes it.
DPI = 400
PROBE_DPI = 100  # density of the throwaway plot that measures the board on its sheet
PAGE_FILL = 0.9  # fraction of the sheet the scaled board is fitted into
CROP_MARGIN = 0.02  # border kept around the outline, as a fraction of its width

# A page: the board body dark so that pale artwork reads on it, the page around and through
# the drill holes left white, and a caption at the top.
SUBSTRATE = "#24402E"
PAGE = "white"
LABEL_COLOR = "#303030"
LABEL_DIVISOR = 24  # the page width over this gives the label point size, and its margin
PLACEHOLDER_COLOR = "#A0A0A0"

CUT_LAYER = "Edge.Cuts"

# The Fab layers are drawings rather than artwork, so their pages carry no substrate, only
# the board outline for context. KiCad's own colors are chosen for a dark canvas and vanish
# on a white one, so these two pages are drawn in ink instead.
OUTLINE_ONLY = ("F.Fab", "B.Fab")
INK = "#303030"
OUTLINE_INK = "#909090"
COPPER = ("F.Cu", "B.Cu")

# PDFium is not thread-safe, so plots are rasterized one at a time. At 12 ms a plot that is
# a small part of the work, and everything around it still runs in parallel; a pool of
# worker interpreters was tried and saved nothing, the pipe eating what the rendering gave.
PDFIUM_LOCK = threading.Lock()

INK_TRACE = 32  # the antialiased coverage, out of 255, above which a pixel counts as drawn

# What decides how big a board plots on its sheet: the paper, and every top-level form that
# draws on Edge.Cuts, footprints included since their edge lines move with them.
PAPER = re.compile(r"\n\t\(paper [^)]*\)")
OUTLINE_FORM = re.compile(r"\n\t\((?:gr_\w+|footprint|dimension)\b")


def require_imaging():
    """Stop with the install hint when the libraries are missing, before any work begins."""
    if IMAGING_PROBLEM:
        die("%s\n          Install the imaging libraries with:\n"
            "          python -m pip install --user pypdfium2 pillow numpy" % IMAGING_PROBLEM)


def default_drill(name):
    """Copper plots carry the actual drill shapes so the holes read; the rest stay clean."""
    return 2 if name in COPPER else 0


def counterpart(name):
    """The same layer on the other side of the board, or None for one that has no side."""
    if name.startswith("F."):
        return "B." + name[2:]
    if name.startswith("B."):
        return "F." + name[2:]
    return None


def sided_order(names):
    """The names with their front/back pairs first and the single-sided ones after.

    Pages of a pair face each other in a two-page view only while nothing single-sided
    comes between them, so Edge.Cuts, Margin and the User layers collect at the back. A
    layer counts as sided when its other half is present too, which needs no list of
    names - the F. and B. prefixes say it.
    """
    present = set(names)
    return ([name for name in names if counterpart(name) in present]
        + [name for name in names if counterpart(name) not in present])


def sibling_project(pcb):
    """The project file beside a board, or None."""
    project = os.path.splitext(pcb)[0] + ".kicad_pro"
    return project if os.path.isfile(project) else None


def stage_board(pcb, directory, project):
    """A copy of the board to plot from, with the project file beside it but nothing else.

    kicad-cli reads the .kicad_prl next to a board, and a scaled plot is centered on what
    the editor last showed: with a layer preset active that hides layers, the drawing lands
    off the sheet and every plot comes out blank. The project file is copied for its text
    variables and design settings. A name without the extension, as git and Fork hand over,
    gets one, since kicad-cli decides what a file is by it.
    """
    if not os.path.isfile(pcb):
        die("not a file: %s" % shown(pcb))
    os.makedirs(directory, exist_ok=True)
    name = os.path.basename(pcb)
    if not name.lower().endswith(".kicad_pcb"):
        name += ".kicad_pcb"
    target = os.path.join(directory, name)
    shutil.copyfile(pcb, target)
    if project:
        shutil.copyfile(project, os.path.splitext(target)[0] + ".kicad_pro")
    return target


class Plotter:
    """Layer plots of one board at one scale, black on white, in as few runs as possible.

    A kicad-cli run costs about half a second to load the board and a few milliseconds
    per layer, so all plots sharing a mirror and drill setting come out of one run. A
    plot is requested as (name, mirror, drill), with the name as the board shows it.

    The board is plotted from a copy in the work directory, with the project file named
    by project, or by default the one beside the board.
    """

    def __init__(self, pcb, work, scale, antialias=True, project="beside"):
        if project == "beside":
            project = sibling_project(pcb)
        self.pcb = stage_board(pcb, os.path.join(work, "board"), project)
        self.work, self.scale, self.antialias = work, scale, antialias
        self.density = DPI / scale
        self.layers = board_layers(self.pcb)
        self.names = [name for _stored, name in self.layers]
        self.stored = dict((name, stored) for stored, name in self.layers)
        self.stem = os.path.splitext(os.path.basename(self.pcb))[0]
        self.made = {}
        self.rasters = {}
        self.lock = threading.Lock()
        self.plotting = threading.Lock()

    def plot(self, wanted):
        """Make every requested plot not made yet, the runs side by side.

        Locked, so that a thread asking for a plot while another's runs are under way
        waits for them rather than plotting the same layers into the same files.
        """
        with self.plotting:
            groups = {}
            for name, mirror, drill in wanted:
                if (name, mirror, drill) not in self.made:
                    groups.setdefault((mirror, drill), []).append(name)
            for made in parallel(partial(self.batch, names, mirror, drill)
                    for (mirror, drill), names in sorted(groups.items())):
                self.made.update(made)

    def one(self, name, mirror, drill):
        """The PDF of one plot, made now if it was not requested before."""
        self.plot([(name, mirror, drill)])
        return self.made[name, mirror, drill]

    def raster(self, name, mirror, drill):
        """The plot as an alpha mask at DPI of the board, rasterized the first time it is
        asked for.

        Locked, unlike plot(): rendering is serialized anyway, so holding the lock while
        one raster is made costs nothing and lets any thread ask for any raster.
        """
        key = (name, mirror, drill)
        with self.lock:
            if key not in self.rasters:
                self.rasters[key] = artwork(self.one(name, mirror, drill), self.density,
                    self.antialias)
            return self.rasters[key]

    def batch(self, names, mirror, drill):
        for name in names:
            if name not in self.stored:
                die("%s is not a layer of %s" % (name, shown(self.pcb)))
        directory = os.path.join(self.work,
            "plots-%s-%d" % ("back" if mirror else "front", drill))
        os.makedirs(directory, exist_ok=True)
        # Without the popups, a plot is the same bytes whenever its layer is the same
        # drawing, the timestamp aside; with them, every footprint that moved anywhere on
        # the board leaves its mark in every layer's PDF.
        command = [kicad_cli(), "pcb", "export", "pdf", "--mode-separate",
            "--layers", ",".join(self.stored[name] for name in names),
            "--black-and-white", "--no-property-popups", "--scale", "%.4f" % self.scale,
            "--drill-shape-opt", str(drill)]
        if mirror:
            command.append("--mirror")
        run(command + ["-o", directory, self.pcb])
        made = {}
        for name in names:
            pdf = os.path.join(directory, "%s-%s.pdf" % (self.stem, name.replace(".", "_")))
            if not os.path.exists(pdf):
                die("kicad-cli did not write %s" % shown(pdf))
            made[name, mirror, drill] = pdf
        return made


def render_plot(pdf, density, antialias=True):
    """One plot as a grayscale page: black artwork on white paper.

    Rendered as gray from the start: PDFium then writes one byte per pixel instead of
    four, and nothing has to be converted, which is nearly three times quicker. Without
    antialiasing every pixel is wholly ink or wholly paper.
    """
    with PDFIUM_LOCK:
        document = pypdfium2.PdfDocument(pdf)
        try:
            return document[0].render(scale=density / 72.0, grayscale=True,
                no_smoothtext=not antialias, no_smoothpath=not antialias,
                no_smoothimage=not antialias).to_pil()
        finally:
            document.close()


def artwork(pdf, density, antialias=True):
    """A plot as an alpha mask: opaque where the layer drew, clear on blank paper."""
    return ImageOps.invert(render_plot(pdf, density, antialias))


def solid(mask, color):
    """One flat color wearing that mask as its alpha."""
    layer = Image.new("RGBA", mask.size, color)
    layer.putalpha(mask)
    return layer


def sharp(mask):
    """A mask with the antialiased fringe pushed to one side or the other."""
    return mask.point(lambda value: 255 if value > INK_TRACE else 0)


def dimmed(mask, alpha):
    """The mask at a fraction of its opacity."""
    return mask.point(lambda value: int(value * alpha))


def tint(mask, color, alpha):
    """A raster as artwork of one color on transparency."""
    return solid(dimmed(mask, alpha) if alpha < 1.0 else mask, color)


def enclosed(mask):
    """Mask of what a plot's ink encloses, dropping the outside and the ink itself.

    Labelling the blank regions and discarding the one touching a corner is the same idea
    as flooding that corner, but Pillow's flood fill is a Python loop and costs seconds. Any
    trace of ink counts as a wall, as sharp() has it: at DPI of the board an outline may be
    under a pixel wide, covering no pixel even half, and a fill through it leaks out.
    """
    white = numpy.asarray(mask) <= INK_TRACE
    try:
        from scipy import ndimage
    except ImportError:
        flat = mask.point(lambda value: 255 if value <= INK_TRACE else 0)
        ImageDraw.floodfill(flat, (0, 0), 0)
        return flat
    labels, _count = ndimage.label(white)
    body = white & (labels != labels[0, 0])
    return Image.fromarray((body * 255).astype("uint8"), "L")


def outline_key(pcb):
    """A digest of what the board's scale on its sheet depends on, and of the kicad-cli build
    that plots it, so a remembered scale is reused only while both are the same."""
    text = open(pcb, encoding="utf-8", errors="replace").read()
    parts = [PAPER.search(text).group(0) if PAPER.search(text) else ""]
    for form in OUTLINE_FORM.finditer(text):
        start = form.start() + 1
        body = text[start:form_end(text, start)]
        if '"Edge.Cuts"' in body:
            parts.append(body)
    cli = os.stat(kicad_cli())
    parts.append("%d %d" % (cli.st_size, cli.st_mtime_ns))
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def board_scale(pcb, work, remember=None):
    """Measure the board on its sheet, and return the scale that fits it to the sheet.

    Working in pixels of a throwaway plot avoids parsing the sheet size and avoids pcbnew:
    only the ratio matters, so the probe density cancels out. The probe is a kicad-cli run,
    so a tool run again and again can name a directory to remember the answer in.
    """
    if remember:
        memo = os.path.join(remember, "scale-%s.txt" % outline_key(pcb))
        if os.path.isfile(memo):
            with open(memo) as handle:
                scale, aspect = handle.read().split()
            return float(scale), float(aspect)
    probe = Plotter(pcb, os.path.join(work, "probe"), 1.0).one(CUT_LAYER, False, 0)
    mask = sharp(artwork(probe, PROBE_DPI))
    page_w, page_h = mask.size
    box = mask.getbbox()
    if not box:
        die("the board outline is empty - is there anything on Edge.Cuts?")
    board_w, board_h = box[2] - box[0], box[3] - box[1]
    scale = min(page_w * PAGE_FILL / board_w, page_h * PAGE_FILL / board_h)
    aspect = float(board_w) / board_h
    if remember:
        with open(memo, "w") as handle:
            handle.write("%r %r\n" % (scale, aspect))
    return scale, aspect


def outline_box(plotter, mirror):
    """The board outline's box in the plotter's rasters."""
    box = sharp(plotter.raster(CUT_LAYER, mirror, default_drill(CUT_LAYER))).getbbox()
    if not box:
        die("the board outline plotted empty at scale %.4f, so the %s side cannot be cropped"
            % (plotter.scale, "back" if mirror else "front"))
    return box


def crop_margin(box):
    """The border kept around the outline, the same for every image and page."""
    return int(round((box[2] - box[0]) * CROP_MARGIN))


def panel_rect(box, size):
    """The crop around an outline: its box with the margin, within the page."""
    margin = crop_margin(box)
    return (max(0, box[0] - margin), max(0, box[1] - margin),
        min(size[0], box[2] + margin), min(size[1], box[3] + margin))


def body_mask(plotter, mirror):
    """Where the substrate is: inside the outline, with the drill holes punched out.

    An outline plot that also carries the drill shapes encloses exactly two kinds of
    region, the body and the holes, so the body falls out of it on its own. It is always
    rasterized with antialiasing: without it, at DPI of the board, the curves of a thin
    outline come out with gaps, and the fill leaks through them.
    """
    if plotter.antialias:
        return enclosed(plotter.raster(CUT_LAYER, mirror, 2))
    return enclosed(artwork(plotter.one(CUT_LAYER, mirror, 2), plotter.density))


def substrate(plotter, mirror):
    """The board body as an image: opaque where the substrate is, holes already punched."""
    return solid(body_mask(plotter, mirror), SUBSTRATE)


def label_font(size):
    """A real face: Pillow's built-in font is a fixed bitmap, far too small for a page."""
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def label_step(page):
    """The caption's point size, which is also the margin it and its neighbors keep."""
    return max(12, page.width // LABEL_DIVISOR)


def write_at(page, text, top, size, color):
    """One line, horizontally centered, with its top edge at the given height."""
    font = label_font(size)
    draw = ImageDraw.Draw(page)
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((page.width - box[2] + box[0]) // 2, top), text, font=font, fill=color)


def caption(page, text):
    """The page's title, centered along its top edge."""
    step = label_step(page)
    write_at(page, text, step, step, LABEL_COLOR)


def caption_band(width):
    """The height of the strip above a page's drawing that carries its caption."""
    return 3 * max(12, width // LABEL_DIVISOR)


def page_size(rect):
    """The size of a page whose drawing is the rect, its caption band included."""
    width = rect[2] - rect[0]
    return width, rect[3] - rect[1] + caption_band(width)


def captioned(drawing, title):
    """A page: the drawing under a band carrying the title. A page is the board and its
    margin, so that at DPI it prints at the board's own size, not the sheet's."""
    band = caption_band(drawing.width)
    page = Image.new("RGB", (drawing.width, drawing.height + band), PAGE)
    page.paste(drawing.convert("RGB"), (0, band))
    caption(page, title)
    return page


def placeholder_page(name, word, size, out_png=None):
    """A page carrying a layer's name and one word in its middle, in place of a drawing."""
    page = Image.new("RGB", size, PAGE)
    caption(page, name)
    step = label_step(page)
    write_at(page, word, (page.height - step) // 2, step, PLACEHOLDER_COLOR)
    if out_png:
        page.save(out_png)
    return page


def text_page(title, lines, size, out_png=None):
    """A page of text: the title as a caption, then the lines, wrapped to the page."""
    page = Image.new("RGB", size, PAGE)
    caption(page, title)
    step = label_step(page)
    font = label_font(step * 2 // 3)
    draw = ImageDraw.Draw(page)
    # A glyph of this face is about a third of the caption size wide, which sets the column.
    columns = max(20, (page.width - 2 * step) * 3 // step)
    top = 3 * step
    for line in lines:
        for piece in textwrap.wrap(line, columns) or [""]:
            draw.text((step, top), piece, font=font, fill=LABEL_COLOR)
            top += step
    if out_png:
        page.save(out_png)
    return page


def compressed_image(page, level):
    """An image, or the path of one, as its size and its raw RGB bytes deflated."""
    image = (page if not isinstance(page, str) else Image.open(page)).convert("RGB")
    width, height = image.size
    return width, height, zlib.compress(image.tobytes(), level)


def write_pdf(pages, out, density, level=9):
    """Assemble full-page images, given as images or as files, into a PDF, each one
    losslessly compressed.

    Deflate level 9 by default; 6 gives a page a tenth larger in a third of the time.

    Pillow's own PDF writer cannot be used here: it encodes RGB as JPEG, whose ringing is
    plain to see along the sharp edges of a plot, and writes indexed images uncompressed.
    """
    body = bytearray(b"%PDF-1.4\n")
    offsets = []

    def add(chunk):
        offsets.append(len(body))
        body.extend(b"%d 0 obj\n" % len(offsets))
        body.extend(chunk)
        body.extend(b"\nendobj\n")

    # Object 1 is the catalog and object 2 the page tree. Each page then takes three more:
    # the page itself, its one-line content stream, and the image it draws.
    first = 3
    kids = " ".join("%d 0 R" % (first + n * 3) for n in range(len(pages)))
    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(("<< /Type /Pages /Count %d /Kids [%s] >>" % (len(pages), kids)).encode())
    for index, (width, height, data) in enumerate(parallel(
            partial(compressed_image, path, level) for path in pages)):
        across, down = width * 72.0 / density, height * 72.0 / density
        base = first + index * 3
        add(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f]"
            " /Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
            % (across, down, base + 2, base + 1)).encode())
        draw = ("q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (across, down)).encode()
        add(("<< /Length %d >>\nstream\n" % len(draw)).encode() + draw + b"\nendstream")
        add(("<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace"
            " /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n"
            % (width, height, len(data))).encode() + data + b"\nendstream")

    table = len(body)
    body.extend(("xref\n0 %d\n0000000000 65535 f \n" % (len(offsets) + 1)).encode())
    for offset in offsets:
        body.extend(("%010d 00000 n \n" % offset).encode())
    body.extend(("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(offsets) + 1, table)).encode())
    with open(out, "wb") as handle:
        handle.write(body)
