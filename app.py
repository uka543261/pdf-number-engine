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

for block_start in (
    0x0660,
    0x06F0,
    0x0966,
    0x09E6,
    0x0AE6,
    0x0BE6,
    0x0C66,
    0x0CE6,
    0x0D66,
    0x0DE6
):
    for i in range(10):
        UNICODE_DIGIT_MAP[
            chr(block_start + i)
        ] = chr(0x30 + i)


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

    for block_index, block in enumerate(
        data.get("blocks", [])
    ):

        if block.get("type") != 0:
            continue

        for line_index, line in enumerate(
            block.get("lines", [])
        ):

            for span in line.get(
                "spans",
                []
            ):

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

                for ch in span.get(
                    "chars",
                    []
                ):

                    char = ch.get(
                        "c",
                        ""
                    )

                    if not char:
                        continue

                    bbox = ch.get(
                        "bbox"
                    )

                    origin = ch.get(
                        "origin"
                    )

                    if not bbox or not origin:
                        continue

                    output.append({
                        "char": char,
                        "digit": normalize_digit(
                            char
                        ),
                        "bbox": tuple(bbox),
                        "origin": tuple(origin),
                        "font": font,
                        "size": size,
                        "color": color,
                        "flags": flags,

                        # IMPORTANT:
                        # Keep PDF block/line information.
                        "block_index": block_index,
                        "line_index": line_index
                    })

    return output


# -------------------------------------------------
# NUMBER HELPERS
# -------------------------------------------------

def candidate_digits(candidate):

    return "".join(
        item["digit"]
        for item in candidate
        if item["digit"]
    )


def candidate_text(candidate):

    return "".join(
        item["char"]
        for item in candidate
    )


# -------------------------------------------------
# NUMBER DETECTION
# -------------------------------------------------

