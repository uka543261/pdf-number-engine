import base64
import io
import os
import re

import fitz
from flask import Flask, request, jsonify

app = Flask(__name__)


# =================================================
# BASIC HELPERS
# =================================================

def digits_only(value):
    return re.sub(r"\D", "", str(value or ""))


def clean_base64(value):
    value = str(value or "").strip()

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


# =================================================
# UNICODE DIGITS
# =================================================

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


# =================================================
# PDF CHARACTER EXTRACTION
# =================================================

def page_characters(page):
    data = page.get_text("rawdict")

    output = []

    for block in data.get("blocks", []):

        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):

            for span in line.get("spans", []):

                font = span.get("font", "")
                size = span.get("size", 10)
                color = span.get("color", 0)
                flags = span.get("flags", 0)

                for ch in span.get("chars", []):

                    char = ch.get("c", "")
                    bbox = ch.get("bbox")
                    origin = ch.get("origin")

                    if not char:
                        continue

                    if not bbox or not origin:
                        continue

                    output.append({
                        "char": char,
                        "digit": normalize_digit(char),
                        "bbox": tuple(bbox),
                        "origin": tuple(origin),
                        "font": font,
                        "size": size,
                        "color": color,
                        "flags": flags
                    })

    return output


# =================================================
# NUMBER DETECTION
# =================================================

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


def detect_numbers(page):

    chars = page_characters(page)

    lines = {}

    for item in chars:

        y = round(
            item["bbox"][1],
            1
        )

        key = int(
            round(y / 2.0)
        )

        lines.setdefault(
            key,
            []
        ).append(item)

    results = []

    allowed_separators = set(
        "+-()[]{}./#@*_:" 
    )

    for line_chars in lines.values():

        line_chars.sort(
            key=lambda x: x["bbox"][0]
        )

        current = []

        for item in line_chars:

            ch = item["char"]

            # Digit
            if item["digit"]:

                current.append(item)
                continue

            # Allowed formatting character.
            # It does NOT become part of the digit sequence.
            if (
                ch.isspace()
                or ch in allowed_separators
            ):
                continue

            # Another character means the current
            # number has ended.
            if current:

                digits = candidate_digits(
                    current
                )

                if 8 <= len(digits) <= 15:
                    results.append(
                        current[:]
                    )

                current = []

        # End of line
        if current:

            digits = candidate_digits(
                current
            )

            if 8 <= len(digits) <= 15:
                results.append(
                    current[:]
                )

    return results


# =================================================
# COUNTRY
# =================================================

def detect_country(number):

    prefixes = {
        "52": "Mexico",
        "57": "Colombia",
        "54": "Argentina",
        "34": "Spain",
        "56": "Chile",
        "51": "Peru",
        "44": "UK",
        "1": "USA / Canada"
    }

    for prefix, country in prefixes.items():

        if number.startswith(prefix):
            return country

    return "Unknown"


# =================================================
# ANALYZE
# =================================================

@app.post("/analyze")
def analyze():

    try:

        body = request.get_json(
            force=True
        ) or {}

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


# =================================================
# FONT HANDLING
# =================================================

def get_font_buffer(
    doc,
    page,
    font_name
):

    if not font_name:
        return None

    for font in page.get_fonts(
        full=True
    ):

        xref = font[0]
        basefont = font[3] or ""

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
                    and info[3]
                ):

                    return info[3]

            except Exception:

                return None

    return None


def color_tuple(color_int):

    color_int = int(
        color_int or 0
    )

    r = (
        (color_int >> 16) & 255
    ) / 255.0

    g = (
        (color_int >> 8) & 255
    ) / 255.0

    b = (
        color_int & 255
    ) / 255.0

    return (
        r,
        g,
        b
    )


def prepare_font(
    page,
    font_buffer,
    font_key
):

    # If original font cannot be extracted,
    # use Helvetica safely.
    if not font_buffer:
        return "helv"

    safe_key = (
        "F"
        + re.sub(
            r"[^A-Za-z0-9_]",
            "_",
            font_key or "Original"
        )
    )

    if not safe_key:
        safe_key = "FOriginal"

    try:

        page.insert_font(
            fontname=safe_key,
            fontbuffer=font_buffer
        )

        return safe_key

    except Exception:

        # Important:
        # Never let a bad embedded font crash
        # the complete PDF replacement.
        return "helv"


# =================================================
# CHARACTER REPLACEMENT
# =================================================

def replace_character_digit(
    page,
    char,
    new_digit,
    font_name
):

    x0, y0, x1, y1 = char["bbox"]

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
        "origin": char["origin"],
        "size": char["size"],
        "color": char["color"],
        "font_name": font_name,
        "digit": new_digit
    }


def insert_replacement(
    page,
    item
):

    page.insert_text(
        item["origin"],
        item["digit"],
        fontsize=item["size"],
        fontname=item["font_name"],
        color=color_tuple(
            item["color"]
        ),
        overlay=True
    )


# =================================================
# REPLACE
# =================================================

@app.post("/replace")
def replace():

    try:

        body = request.get_json(
            force=True
        ) or {}

        pdf_bytes = clean_base64(
            body.get("data")
        )

        replacements = (
            body.get("replacements")
            or []
        )

        replacement_map = {}

        for item in replacements:

            if not isinstance(
                item,
                dict
            ):
                continue

            old_value = item.get(
                "search"
            )

            if old_value is None:
                old_value = item.get(
                    "old"
                )

            new_value = item.get(
                "replacement"
            )

            if new_value is None:
                new_value = item.get(
                    "new"
                )

            old = digits_only(
                old_value
            )

            new = digits_only(
                new_value
            )

            # Same number of digits is required.
            # This preserves the original character positions.
            if (
                old
                and new
                and len(old) == len(new)
            ):

                replacement_map[
                    old
                ] = new

        if not replacement_map:

            return jsonify({
                "error":
                    "No valid replacements supplied."
            }), 400

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

                old_digits = candidate_digits(
                    candidate
                )

                new_digits = replacement_map.get(
                    old_digits
                )

                if not new_digits:
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

                    # Try to preserve the original
                    # embedded font.
                    font_buffer = get_font_buffer(
                        doc,
                        page,
                        char["font"]
                    )

                    font_name = prepare_font(
                        page,
                        font_buffer,
                        char["font"]
                    )

                    pending.append(
                        replace_character_digit(
                            page,
                            char,
                            new_digits[
                                digit_index
                            ],
                            font_name
                        )
                    )

                    digit_index += 1
                    changed += 1

            if pending:

                # Remove ONLY the selected
                # original digit glyphs.
                page.apply_redactions(
                    images=0,
                    graphics=0,
                    text=0
                )

                # Put new digit at the exact
                # original character position.
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


# =================================================
# HEALTH CHECK
# =================================================

@app.get("/")
def home():

    return jsonify({
        "status":
            "PDF Number Engine running"
    })


# =================================================
# LOCAL RUN
# =================================================

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
