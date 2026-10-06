"""Make variants of a workbook for tests by editing its XML directly.

Only the cells/sheet names asked for are touched; everything else (including the
cached results of formulas) is kept byte-for-byte, so a variant is still a
realistic, recalculated workbook. Nothing here opens or reads any sheet's content
other than the ones being patched.
"""
import io
import re
import zipfile
from xml.sax.saxutils import escape


def _sheet_parts(z):
    wbx = z.read("xl/workbook.xml").decode("utf-8")
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    target = {m.group(1): m.group(2) for m in re.finditer(r'<Relationship\b[^>]*?\bId="([^"]+)"[^>]*?\bTarget="([^"]+)"', rels)}
    target.update({m.group(2): m.group(1) for m in re.finditer(r'<Relationship\b[^>]*?\bTarget="([^"]+)"[^>]*?\bId="([^"]+)"', rels)})
    out = {}
    for m in re.finditer(r'<sheet\b[^>]*?\bname="([^"]+)"[^>]*?\br:id="([^"]+)"', wbx):
        t = target[m.group(2)].lstrip("/")
        out[m.group(1).replace("&amp;", "&")] = t if t.startswith("xl/") else "xl/" + t
    return out


def _patch_cell(xml, ref, value):
    pat = re.compile(rf'<c r="{ref}"([^>]*?)(?:/>|>(.*?)</c>)', re.S)
    m = pat.search(xml)
    if not m:
        raise KeyError(f"cell {ref} not present in the sheet XML")
    attrs = re.sub(r'\s+t="[^"]*"', "", m.group(1))
    inner = m.group(2) or ""
    formula = re.search(r"<f\b.*?(?:</f>|/>)", inner, re.S)
    f = formula.group(0) if formula else ""
    if isinstance(value, str):
        new = f'<c r="{ref}"{attrs} t="inlineStr"><is><t>{escape(value)}</t></is></c>'
    else:
        new = f'<c r="{ref}"{attrs}>{f}<v>{value!r}</v></c>'
    return xml[:m.start()] + new + xml[m.end():]


def patch(data, edits=None, renames=None):
    """edits = {(sheet_name, 'AC10'): number-or-text}; renames = {old_sheet_name: new_sheet_name}. Returns new bytes."""
    edits, renames = edits or {}, renames or {}
    zin = zipfile.ZipFile(io.BytesIO(data))
    parts = _sheet_parts(zin)
    by_part = {}
    for (sheet, ref), value in edits.items():
        by_part.setdefault(parts[sheet], []).append((ref, value))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            blob = zin.read(item.filename)
            if item.filename in by_part:
                xml = blob.decode("utf-8")
                for ref, value in by_part[item.filename]:
                    xml = _patch_cell(xml, ref, value)
                blob = xml.encode("utf-8")
            elif item.filename == "xl/workbook.xml" and renames:
                xml = blob.decode("utf-8")
                for old, new in renames.items():
                    old_x, new_x = escape(old), escape(new)
                    assert xml.count(f'name="{old_x}"') == 1, old
                    xml = xml.replace(f'name="{old_x}"', f'name="{new_x}"')
                blob = xml.encode("utf-8")
            zout.writestr(item, blob)
    return out.getvalue()
