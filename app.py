import base64
import io
import os
import re
import tempfile

import fitz
from flask import Flask, request, jsonify


app = Flask(__name__)


# -------------------------------------------------
# BASIC HELPERS
# -------------------------------------------------

def digits_only(value):
    return re.sub(r"\D", "", str(value or ""))


def clean_base64(value):
    value = str(value or "")

    if "," in value:
        value = value.split(",", 1)[1]

    return base64.b64decode(value)


def make_output_name(name):
    name = str(name or "output.pdf")

    if not name.lower().endswith(".pdf"):
        name += ".pdf"

    return re.sub(
        r"\.pdf$",
        "_replaced.pdf",
        name,
        flags=re.I
    )


# -------------------------------------------------
# UNICODE DIGIT NORMALIZATION
# -------------------------------------------------

UNICODE_DIGIT_MAP = {}

for block_start, ascii_start in [
    (0x0660, 0x30),
    (0x06F0, 0x30),
    (0x0966, 0x30),
    (0x09E6, 0x30),
    (0x0AE6, 0x30),
    (0x0BE6, 0x30),
    (0x0C66, 0x30),
    (0x0CE6, 0x30),
    (0x0D66, 0x30),
    (0x0DE6, 0x30),
]:
    for i in range(10):
        UNICODE_DIGIT_MAP[
            chr(block_start + i)
        ] = chr(ascii_start + i)


def normalize_digit(ch):
    if ch in UNICODE_DIGIT_MAP:
        return UNICODE_DIGIT_MAP[ch]

    if "0" <= ch <= "9":
        return ch

    return None


# -------------------------------------------------
# CHARACTER EXTRACTION
# -------------------------------------------------

def page_characters(page):
    data = page.get_text("rawdict")

    output = []

    for block in data.get("blocks", []):

        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):

            for span in line.get("spans", []):

                font = span.get(
                    "font",
                    ""
                )

                size = span.get(
                    "size",
                    10
                )

                color = span.get(
                    "color",
                    0
                )

                flags = span.get(
                    "flags",
                    0
                )

                for ch in span.get("chars", []):

                    char = ch.get("c", "")

                    if not char:
                        continue

                    bbox = ch.get("bbox")

                    origin = ch.get("origin")

                    if not bbox or not origin:
                        continue

                    digit =
                        normalize_digit(char)

                    output.append({
                        "char": char,
                        "digit": digit,
                        "bbox": tuple(bbox),
                        "origin": tuple(origin),
                        "font": font,
                        "size": size,
                        "color": color,
                        "flags": flags
                    })

    return output


# -------------------------------------------------
# NUMBER GROUP DETECTION
# -------------------------------------------------

def build_phone_candidates(chars):

    candidates = []

    current = []

    last = None

    for item in chars:

        ch = item["char"]

        d = item["digit"]

        if d:

            if (
                last is not None
                and item["bbox"][1] -
                    last["bbox"][1] > 5
            ):
                if current:
                    candidates.append(current)

                current = []

            current.append(item)

            last = item

        else:

            if not current:
                continue

            # punctuation / space between digits
            if (
                ch.isspace()
                or ch in "+-()[]{}./#@*_:"
            ):
                continue

            # anything else breaks number
            if current:
                candidates.append(current)

            current = []
            last = None

    if current:
        candidates.append(current)

    return candidates


def candidate_digits(candidate):

    return "".join(
        x["digit"]
        for x in candidate
        if x["digit"]
    )


def candidate_text(candidate):

    if not candidate:
        return ""

    return "".join(
        x["char"]
        for x in candidate
    )


# -------------------------------------------------
# BETTER LINE-BASED NUMBER GROUPING
# -------------------------------------------------

def detect_numbers(page):

    chars = page_characters(page)

    lines = {}

    for item in chars:

        y = round(
            item["bbox"][1],
            1
        )

        key = int(round(y / 2.0))

        lines.setdefault(
            key,
            []
        ).append(item)

    results = []

    for line_chars in lines.values():

        line_chars.sort(
            key=lambda x: x["bbox"][0]
        )

        current = []

        for item in line_chars:

            ch = item["char"]

            if item["digit"]:

                current.append(item)
                continue

            if ch.isspace() or ch in \
                "+-()[]{}./#@*_:":

                continue

            if current:

                digits = candidate_digits(
                    current
                )

                if 8 <= len(digits) <= 15:

                    results.append(
                        current[:]
                    )

                current = []

        if current:

            digits = candidate_digits(
                current
            )

            if 8 <= len(digits) <= 15:

                results.append(
                    current[:]
                )

    return results


# -------------------------------------------------
# COUNTRY
# -------------------------------------------------

def detect_country(number):

    if number.startswith("52"):
        return "Mexico"

    if number.startswith("57"):
        return "Colombia"

    if number.startswith("54"):
        return "Argentina"

    if number.startswith("34"):
        return "Spain"

    if number.startswith("56"):
        return "Chile"

    if number.startswith("51"):
        return "Peru"

    if number.startswith("44"):
        return "UK"

    if number.startswith("1"):
        return "USA / Canada"

    return "Unknown"


# -------------------------------------------------
# ANALYZE
# -------------------------------------------------

