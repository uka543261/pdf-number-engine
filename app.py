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

def get_font_file(
    doc,
    page,
    font_name,
    font_cache
):
    """Return an extracted font file path once per PDF font name."""

    cache_key = font_name or ""

    if cache_key in font_cache:
        return font_cache[cache_key]

    matched_xref = None

    for font in page.get_fonts(full=True):

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
        info = doc.extract_font(matched_xref)

        if info and len(info) >= 4:
            font_bytes = info[3]

            if font_bytes:
                suffix = ".ttf" if str(info[1]).lower() in ("ttf", "truetype") else ".otf"
                temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                temp.write(font_bytes)
                temp.close()
                path = temp.name
                font_cache[cache_key] = path
                return path

    except Exception:
        pass

    font_cache[cache_key] = None
    return None


def replace_character_digit(
    page,
    char,
    new_digit,
    font_name=None
):

    x0, y0, x1, y1 = char["bbox"]
    origin_x, origin_y = char["origin"]

    rect = fitz.Rect(x0, y0, x1, y1)

    page.add_redact_annot(
        rect,
        fill=False,
        cross_out=False
    )

    return {
        "origin": (origin_x, origin_y),
        "size": char["size"],
        "color": char["color"],
        "font_file": font_name,
        "digit": new_digit
    }


def insert_replacement(page, item):

    color_int = item["color"]

    color = (
        ((color_int >> 16) & 255) / 255,
        ((color_int >> 8) & 255) / 255,
        (color_int & 255) / 255
    )

    kwargs = {
        "fontsize": item["size"],
        "color": color,
        "overlay": True
    }

    if item.get("font_name"):
        kwargs["fontname"] = item["font_name"]
    elif item.get("font_file"):
        kwargs["fontfile"] = item["font_file"]
    else:
        kwargs["fontname"] = "helv"

    page.insert_text(
        item["origin"],
        item["digit"],
        **kwargs
    )


# -------------------------------------------------
# FAST REPLACEMENT HELPERS
# -------------------------------------------------

def find_target_numbers(page, target_map):
    """
    Fast replacement-only scanner.

    Unlike detect_numbers(), this does not build every possible phone
    candidate. It only looks for the exact digit strings requested by
    the user and keeps the original character objects/formatting.
    """
    chars = page_characters(page)
    if not chars:
        return []

    # Work inside each original text block.  A phone number can wrap to
    # the next line, so block text is flattened while letters still act
    # as hard boundaries and non-alphanumeric characters are formatting.
    blocks = {}
    for item in chars:
        blocks.setdefault(item["block_index"], []).append(item)

    found = []
    targets = sorted(target_map, key=len, reverse=True)

    for block_chars in blocks.values():
        # Keep PDF reading order: line Y, then line id, then X.
        lines = {}
        for item in block_chars:
            lines.setdefault(item["line_index"], []).append(item)

        ordered = []
        for line_id, line_chars in lines.items():
            line_chars.sort(key=lambda x: x["bbox"][0])
            if line_chars:
                ordered.append((
                    min(x["bbox"][1] for x in line_chars),
                    line_id,
                    line_chars
                ))
        ordered.sort(key=lambda x: (x[0], x[1]))

        # Build only digit runs. Adjacent runs on consecutive lines are
        # joined when the line break is clearly formatting.
        fragments = []
        for pos, (_, line_id, line_chars) in enumerate(ordered):
            current = []
            for item in line_chars:
                if item["digit"]:
                    current.append(item)
                    continue
                if item["char"].isalnum():
                    if current:
                        fragments.append((pos, line_id, current))
                        current = []
            if current:
                fragments.append((pos, line_id, current))

        # Merge line fragments only when the break is formatting.
        merged = []
        i = 0
        while i < len(fragments):
            pos, line_id, current = fragments[i]
            items = current[:]
            j = i + 1
            last_pos = pos
            while j < len(fragments):
                npos, nline, nxt = fragments[j]
                if npos != last_pos + 1:
                    break
                prev_line = ordered[last_pos][2]
                next_line = ordered[npos][2]
                if not prev_line or not next_line:
                    break
                if not (
                    (not prev_line[-1]["digit"] and not prev_line[-1]["char"].isalnum())
                    or (not next_line[0]["digit"] and not next_line[0]["char"].isalnum())
                ):
                    break
                if len(items) + len(nxt) > 15:
                    break
                items.extend(nxt)
                last_pos = npos
                j += 1
            merged.append(items)
            i = max(i + 1, j)

        # Exact target matching.  Each merged candidate is checked only
        # against the requested numbers, not every possible phone length.
        for items in merged:
            digits = "".join(x["digit"] for x in items)
            if digits in target_map:
                found.append((items, target_map[digits]))

    return found


