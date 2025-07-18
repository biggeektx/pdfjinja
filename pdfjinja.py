# -*- coding: utf-8 -*-
""" Use jinja2 templates to fill and sign PDF forms. """

import argparse
import datetime
import logging
import sys
import os
import time

from fdfgen import forge_fdf
from jinja2 import Environment, TemplateSyntaxError, Undefined
from pdfminer.pdfparser import PDFParser
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.pdfdevice import PDFDevice
from pdfminer.pdftypes import PDFObjRef
from pdfminer.layout import LAParams
from pdfminer.converter import PDFPageAggregator
from PIL import Image, ImageDraw, ImageFont
from PyPDF2 import PdfWriter, PdfReader
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from subprocess import Popen, PIPE
from io import BytesIO
from logging import NullHandler


logger = logging.getLogger(__name__)
logger.addHandler(NullHandler())


class Attachment(object):

    label_x = 8

    label_y = 8

    font = None

    fontsize = 12

    def __init__(self, data, dimensions=None, text=None, font=None):
        img = Image.open(data)
        self.img = img
        self.dimensions = dimensions or (0, 0, img.size[0], img.size[1])

        if img.mode == "RGBA":
            self.img = Image.new("RGB", img.size, (255, 255, 255))
            mask = img.split()[-1]  # 3 is the alpha channel
            self.img.paste(img, mask=mask)

        if font is not None:
            self.font = font

        if text is not None:
            font = ImageFont.truetype(self.font, self.fontsize)
            lines = text.split(os.linesep)
            dims = []
            for l in lines:
                # For TrueType fonts, getbbox returns (left, top, right, bottom)
                # For bitmap fonts (not used here for labels), getsize is (width, height)
                bbox = font.getbbox(l)
                line_width = bbox[2] - bbox[0]
                line_height = bbox[3] - bbox[1]
                dims.append((line_width, line_height))

            w = max(w for w, h in dims) if dims else 0
            h = sum(h for w, h in dims) if dims else 0

            self.label = Image.new("RGB", (int(w), int(h)), (255, 255, 255))
            draw = ImageDraw.Draw(self.label)

            y = 0
            for (lw, lh), line in zip(dims, lines):
                draw.text((0, y), line, (0, 0, 0), font=font)
                y += lh

    def pdf(self):
        stream = BytesIO()
        pdf = canvas.Canvas(stream)
        w, h = self.img.size
        pdf.drawImage(ImageReader(self.img), *self.dimensions)

        if hasattr(self, "label"):
            w, h = self.label.size
            x, y = self.label_x, self.label_y
            pdf.drawImage(ImageReader(self.label), x, y, w, h)

        pdf.save()
        return PdfReader(stream).pages[0]

class SilentUndefined(Undefined):
    def _fail_with_undefined_error(self, *args, **kwargs):
        return ""

