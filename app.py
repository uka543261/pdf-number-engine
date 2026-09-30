import base64
import gc
import io
import os
import re
import tempfile
import unicodedata

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

    # Automatically recognize Unicode decimal digits used by
    # fancy/LingoJam-style number variants.
    try:
        value = unicodedata.decimal(ch)

        if 0 <= value <= 9:
            return str(value)

    except (TypeError, ValueError):
        pass

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

                    digit = normalize_digit(
                        char
                    )

                    output.append({
                        "char": char,
                        "digit": digit,
                        "bbox": tuple(bbox),
                        "origin": tuple(origin),
                        "font": font,
                        "size": size,
                        "color": color,
                        "flags": flags,

                        # Needed only so a phone number
                        # can continue across a PDF
                        # text-line break.
                        "block_index": block_index,
                        "line_index": line_index
                    })

    return output


# -------------------------------------------------
# NUMBER HELPERS
# -------------------------------------------------

def candidate_digits(candidate):

    return "".join(
        x["digit"]
        for x in candidate
        if x["digit"]
    )


def candidate_text(candidate):

    return "".join(
        x["char"]
        for x in candidate
    )


# -------------------------------------------------
# NUMBER DETECTION
# -------------------------------------------------

def detect_numbers(page):

    """
    Detect phone numbers from their DIGITS.

    Non-digit/non-letter characters are treated as
    formatting automatically.

    No list of separators is used.

    Therefore all of these can work:

        +52 — 800 — 461—1544
        +52 – 800 • 461 ✦ 1544
        +52 | 800 | 461 | 1544
        +52 (800) 461/1544
        +52)800#461@1544

    A phone number can also continue onto the
    immediately following PDF text line when the
    line break is clearly part of the formatting.

    Example:

        +52 — 800 —
        461—1544

    becomes:

        528004611544

    And:

        +1 | 888 | 393
        | 2619

    becomes:

        18883932619
    """

    chars = page_characters(page)

    # ---------------------------------------------
    # Group characters by original PDF text block.
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

        # -----------------------------------------
        # Group by original PDF line.
        # -----------------------------------------

        lines = {}

        for item in block_chars:

            line_id = item["line_index"]

            lines.setdefault(
                line_id,
                []
            ).append(item)

        ordered_lines = []

        for line_id, line_chars in lines.items():

            line_chars.sort(
                key=lambda x:
                    x["bbox"][0]
            )

            ordered_lines.append(
                (
                    line_id,
                    line_chars
                )
            )

        ordered_lines.sort(
            key=lambda x: (
                min(
                    c["bbox"][1]
                    for c in x[1]
                ),
                x[0]
            )
        )

        # -----------------------------------------
        # Create digit fragments on each line.
        # -----------------------------------------

        fragments = []

        for line_position, (
            line_id,
            line_chars
        ) in enumerate(
            ordered_lines
        ):

            current = []

            for item in line_chars:

                # Digit
                if item["digit"]:

                    current.append(
                        item
                    )

                    continue

                # ANY non-alphanumeric character
                # is formatting.
                #
                # No hard-coded separator list.
                #
                # This automatically handles:
                # -  —  –  |  /  \
                # -  ( ) [ ] { }
                # -  # @ * _
                # -  • ✦ ~
                # - Unicode spaces
                # - other special characters

                if not item["char"].isalnum():

                    continue

                # A real letter/alphanumeric
                # character ends the number.
                if current:

                    digits = candidate_digits(
                        current
                    )

                    if 1 <= len(digits) <= 15:

                        fragments.append({
                            "items": current[:],
                            "line_position":
                                line_position,
                            "line_id":
                                line_id
                        })

                    current = []

            # End of line
            if current:

                digits = candidate_digits(
                    current
                )

                if 1 <= len(digits) <= 15:

                    fragments.append({
                        "items": current[:],
                        "line_position":
                            line_position,
                        "line_id":
                            line_id
                    })

        # -----------------------------------------
        # Merge genuine wrapped phone numbers.
        # -----------------------------------------

        i = 0

        while i < len(fragments):

            current = fragments[i][
                "items"
            ][:]

            current_digits = candidate_digits(
                current
            )

            last_fragment = fragments[i]

            j = i + 1

            while j < len(fragments):

                nxt = fragments[j]

                # Must be the immediately
                # following PDF text line.
                if nxt["line_position"] != (
                    last_fragment[
                        "line_position"
                    ] + 1
                ):

                    break

                next_items = nxt[
                    "items"
                ]

                next_digits = candidate_digits(
                    next_items
                )

                if not next_digits:
                    break

                # Never make a candidate longer
                # than the supported phone range.
                if (
                    len(current_digits)
                    + len(next_digits)
                    > 15
                ):

                    break

                previous_line = (
                    ordered_lines[
                        last_fragment[
                            "line_position"
                        ]
                    ][1]
                )

                next_line = (
                    ordered_lines[
                        nxt[
                            "line_position"
                        ]
                    ][1]
                )

                if not previous_line:
                    break

                if not next_line:
                    break

                previous_last = (
                    previous_line[-1]
                )

                next_first = (
                    next_line[0]
                )

                # Example:
                #
                # +52 — 800 —
                # 461—1544
                #
                # Previous line ends with
                # formatting.
                previous_has_formatting = (
                    not previous_last["digit"]
                    and not previous_last[
                        "char"
                    ].isalnum()
                )

                # Example:
                #
                # +1 | 888 | 393
                # | 2619
                #
                # Next line starts with
                # formatting.
                next_has_formatting = (
                    not next_first["digit"]
                    and not next_first[
                        "char"
                    ].isalnum()
                )

                # Only merge when the line break
                # is clearly part of formatting.
                if not (
                    previous_has_formatting
                    or next_has_formatting
                ):

                    break

                current.extend(
                    next_items
                )

                current_digits = (
                    candidate_digits(
                        current
                    )
                )

                last_fragment = nxt

                j += 1

            # Only actual phone-length candidates.
            if 8 <= len(
                current_digits
            ) <= 15:

                results.append(
                    current
                )

            i = max(
                i + 1,
                j
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

            page = doc[
                page_index
            ]

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
                        "country":
                            detect_country(
                                digits
                            ),
                        "count": 0,
                        "variants": []
                    }

                found[digits][
                    "count"
                ] += 1

                if (
                    raw
                    not in found[digits][
                        "variants"
                    ]
                ):

                    found[digits][
                        "variants"
                    ].append(
                        raw
                    )

        doc.close()

        numbers = list(
            found.values()
        )

        numbers.sort(
            key=lambda x:
                x["count"],
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

def get_font_buffer(
    doc,
    fonts,
    font_name,
    font_cache
):
    """
    SPEED ONLY:
    Extract each embedded font once per replacement request.
    """

    cache_key = font_name or ""

    if cache_key in font_cache:
        return font_cache[cache_key]

    matched_xref = None

    for font in fonts:

        xref = font[0]
        basefont = font[3] or ""

        if (
            basefont == font_name
            or font_name in basefont
            or basefont in font_name
        ):
            matched_xref = xref
            break

    if matched_xref is None:
        font_cache[cache_key] = None
        return None

    try:

        info = doc.extract_font(
            matched_xref
        )

        if info and len(info) >= 4:

            font_bytes = info[3]

            if font_bytes:
                font_cache[cache_key] = font_bytes
                return font_bytes

    except Exception:
        pass

    font_cache[cache_key] = None
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

    x0, y0, x1, y1 = (
        char["bbox"]
    )

    origin_x, origin_y = (
        char["origin"]
    )

    rect = fitz.Rect(
        x0,
        y0,
        x1,
        y1
    )

    # Remove ONLY this original
    # digit glyph.
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

        # Keep original font size.
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

    kwargs = {
        "fontsize": size,
        "color": color,
        "overlay": True
    }

    registered_font = item.get(
        "registered_font"
    )

    if registered_font:

        kwargs["fontname"] = (
            registered_font
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

    try:

        body = request.get_json(
            force=True
        )

        pdf_bytes = clean_base64(
            body.get("data")
        )

        replacements = body.get(
            "replacements",
            []
        )

        replacement_map = {}

        for item in replacements:

            old = digits_only(
                item.get("search")
            )

            new = digits_only(
                item.get("replacement")
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

        # SPEED ONLY:
        # Embedded font bytes are extracted once for the whole PDF.
        font_cache = {}

        # SPEED ONLY:
        # Keep each page's font table only while that page is processed.
        for page_index in range(
            len(doc)
        ):

            page = doc[
                page_index
            ]

            # full=True is not required for matching the base font name.
            # The replacement logic only needs the standard font tuple.
            page_fonts = page.get_fonts(
                full=False
            )

            # Register a PDF font once per page/font instead of passing
            # a font file to every single inserted digit.
            page_font_aliases = {}
            page_font_counter = 0

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

                # Keep the original number's
                # digit count.
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

                    font_name = (
                        char["font"]
                    )

                    if (
                        font_name
                        in page_font_aliases
                    ):

                        registered_font = (
                            page_font_aliases[
                                font_name
                            ]
                        )

                    else:

                        font_buffer = (
                            get_font_buffer(
                                doc,
                                page_fonts,
                                font_name,
                                font_cache
                            )
                        )

                        registered_font = None

                        if font_buffer:

                            registered_font = (
                                "R"
                                + str(
                                    page_font_counter
                                )
                            )

                            page_font_counter += 1

                            try:

                                page.insert_font(
                                    fontname=(
                                        registered_font
                                    ),
                                    fontbuffer=(
                                        font_buffer
                                    )
                                )

                            except Exception:

                                registered_font = None

                        page_font_aliases[
                            font_name
                        ] = registered_font

                    item = (
                        replace_character_digit(
                            page,
                            char,
                            new_digit,
                            None
                        )
                    )

                    item[
                        "registered_font"
                    ] = registered_font

                    pending.append(
                        item
                    )

                    changed += 1

                    digit_index += 1

            if pending:

                # Remove ONLY the selected
                # original digit glyphs.
                page.apply_redactions(
                    images=0,
                    graphics=0,
                    text=0
                )

                # Put replacement digits back
                # at their original coordinates
                # and original font size.
                for item in pending:

                    insert_replacement(
                        page,
                        item
                    )

            # Release references without forcing a full GC cycle on
            # every page. This avoids unnecessary CPU time.
            candidates = None
            pending = None
            page_fonts = None
            page_font_aliases = None

        output = io.BytesIO()

        # Fast save: avoid expensive garbage/cleanup passes.
        doc.save(
            output,
            garbage=0,
            deflate=True,
            clean=False
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