def prepare_digit_replacement(page, char, new_digit, font_file):
    x0, y0, x1, y1 = char["bbox"]
    page.add_redact_annot(
        fitz.Rect(x0, y0, x1, y1),
        fill=False,
        cross_out=False
    )
    return {
        "origin": tuple(char["origin"]),
        "size": char["size"],
        "color": char["color"],
        "font_file": font_file,
        "digit": new_digit
    }


def insert_fast(page, item, font_alias):
    color_int = item["color"]
    color = (
        ((color_int >> 16) & 255) / 255,
        ((color_int >> 8) & 255) / 255,
        (color_int & 255) / 255
    )
    kwargs = {
        "fontsize": item["size"],
        "color": color,
        "overlay": True
    }
    if font_alias:
        kwargs["fontname"] = font_alias
    else:
        kwargs["fontname"] = "helv"
    page.insert_text(item["origin"], item["digit"], **kwargs)


# -------------------------------------------------
# REPLACE
# -------------------------------------------------

@app.post("/replace")
def replace():
    font_cache = {}
    temp_files = []
    page_font_aliases = {}

    try:
        body = request.get_json(force=True)
        pdf_bytes = clean_base64(body.get("data"))
        replacements = body.get("replacements", [])

        replacement_map = {}
        for item in replacements:
            old = digits_only(item.get("search"))
            new = digits_only(item.get("replacement"))
            if old and new and len(old) == len(new):
                replacement_map[old] = new

        if not replacement_map:
            return jsonify({"error": "No valid replacements."}), 400

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        changed = 0

        for page_index in range(len(doc)):
            page = doc[page_index]

            # Very cheap text pre-check before rawdict extraction.
            text = page.get_text("text")
            text_digits = digits_only(text)
            if not text_digits or not any(x in text_digits for x in replacement_map):
                continue

            occurrences = find_target_numbers(page, replacement_map)
            if not occurrences:
                continue

            pending = []
            aliases = page_font_aliases.setdefault(page_index, {})

            for chars, new_digits in occurrences:
                digit_index = 0
                for char in chars:
                    if not char["digit"]:
                        continue

                    font_file = get_font_file(
                        doc, page, char["font"] or "", font_cache
                    )
                    pending.append(
                        prepare_digit_replacement(
                            page,
                            char,
                            new_digits[digit_index],
                            font_file
                        )
                    )
                    changed += 1
                    digit_index += 1

            page.apply_redactions(images=0, graphics=0, text=0)

            # Register each extracted font only once per page. Using the
            # registered alias avoids passing a font file on every glyph.
            for item in pending:
                font_file = item["font_file"]
                alias = None
                if font_file:
                    alias = aliases.get(font_file)
                    if alias is None and font_file not in aliases:
                        alias = "N" + str(len(aliases))
                        try:
                            page.insert_font(
                                fontname=alias,
                                fontfile=font_file
                            )
                            aliases[font_file] = alias
                        except Exception:
                            aliases[font_file] = None
                            alias = None

                insert_fast(page, item, alias)

        output = io.BytesIO()
        doc.save(output, garbage=0, deflate=True, clean=False)
        doc.close()
        output.seek(0)

        encoded = base64.b64encode(output.read()).decode("ascii")
        return jsonify({
            "success": True,
            "changedCharacters": changed,
            "fileName": make_output_name(body.get("fileName")),
            "data": "data:application/pdf;base64," + encoded
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
