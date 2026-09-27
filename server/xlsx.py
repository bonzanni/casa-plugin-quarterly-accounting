# server/xlsx.py
"""A minimal SpreadsheetML workbook, hand-written with zipfile (the server is
stdlib-only; spec §Packaging). Inline strings only, one sheet, a header row.
ledger.csv is authoritative: if the two ever disagree, the CSV is right."""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from xml.sax.saxutils import escape

FIXED_TIME = (1980, 1, 1, 0, 0, 0)
_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
# Characters XML 1.0 cannot carry at all: dropped from BOTH ledgers' comparison
# (package.xml_safe). U+FFFE/U+FFFF included — left in, they make the sheet
# unparseable.
_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")

CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/>'
                 '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                 '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                 '</Types>')
ROOT_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
             '</Relationships>')
WORKBOOK = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="ledger" sheetId="1" r:id="rId1"/></sheets></workbook>')
WORKBOOK_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                 '</Relationships>')


def xml_safe(value: str) -> str:
    return _BAD.sub("", value)


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet(rows) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           f'<worksheet xmlns="{_NS}"><sheetData>']
    for r, row in enumerate(rows, start=1):
        out.append(f'<row r="{r}">')
        for c, value in enumerate(row):
            # a literal CR would be normalized to LF by every XML parser, so the
            # cell would no longer equal ledger.csv: carry it as a character ref
            text = escape(xml_safe(str(value))).replace("\r", "&#13;")
            out.append(f'<c r="{_col(c)}{r}" t="inlineStr"><is><t xml:space="preserve">'
                       f'{text}</t></is></c>')
        out.append("</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def zip_files(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            data = files[name]
            z.writestr(info, data.encode("utf-8") if isinstance(data, str) else data,
                       compresslevel=6)
    return buf.getvalue()


def workbook(rows) -> bytes:
    return zip_files({"[Content_Types].xml": CONTENT_TYPES, "_rels/.rels": ROOT_RELS,
                      "xl/workbook.xml": WORKBOOK, "xl/_rels/workbook.xml.rels": WORKBOOK_RELS,
                      "xl/worksheets/sheet1.xml": _sheet(rows)})


def read_cells(data: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    ns = {"s": _NS}
    return [[(c.find("s:is/s:t", ns).text or "") for c in row.findall("s:c", ns)]
            for row in root.find("s:sheetData", ns).findall("s:row", ns)]
