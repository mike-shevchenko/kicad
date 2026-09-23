"""What the ki tools that draw share: plotting a board's layers, and making pages of them."""
# Written with the help of Claude Opus 5.

import os
import threading
import zlib
from functools import partial

from ki_common_lib import board_layers, die, kicad_cli, parallel, run, shown

# The imaging is pure Python. pypdfium2 ships its renderer inside the wheel, so nothing has
# to be installed outside pip. scipy is optional and only makes one step quicker. A tool
# imports this module for its --help too, so a missing library is reported, not raised.
try:
    import numpy
    import pypdfium2
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError as problem:
    IMAGING_PROBLEM = problem
    numpy = pypdfium2 = Image = ImageDraw = ImageFont = ImageOps = None
else:
    IMAGING_PROBLEM = None

# Rasterization is fixed at 400 dpi, and the board is scaled to fill the drawing sheet, so
# the pixel size of a page follows from the sheet alone and not from how big the board is.
DPI = 400
PROBE_DPI = 100  # density of the throwaway plot that measures the board on its sheet
PAGE_FILL = 0.9  # fraction of the sheet the scaled board is fitted into

# A page: the board body dark so that pale artwork reads on it, the page around and through
# the drill holes left white, and a caption at the top.
SUBSTRATE = "#24402E"
PAGE = "white"
LABEL_COLOR = "#303030"
LABEL_DIVISOR = 24  # the page width over this gives the label point size, and its margin
PLACEHOLDER_COLOR = "#A0A0A0"

CUT_LAYER = "Edge.Cuts"
COPPER = ("F.Cu", "B.Cu")

# PDFium is not thread-safe, so plots are rasterized one at a time. That is a small part
# of the work, and everything around it still runs in parallel.
PDFIUM_LOCK = threading.Lock()


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


class Plotter:
    """Layer plots of one board at one scale, black on white, in as few runs as possible.

    A kicad-cli run costs about half a second to load the board and a few milliseconds
    per layer, so all plots sharing a mirror and drill setting come out of one run. A
    plot is requested as (name, mirror, drill), with the name as the board shows it.
    """

    def __init__(self, pcb, work, scale):
        self.pcb, self.work, self.scale = pcb, work, scale
        self.layers = board_layers(pcb)
        self.names = [name for _stored, name in self.layers]
        self.stored = dict((name, stored) for stored, name in self.layers)
        self.stem = os.path.splitext(os.path.basename(pcb))[0]
        self.made = {}

    def plot(self, wanted):
        """Make every requested plot not made yet, the runs side by side.

        Not locked: a tool requests everything it will need in one call before any thread
        asks for a plot, so later calls only read what is already made.
        """
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

    def batch(self, names, mirror, drill):
        for name in names:
            if name not in self.stored:
                die("%s is not a layer of %s" % (name, shown(self.pcb)))
        directory = os.path.join(self.work,
            "plots-%s-%d" % ("back" if mirror else "front", drill))
        os.makedirs(directory, exist_ok=True)
        command = [kicad_cli(), "pcb", "export", "pdf", "--mode-separate",
            "--layers", ",".join(self.stored[name] for name in names),
            "--black-and-white", "--scale", "%.4f" % self.scale,
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


def render_plot(pdf, density):
    """One plot as a grayscale page: black artwork on white paper."""
    with PDFIUM_LOCK:
        document = pypdfium2.PdfDocument(pdf)
        try:
            return document[0].render(scale=density / 72.0).to_pil().convert("L")
        finally:
            document.close()


def artwork(pdf, density):
    """A plot as an alpha mask: opaque where the layer drew, clear on blank paper."""
    return ImageOps.invert(render_plot(pdf, density))


def solid(mask, color):
    """One flat color wearing that mask as its alpha."""
    layer = Image.new("RGBA", mask.size, color)
    layer.putalpha(mask)
    return layer


def sharp(mask):
    """A mask with the antialiased fringe pushed to one side or the other."""
    return mask.point(lambda value: 255 if value > 32 else 0)


def dimmed(mask, alpha):
    """The mask at a fraction of its opacity."""
    return mask.point(lambda value: int(value * alpha))


def tint(pdf, color, alpha, density):
    """A plot rasterized into artwork of one color on transparency."""
    mask = artwork(pdf, density)
    if alpha < 1.0:
        mask = dimmed(mask, alpha)
    return solid(mask, color)


def enclosed(page):
    """Mask of what a page's ink encloses, dropping the outside and the ink itself.

    Labelling the white regions and discarding the one touching a corner is the same idea
    as flooding that corner, but Pillow's flood fill is a Python loop and costs seconds.
    """
    white = numpy.asarray(page) > 127
    try:
        from scipy import ndimage
    except ImportError:
        flat = page.point(lambda value: 255 if value > 127 else 0)
        ImageDraw.floodfill(flat, (0, 0), 0)
        return flat
    labels, _count = ndimage.label(white)
    body = white & (labels != labels[0, 0])
    return Image.fromarray((body * 255).astype("uint8"), "L")


def board_scale(pcb, work):
    """Measure the board on its sheet, and return the scale that fits it to the sheet.

    Working in pixels of a throwaway plot avoids parsing the sheet size and avoids pcbnew:
    only the ratio matters, so the probe density cancels out.
    """
    probe = Plotter(pcb, os.path.join(work, "probe"), 1.0).one(CUT_LAYER, False, 0)
    mask = sharp(artwork(probe, PROBE_DPI))
    page_w, page_h = mask.size
    box = mask.getbbox()
    if not box:
        die("the board outline is empty - is there anything on Edge.Cuts?")
    board_w, board_h = box[2] - box[0], box[3] - box[1]
    scale = min(page_w * PAGE_FILL / board_w, page_h * PAGE_FILL / board_h)
    return scale, float(board_w) / board_h


def body_mask(plotter, mirror):
    """Where the substrate is: inside the outline, with the drill holes punched out.

    An outline plot that also carries the drill shapes encloses exactly two kinds of
    region, the body and the holes, so the body falls out of it on its own.
    """
    return enclosed(render_plot(plotter.one(CUT_LAYER, mirror, 2), DPI))


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


def placeholder_page(name, word, size, out_png):
    """A page carrying a layer's name and one word in its middle, in place of a drawing."""
    page = Image.new("RGB", size, PAGE)
    caption(page, name)
    step = label_step(page)
    write_at(page, word, (page.height - step) // 2, step, PLACEHOLDER_COLOR)
    page.save(out_png)


def compressed_image(path):
    """An image file as its size and its raw RGB bytes deflated, the slow part of a page."""
    image = Image.open(path).convert("RGB")
    width, height = image.size
    return width, height, zlib.compress(image.tobytes(), 9)


def write_pdf(pages, out, density):
    """Assemble full-page images into a PDF, each one losslessly compressed.

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
            partial(compressed_image, path) for path in pages)):
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