@app.post("/analyze")
def analyze():

    try:

        body = request.get_json(
            force=True
        )

        pdf_bytes = clean_base64(
            body.get("data")
        )

        doc = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        found = {}

        for page_index in range(
            len(doc)
        ):

            page = doc[page_index]

            candidates =
                detect_numbers(page)

            for candidate in candidates:

                digits =
                    candidate_digits(
                        candidate
                    )

                if not (
                    8 <= len(digits) <= 15
                ):
                    continue

                raw =
                    candidate_text(
                        candidate
                    )

                if digits not in found:

                    found[digits] = {
                        "number": digits,
                        "country":
                            detect_country(
                                digits
                            ),
                        "count": 0,
                        "variants": []
                    }

                found[digits]["count"] += 1

                if raw not in \
                    found[digits]["variants"]:

                    found[digits]["variants"].append(
                        raw
                    )

        doc.close()

        numbers = list(
            found.values()
        )

        numbers.sort(
            key=lambda x: x["count"],
            reverse=True
        )

        return jsonify({
            "success": True,
            "numbers": numbers
        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


# -------------------------------------------------
# FONT EXTRACTION
# -------------------------------------------------

def get_font_file(
    doc,
    page,
    font_name
):

    fonts = page.get_fonts(
        full=True
    )

    for font in fonts:

        xref = font[0]
        basefont = font[3]

        if (
            basefont == font_name
            or font_name in basefont
            or basefont in font_name
        ):

            try:

                info =
                    doc.extract_font(
                        xref
                    )

                if info and len(info) >= 4:

                    font_bytes =
                        info[3]

                    if font_bytes:

                        suffix = (
                            ".ttf"
                            if info[1].lower()
                            in ("ttf", "truetype")
                            else ".otf"
                        )

                        temp =
                            tempfile.NamedTemporaryFile(
                                delete=False,
                                suffix=suffix
                            )

                        temp.write(
                            font_bytes
                        )

                        temp.close()

                        return temp.name

            except Exception:
                pass

    return None


# -------------------------------------------------
# EXACT DIGIT REPLACEMENT
# -------------------------------------------------

def replace_character_digit(
    page,
    char,
    new_digit,
    font_file=None
):

    x0, y0, x1, y1 =
        char["bbox"]

    origin_x, origin_y =
        char["origin"]

    rect = fitz.Rect(
        x0,
        y0,
        x1,
        y1
    )

    # Remove ONLY this glyph.
    page.add_redact_annot(
        rect,
        fill=False,
        cross_out=False
    )

    # Redaction is applied later.
    return {
        "origin": (
            origin_x,
            origin_y
        ),
        "size": char["size"],
        "color": char["color"],
        "font": char["font"],
        "font_file": font_file,
        "rect": rect,
        "digit": new_digit
    }


def insert_replacement(
    page,
    item
):

    origin =
        item["origin"]

    size =
        item["size"]

    color_int =
        item["color"]

    r =
        ((color_int >> 16) & 255) / 255

    g =
        ((color_int >> 8) & 255) / 255

    b =
        (color_int & 255) / 255

    color = (r, g, b)

    font_file =
        item.get("font_file")

    kwargs = {
        "fontsize": size,
        "color": color,
        "overlay": True
    }

    if font_file:

        kwargs["fontfile"] =
            font_file

    else:

        kwargs["fontname"] =
            "helv"

    page.insert_text(
        origin,
        item["digit"],
        **kwargs
    )


# -------------------------------------------------
# REPLACE
# -------------------------------------------------

@app.post("/replace")
def replace():

    temp_files = []

    try:

        body = request.get_json(
            force=True
        )

        pdf_bytes =
            clean_base64(
                body.get("data")
            )

        replacements =
            body.get(
                "replacements",
                []
            )

        replacement_map = {}

        for item in replacements:

            old =
                digits_only(
                    item.get("search")
                )

            new =
                digits_only(
                    item.get("replacement")
                )

            if (
                old
                and new
            ):

                replacement_map[
                    old
                ] = new

        doc = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        changed = 0

        for page_index in range(
            len(doc)
        ):

            page = doc[page_index]

            candidates =
                detect_numbers(page)

            pending = []

            for candidate in candidates:

                old_digits =
                    candidate_digits(
                        candidate
                    )

                if old_digits not in \
                    replacement_map:

                    continue

                new_digits =
                    replacement_map[
                        old_digits
                    ]

                # Exact digit count is safest.
                if len(new_digits) != \
                    len(old_digits):

                    continue

                for i, char in enumerate(
                    candidate
                ):

                    if not char["digit"]:
                        continue

                    if i >= len(new_digits):
                        continue

                    new_digit =
                        new_digits[i]

                    font_file =
                        get_font_file(
                            doc,
                            page,
                            char["font"]
                        )

                    if font_file:
                        temp_files.append(
                            font_file
                        )

                    item =
                        replace_character_digit(
                            page,
                            char,
                            new_digit,
                            font_file
                        )

                    pending.append(item)

                    changed += 1

            if pending:

                # Remove only the selected
                # original glyphs.
                page.apply_redactions(
                    images=0,
                    graphics=0,
                    text=0
                )

                # Put replacement glyphs
                # back at their ORIGINAL
                # coordinates.
                for item in pending:

                    insert_replacement(
                        page,
                        item
                    )

        output = io.BytesIO()

        doc.save(
            output,
            garbage=4,
            deflate=True,
            clean=True
        )

        doc.close()

        output.seek(0)

        encoded =
            base64.b64encode(
                output.read()
            ).decode("ascii")

        return jsonify({
            "success": True,
            "changedCharacters": changed,
            "fileName":
                make_output_name(
                    body.get(
                        "fileName"
                    )
                ),
            "data":
                "data:application/pdf;base64," +
                encoded
        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500

    finally:

        for path in temp_files:

            try:
                os.unlink(path)
            except Exception:
                pass


# -------------------------------------------------
# HEALTH
# -------------------------------------------------

@app.get("/")
def home():

    return jsonify({
        "status": "PDF Number Engine running"
    })


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "8080"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
