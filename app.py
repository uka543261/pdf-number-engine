import base64
import io
import os
import re
import tempfile

import fitz
from flask import Flask, request, jsonify

app = Flask(__name__)

MIN_DIGITS = 8
MAX_DIGITS = 15
MAX_SEPARATOR_CHARS = 8


# =================================================
# BASIC
# =================================================

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


# =================================================
# COUNTRY
# =================================================

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


# =================================================
# FAST ANALYSIS
# =================================================

PHONE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"\+?"
    r"\d"
    r"(?:"
    r"[^A-Za-z0-9\r\n]{0,8}"
    r"\d"
    r"){7,14}"
    r"(?![A-Za-z0-9])"
)


def find_numbers_fast(text):

    results = []

    if not text:
        return results

    for match in PHONE_PATTERN.finditer(text):

        raw = match.group(0).strip()

        digits = digits_only(raw)

        if (
            MIN_DIGITS
            <= len(digits)
            <= MAX_DIGITS
        ):
            results.append(
                (
                    raw,
                    digits
                )
            )

    return results


# =================================================
# ANALYZE ENDPOINT
# =================================================

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
                "error":
                    "PDF data missing."
            }), 400

        doc = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        found = {}

        for page in doc:

            text = page.get_text(
                "text",
                sort=True
            ) or ""

            for raw, digits in find_numbers_fast(
                text
            ):

                if digits not in found:

                    found[digits] = {

                        "number":
                            digits,

                        "country":
                            detect_country(
                                digits
                            ),

                        "count":
                            0,

                        "variants":
                            []
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

            "success":
                True,

            "numbers":
                numbers
        })

    except Exception as e:

        return jsonify({
            "error":
                str(e)
        }), 500


# =================================================
# CHARACTER EXTRACTION
# ONLY USED ON TARGET PAGES
# =================================================

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

                        "char":
                            char,

                        "digit":
                            digit,

                        "bbox":
                            tuple(bbox),

                        "origin":
                            tuple(origin),

                        "font":
                            font,

                        "size":
                            size,

                        "color":
                            color
                    })

    return output


# =================================================
# CHARACTER NUMBER DETECTION
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


def detect_numbers_from_chars(page):

    chars = page_characters(
        page
    )

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

            # Formatting characters are
            # allowed inside the number.
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


# =================================================
# TARGET PAGE DETECTION
#
# FAST:
# page text ko digits-only karke target
# number present hai ya nahi check.
# =================================================

def find_target_pages(
    doc,
    replacement_map
):

    target_pages = set()

    targets = list(
        replacement_map.keys()
    )

    if not targets:
        return target_pages

    for page_index in range(
        len(doc)
    ):

        page = doc[
            page_index
        ]

        text = page.get_text(
            "text",
            sort=True
        ) or ""

        page_digits = digits_only(
            text
        )

        if not page_digits:
            continue

        for target in targets:

            if target in page_digits:

                target_pages.add(
                    page_index
                )

                break

    return target_pages


# =================================================
# FONT CACHE
# =================================================

def get_font_file(
    doc,
    page,
    font_name,
    font_cache,
    temp_files
):

    cache_key = str(
        font_name or ""
    )

    if cache_key in font_cache:

        return font_cache[
            cache_key
        ]

    fonts = page.get_fonts(
        full=True
    )

    for font in fonts:

        xref = font[0]

        basefont = str(
            font[3] or ""
        )

        if not (
            basefont == font_name
            or font_name in basefont
            or basefont in font_name
        ):
            continue

        try:

            info = doc.extract_font(
                xref
            )

            if (
                not info
                or len(info) < 4
            ):
                continue

            font_bytes = info[3]

            if not font_bytes:
                continue

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

            temp = tempfile.NamedTemporaryFile(
                delete=False,
                suffix=suffix
            )

            temp.write(
                font_bytes
            )

            temp.close()

            path = temp.name

            font_cache[
                cache_key
            ] = path

            temp_files.append(
                path
            )

            return path

        except Exception:
            continue

    font_cache[
        cache_key
    ] = None

    return None


# =================================================
# PREPARE CHARACTER
# =================================================

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

        "origin":
            (
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


# =================================================
# INSERT CHARACTER
# =================================================

def insert_replacement(
    page,
    item
):

    color_int = int(
        item["color"] or 0
    )

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


# =================================================
# REPLACE ENDPOINT
# =================================================

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

        if not pdf_bytes:

            return jsonify({
                "error":
                    "PDF data missing."
            }), 400

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

            # Same number of digits is
            # required to keep positions.
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


        # -----------------------------------------
        # OPEN PDF
        # -----------------------------------------

        doc = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )


        # -----------------------------------------
        # FAST TARGET PAGE SEARCH
        # -----------------------------------------

        target_pages = find_target_pages(
            doc,
            replacement_map
        )


        if not target_pages:

            doc.close()

            return jsonify({

                "error":
                    "Replacement numbers PDF mein nahi mile."

            }), 400


        # -----------------------------------------
        # FONT CACHE
        # -----------------------------------------

        font_cache = {}


        changed = 0


        # -----------------------------------------
        # ONLY TARGET PAGES
        # -----------------------------------------

        for page_index in sorted(
            target_pages
        ):

            page = doc[
                page_index
            ]


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


                # Safety check
                if (
                    len(old_digits)
                    !=
                    len(new_digits)
                ):

                    continue


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
                            char[
                                "font"
                            ],
                            font_cache,
                            temp_files
                        )
                    )


                    pending.append(
                        prepare_replacement(
                            page,
                            char,
                            new_digit,
                            font_file
                        )
                    )


                    changed += 1


            # -------------------------------------
            # APPLY PAGE CHANGES ONCE
            # -------------------------------------

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


        # -----------------------------------------
        # SAVE
        # -----------------------------------------

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
        ).decode(
            "ascii"
        )


        return jsonify({

            "success":
                True,

            "changedCharacters":
                changed,

            "pagesProcessed":
                len(target_pages),

            "totalPages":
                len(
                    fitz.open(
                        stream=pdf_bytes,
                        filetype="pdf"
                    )
                ),

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

            "error":
                str(e)

        }), 500


    finally:

        # Delete temporary font files.
        for path in set(
            temp_files
        ):

            try:

                os.unlink(
                    path
                )

            except Exception:

                pass


# =================================================
# HEALTH
# =================================================

@app.get("/")
def home():

    return jsonify({

        "status":
            "PDF Number Engine running"

    })


# =================================================
# START
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
