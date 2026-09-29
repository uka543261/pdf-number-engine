import base64
import io
import os
import re
import tempfile

import fitz
from flask import Flask, request, jsonify

app = Flask(__name__)

# -------------------------------------------------
# SETTINGS
# -------------------------------------------------

MIN_DIGITS = 8
MAX_DIGITS = 15

# Digits ke beech maximum special formatting characters.
# Example:
# +52-800-461-1544
# +52/800)-(461)#@1544
MAX_SEPARATOR_CHARS = 8


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
# COUNTRY
# -------------------------------------------------

def detect_country(number):
    number = str(number)

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
# FAST PHONE DETECTION
#
# Analysis ke waqt rawdict use nahi karna.
# Normal PDF text extraction much faster hai.
# -------------------------------------------------

PHONE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"\+?"
    r"\d"
    r"(?:"
        r"[^A-Za-z0-9\r\n]{0," +
        str(MAX_SEPARATOR_CHARS) +
        r"}"
        r"\d"
    r"){7,14}"
    r"(?![A-Za-z0-9])"
)


def extract_text_fast(page):
    """
    Fast text extraction.
    rawdict nahi use karta.
    """
    try:
        return page.get_text(
            "text",
            sort=True
        ) or ""
    except Exception:
        return ""


def find_numbers_fast(text):
    found = []

    if not text:
        return found

    # Normal lines
    for match in PHONE_PATTERN.finditer(text):

        raw = match.group(0).strip()

        digits = digits_only(raw)

        if (
            MIN_DIGITS
            <= len(digits)
            <= MAX_DIGITS
        ):
            found.append(
                (
                    raw,
                    digits
                )
            )

    return found


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

        if not pdf_bytes:
            return jsonify({
                "error": "PDF data missing."
            }), 400

        doc = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        found = {}

        for page in doc:

            text = extract_text_fast(
                page
            )

            matches = find_numbers_fast(
                text
            )

            for raw, digits in matches:

                if digits not in found:

                    found[digits] = {
                        "number": digits,
                        "country": detect_country(
                            digits
                        ),
                        "count": 0,
                        "variants": []
                    }

                found[digits][
                    "count"
                ] += 1

                if (
                    raw not in
                    found[digits][
                        "variants"
                    ]
                ):

                    found[digits][
                        "variants"
                    ].append(raw)

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
# CHARACTER EXTRACTION
#
# Ye sirf replacement ke waqt chalega.
# -------------------------------------------------

def page_characters(page):

    data = page.get_text(
        "rawdict"
    )

    output = []

    for block in data.get(
        "blocks",
        []
    ):

        if block.get("type") != 0:
            continue

        for line in block.get(
            "lines",
            []
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

                for ch in span.get(
                    "chars",
                    []
                ):

                    char = ch.get(
                        "c",
                        ""
                    )

                    bbox = ch.get(
                        "bbox"
                    )

                    origin = ch.get(
                        "origin"
                    )

                    if (
                        not char
                        or not bbox
                        or not origin
                    ):
                        continue

                    digit = (
                        char
                        if char.isdigit()
                        else None
                    )

                    output.append({

                        "char": char,

                        "digit": digit,

                        "bbox": tuple(
                            bbox
                        ),

                        "origin": tuple(
                            origin
                        ),

                        "font": font,

                        "size": size,

                        "color": color
                    })

    return output


# -------------------------------------------------
# FIND PHONE NUMBERS FROM PDF CHARACTERS
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


def detect_numbers_from_chars(page):

    chars = page_characters(
        page
    )

    # Group characters by visual line.
    lines = {}

    for item in chars:

        y = item["bbox"][1]

        key = int(
            round(y / 2.0)
        )

        lines.setdefault(
            key,
            []
        ).append(item)

    results = []

    for line_chars in lines.values():

        line_chars.sort(
            key=lambda x:
                x["bbox"][0]
        )

        current = []

        for item in line_chars:

            char = item["char"]

            if item["digit"]:

                current.append(
                    item
                )

                continue

            # Allowed formatting characters.
            if (
                char.isspace()
                or char in
                "+-()[]{}./#@*_:=;"
            ):

                continue

            if current:

                digits = candidate_digits(
                    current
                )

                if (
                    MIN_DIGITS
                    <= len(digits)
                    <= MAX_DIGITS
                ):

                    results.append(
                        current[:]
                    )

                current = []

        if current:

            digits = candidate_digits(
                current
            )

            if (
                MIN_DIGITS
                <= len(digits)
                <= MAX_DIGITS
            ):

                results.append(
                    current[:]
                )

    return results


# -------------------------------------------------
# FONT CACHE
# -------------------------------------------------

def get_font_file(
    doc,
    page,
    font_name,
    cache
):

    cache_key = str(
        font_name
    )

    if cache_key in cache:
        return cache[cache_key]

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
                            tempfile
                            .NamedTemporaryFile(
                                delete=False,
                                suffix=suffix
                            )
                        )

                        temp.write(
                            font_bytes
                        )

                        temp.close()

                        cache[
                            cache_key
                        ] = temp.name

                        return temp.name

            except Exception:
                pass

    cache[
        cache_key
    ] = None

    return None