def detect_numbers(page):

    chars = page_characters(page)

    # ---------------------------------------------
    # Group characters by their ORIGINAL PDF block.
    #
    # This is important because a phone number can
    # continue onto the next visual text line.
    # ---------------------------------------------

    blocks = {}

    for item in chars:

        block_id = item["block_index"]

        blocks.setdefault(
            block_id,
            []
        ).append(item)

    results = []

    for block_chars in blocks.values():

        # Keep PDF reading order:
        # line first, then horizontal position.
        block_chars.sort(
            key=lambda x: (
                x["line_index"],
                x["bbox"][0]
            )
        )

        current = []

        for item in block_chars:

            # -------------------------------------
            # DIGIT
            # -------------------------------------

            if item["digit"]:

                current.append(item)

                continue

            # -------------------------------------
            # NON-DIGIT
            # -------------------------------------
            #
            # IMPORTANT:
            #
            # We do NOT maintain a list such as:
            #
            # "+-()[]{}./#@*_:"
            #
            # ANY non-alphanumeric character is
            # treated as formatting.
            #
            # Examples:
            #
            # -
            # —
            # –
            # /
            # (
            # )
            # [
            # ]
            # #
            # @
            # •
            # ✦
            # Unicode spaces
            # etc.
            #
            # They do NOT break the number.
            # -------------------------------------

            if not item["char"].isalnum():

                continue

            # -------------------------------------
            # A real letter/alphanumeric character
            # ends the current number.
            # -------------------------------------

            if current:

                digits = candidate_digits(
                    current
                )

                if 8 <= len(digits) <= 15:

                    results.append(
                        current[:]
                    )

                current = []

        # -----------------------------------------
        # End of PDF text block
        # -----------------------------------------

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

            candidates = detect_numbers(
                page
            )

            for candidate in candidates:

                digits = candidate_digits(
                    candidate
                )

                if not (
                    8 <= len(digits) <= 15
                ):
                    continue

                raw = candidate_text(
                    candidate
                )

                if digits not in found:

                    found[digits] = {
                        "number": digits,
                        "country": detect_country(
                            digits
                        ),
                        "count": 0,
                        "variants": []
                    }

                found[digits]["count"] += 1

                if (
                    raw
                    not in found[digits]["variants"]
                ):

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

                info = doc.extract_font(
                    xref
                )

                if (
                    info
                    and len(info) >= 4
                ):

                    font_bytes = info[3]

                    if font_bytes:

                        suffix = (
                            ".ttf"
                            if str(
                                info[1]
                            ).lower()
                            in (
                                "ttf",
                                "truetype"
                            )
                            else ".otf"
                        )

                        temp = (
                            tempfile.NamedTemporaryFile(
                                delete=False,
                                suffix=suffix
                            )
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

    x0, y0, x1, y1 = char["bbox"]

    origin_x, origin_y = (
        char["origin"]
    )

    rect = fitz.Rect(
        x0,
        y0,
        x1,
        y1
    )

    page.add_redact_annot(
        rect,
        fill=False,
        cross_out=False
    )

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

    origin = item["origin"]

    size = item["size"]

    color_int = item["color"]

    r = (
        (color_int >> 16)
        & 255
    ) / 255

    g = (
        (color_int >> 8)
        & 255
    ) / 255

    b = (
        color_int
        & 255
    ) / 255

    color = (
        r,
        g,
        b
    )

    font_file = item.get(
        "font_file"
    )

    kwargs = {
        "fontsize": size,
        "color": color,
        "overlay": True
    }

    # Keep the existing font strategy.
    # We are NOT changing this part unnecessarily.
    if font_file:

        kwargs["fontfile"] = (
            font_file
        )

    else:

        kwargs["fontname"] = "helv"

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

        pdf_bytes = clean_base64(
            body.get("data")
        )

        replacements = (
            body.get(
                "replacements",
                []
            )
        )

        replacement_map = {}

        for item in replacements:

            old = digits_only(
                item.get(
                    "search"
                )
            )

            new = digits_only(
                item.get(
                    "replacement"
                )
            )

            if old and new:

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

            candidates = detect_numbers(
                page
            )

            pending = []

            for candidate in candidates:

                old_digits = (
                    candidate_digits(
                        candidate
                    )
                )

                if (
                    old_digits
                    not in replacement_map
                ):
                    continue

                new_digits = (
                    replacement_map[
                        old_digits
                    ]
                )

                # Same digit count is required
                # so original character positions
                # remain unchanged.
                if (
                    len(new_digits)
                    != len(old_digits)
                ):
                    continue

                digit_index = 0

                for char in candidate:

                    if not char["digit"]:
                        continue

                    if (
                        digit_index
                        >= len(new_digits)
                    ):
                        break

                    new_digit = (
                        new_digits[
                            digit_index
                        ]
                    )

                    font_file = (
                        get_font_file(
                            doc,
                            page,
                            char["font"]
                        )
                    )

                    if font_file:

                        temp_files.append(
                            font_file
                        )

                    item = (
                        replace_character_digit(
                            page,
                            char,
                            new_digit,
                            font_file
                        )
                    )

                    pending.append(
                        item
                    )

                    changed += 1

                    digit_index += 1

            if pending:

                # Remove ONLY the original
                # digit glyphs.
                page.apply_redactions(
                    images=0,
                    graphics=0,
                    text=0
                )

                # Put replacement digits back
                # at their original coordinates.
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

        encoded = base64.b64encode(
            output.read()
        ).decode("ascii")

        return jsonify({

            "success": True,

            "changedCharacters":
                changed,

            "fileName":
                make_output_name(
                    body.get(
                        "fileName"
                    )
                ),

            "data":
                "data:application/pdf;base64,"
                + encoded
        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500

    finally:

        for path in temp_files:

            try:

                os.unlink(
                    path
                )

            except Exception:

                pass


# -------------------------------------------------
# HEALTH
# -------------------------------------------------

@app.get("/")
def home():

    return jsonify({
        "status":
            "PDF Number Engine running"
    })


# -------------------------------------------------
# RUN
# -------------------------------------------------

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