class PdfJinja(object):

    Attachment = Attachment

    def __init__(self, filename, jinja_env=None):
        self.jinja_env = Environment(undefined=SilentUndefined)
        self.context = None
        self.fields = {}
        self.watermarks = []
        self.filename = filename
        self.register_filters()
        with open(filename, "rb") as fp:
            self.parse_pdf(fp)

    def register_filters(self):
        self.jinja_env.filters.update(dict(
            date=self.format_date,
            paste=lambda v: self.paste(v) if callable(getattr(v, 'read', None)) or isinstance(v, str) else "",
            check=lambda v: "Yes" if v else "Off",
            X=lambda v: "X" if v else " ",
            Y=lambda v: "Y" if v else "N",
        ))

    def paste(self, data):
        rect = self.context["rect"]
        x, y = rect[0], rect[1]
        w, h = rect[2] - x, rect[3] - y
        pdf = self.Attachment(data, dimensions=(x, y, w, h)).pdf()
        self.watermarks.append((self.context["page"], pdf))
        return " "

    def parse_pdf(self, fp):
        parser = PDFParser(fp)
        doc = PDFDocument(parser)
        rsrcmgr = PDFResourceManager()
        laparams = LAParams()
        device = PDFPageAggregator(rsrcmgr, laparams=laparams)
        interpreter = PDFPageInterpreter(rsrcmgr, device)

        device = PDFDevice(rsrcmgr)
        interpreter = PDFPageInterpreter(rsrcmgr, device)

        for pgnum, page in enumerate(PDFPage.create_pages(doc)):
            interpreter.process_page(page)
            page.annots and self.parse_annotations(pgnum, page)

    def parse_annotations(self, pgnum, page):
        annots = page.annots
        if isinstance(page.annots, PDFObjRef):
            annots = page.annots.resolve()

        annots = (
            r.resolve() for r in annots if isinstance(r, PDFObjRef))

        widgets = (
            r for r in annots if r["Subtype"].name == "Widget")

        for ref in widgets:
            data_holder = ref
            try:
                name = ref["T"]
            except KeyError:
                ref = ref['Parent'].resolve()
                name = ref['T']
            field = self.fields.setdefault(name, {"name": name, "page": pgnum})
            allowed_fts = ("Btn", "Tx", "Ch", "Sig")
            if "FT" in data_holder and data_holder["FT"].name in allowed_fts:
                field["rect"] = data_holder["Rect"]

            if "TU" in ref:
                tmpl = ref["TU"]

                try:
                    if ref["TU"].startswith(b"\xfe"):
                        tmpl = tmpl.decode("utf-16")
                    else:
                        tmpl = tmpl.decode("utf-8")
                    field["template"] = self.jinja_env.from_string(tmpl)
                except (UnicodeDecodeError, TemplateSyntaxError) as err:
                    logger.error("%s: %s %s", name, tmpl, err)

    def template_args(self, data):
        kwargs = {}
        today = datetime.datetime.today().strftime("%m/%d/%y")
        kwargs.update({"today": today})
        kwargs.update(data)
        return kwargs

    def format_date(self, datestr):
        if not datestr:
            return ""

        ts = time.strptime(datestr, "%Y-%m-%dT%H:%M:%S.%fZ")
        dt = datetime.datetime.fromtimestamp(time.mktime(ts))
        return dt.strftime("%m/%d/%y")

    def exec_pdftk(self, data):
        fdf_kwargs = dict(checkbox_checked_name="Yes")
        fdf = forge_fdf("", data.items(), [], [], [], **fdf_kwargs)
        args = [
            "/usr/bin/pdftk",
            self.filename,
            "fill_form", "-",
            "output", "-",
            "dont_ask",
            "flatten"
        ]

        p = Popen(args, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        stdout, stderr = p.communicate(fdf)
        if stderr.strip():
            raise IOError(stderr.decode('utf-8', errors='replace').strip())

        return BytesIO(stdout)

    def __call__(self, data, attachments=[], pages=None):
        self.rendered = {}
        for field, ctx in self.fields.items():
            if "template" not in ctx:
                continue

            self.context = ctx
            kwargs = self.template_args(data)
            template = self.context["template"]

            try:
                rendered_field = template.render(**kwargs)
            except Exception as err:
                logger.error("%s: %s %s", field, template, err)
            else:
                # Skip the field if it is already rendered by filter
                if field not in self.rendered:
                    self.rendered[field] = rendered_field

        filled = PdfReader(self.exec_pdftk(self.rendered))
        for pagenumber, watermark in self.watermarks:
            page = filled.pages[pagenumber]
            page.merge_page(watermark)

        output = PdfWriter()
        pages = pages or range(len(filled.pages))
        for p in pages:
            output.add_page(filled.pages[p])

        for attachment in attachments:
            output.add_blank_page().merge_page(attachment.pdf())

        return output


def parse_args(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "-f", "--font", type=str, default=None,
        help="TTF font for attachment labels.")

    parser.add_argument(
        "-j", "--json", type=argparse.FileType("rb"), default=sys.stdin,
        help="JSON format file with data.")

    parser.add_argument(
        "-p", "--page", type=str, default=None,
        help="Pages to select (Comma separated).")

    parser.add_argument(
        "pdf", type=str,
        help="PDF form with jinja2 tooltips.")

    parser.add_argument(
        "out", nargs="?", type=argparse.FileType("wb"), default=sys.stdout,
        help="PDF filled with the form data.")

    return parser.parse_args()


def main():
    logging.basicConfig()
    args = parse_args(__doc__)
    pdfparser = PdfJinja(args.pdf)
    pages = args.page and args.page.split(",")

    import json
    json_data = args.json.read().decode('utf-8')
    data = json.loads(json_data)
    Attachment.font = args.font
    attachments = [
        Attachment(**kwargs) for kwargs in data.pop("attachments", [])
    ]

    output = pdfparser(data, attachments, pages)
    output.write(args.out)


if __name__ == "__main__":
    main()