# -------------------------------------------------
# REPLACE ONE DIGIT
# -------------------------------------------------

def prepare_replacement(
    page,
    char,
    new_digit,
    font_file
):

    x0, y0, x1, y1 = char[
        "bbox"
    ]

    origin_x, origin_y = char[
        "origin"
    ]

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

        "size":
            char["size"],

        "color":
            char["color"],

        "font_file":
            font_file,

        "digit":
            new_digit
    }


def insert_replacement(
    page,
    item
):

    color_int = item[
        "color"
    ]

    r = (
        (color_int >> 16)
        & 255
    ) / 255.0

    g = (
        (color_int >> 8)
        & 255
    ) / 255.0

    b = (
        color_int
        & 255
    ) / 255.0

    kwargs = {

        "fontsize":
            item["size"],

        "color":
            (r, g, b),

        "overlay":
            True
    }

    font_file = item.get(
        "font_file"
    )

    if font_file:

        kwargs[
            "fontfile"
        ] = font_file

    else:

        kwargs[
            "fontname"
        ] = "helv"

    page.insert_text(
        item["origin"],
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

        replacements = body.get(
            "replacements",
            []
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

            if not old or not new:
                continue

            # Exact same digit count.
            # Isse original character positions
            # preserve karna possible hota hai.
            if len(old) != len(new):
                continue

            replacement_map[
                old
            ] = new

        if not replacement_map:

            return jsonify({
                "error":
                    "Valid replacement nahi mila. Old aur new number mein same digit count hona chahiye."
            }), 400

        doc = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        changed = 0

        # Font files ko baar-baar extract
        # karne se bachane ke liye cache.
        font_cache = {}

        for page in doc:

            candidates = (
                detect_numbers_from_chars(
                    page
                )
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
                    not in
                    replacement_map
                ):
                    continue

                new_digits = (
                    replacement_map[
                        old_digits
                    ]
                )

                digit_index = 0

                for char in candidate:

                    if not char[
                        "digit"
                    ]:
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

                    digit_index += 1

                    font_file = (
                        get_font_file(
                            doc,
                            page,
                            char["font"],
                            font_cache
                        )
                    )

                    if font_file:
                        temp_files.append(
                            font_file
                        )

                    item = (
                        prepare_replacement(
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

            if pending:

                page.apply_redactions(
                    images=0,
                    graphics=0,
                    text=0
                )

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

        encoded = (
            base64.b64encode(
                output.read()
            ).decode(
                "ascii"
            )
        )

        return jsonify({

            "success":
                True,

            "changedCharacters":
                changed,

            "fileName":
                make_output_name(
                    body.get(
                        "fileName"
                    )
                ),

            "data":
                (
                    "data:application/pdf;base64,"
                    + encoded
                )
        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500

    finally:

        # Duplicate paths remove karo.
        for path in set(
            temp_files
        ):

            try:
                os.unlink(
                    path
                )

            except Exception:
                pass


# -------------------------------------------------
# HEALTH CHECK
# -------------------------------------------------

@app.get("/")
def home():

    return jsonify({
        "status":
            "PDF Number Engine running"
    })


# -------------------------------------------------
# START
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
