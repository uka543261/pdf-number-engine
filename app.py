import base64
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

    # Automatically recognize Unicode decimal digits (Nd), including
    # mathematical bold/double-struck/sans/monospace and fullwidth digits.
    try:
        value = unicodedata.digit(ch)
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
# SPECIAL TEXT DETECTION
# -------------------------------------------------

def is_special_char(ch):
    """True when the character is not an ordinary ASCII alphanumeric."""
    if not ch:
        return False
    return not ("A" <= ch <= "Z" or "a" <= ch <= "z" or "0" <= ch <= "9")


def is_unicode_styled_char(ch):
    """
    Detect Unicode characters commonly produced by fancy-text generators.

    This is intentionally based on Unicode properties/blocks rather than a
    short hard-coded list, so new variants in the same Unicode families can
    still be detected.
    """
    if not ch:
        return False

    code = ord(ch)
    category = unicodedata.category(ch)
    name = unicodedata.name(ch, "")

    # Combining marks are used heavily by glitch/Zalgo generators.
    if category.startswith("M"):
        return True

    # Non-ASCII mathematical alphanumeric symbols.
    if 0x1D400 <= code <= 0x1D7FF:
        return True

    # Enclosed alphanumerics / dingbats / superscripts / subscripts.
    if 0x2460 <= code <= 0x24FF:
        return True
    if 0x2776 <= code <= 0x2793:
        return True
    if 0x2070 <= code <= 0x209F:
        return True

    # Fullwidth forms.
    if 0xFF00 <= code <= 0xFFEF:
        return True

    # Common Unicode fancy-text alphabets and compatibility forms.
    if any(key in name for key in (
        "MATHEMATICAL",
        "FULLWIDTH",
        "CIRCLED",
        "PARENTHESIZED",
        "SQUARED",
        "NEGATIVE CIRCLED",
        "NEGATIVE SQUARED",
        "SMALL CAPITAL",
    )):
        return True

    # Non-ASCII letters/numbers are useful signals for fancy Unicode text.
    if code > 0x7F and category[0] in ("L", "N"):
        return True

    return False


def is_decorative_char(ch):
    """Unicode punctuation/symbol/emoji that may be part of fancy text."""
    if not ch:
        return False

    category = unicodedata.category(ch)
    return category[0] in ("P", "S") and ord(ch) > 0x7F


def token_is_fancy_or_special(token):
    """
    Decide whether a whitespace-delimited PDF token is worth exposing as a
    replaceable special/fancy-text item.

    Ordinary ASCII words and ordinary ASCII punctuation are ignored unless
    the token contains multiple decorative characters. Unicode fancy text,
    emoji, combining marks, fullwidth characters, mathematical alphabets,
    etc. are exposed.
    """
    if not token:
        return False

    text = "".join(x["char"] for x in token)

    if any(is_unicode_styled_char(x["char"]) for x in token):
        return True

    decorative_count = sum(
        1 for x in token if is_decorative_char(x["char"])
    )

    # Preserve the user's earlier requirement for strings such as !@#$%^&*()
    # without turning every normal sentence ending into a candidate.
    ascii_symbol_count = sum(
        1
        for x in token
        if ord(x["char"]) < 128
        and not x["char"].isalnum()
        and not x["char"].isspace()
    )

    return decorative_count >= 1 or ascii_symbol_count >= 3


def special_text_candidates(page):
    """
    Find exact whitespace-delimited PDF text tokens that contain fancy
    Unicode, combining marks, emoji/decorative symbols, or several special
    ASCII characters.

    This deliberately does not require a fixed list of LingoJam/Picsart/
    FancyText/Coddy styles. Unicode properties are used instead.
    """
    chars = page_characters(page)
    lines = {}

    for item in chars:
        key = (item["block_index"], item["line_index"])
        lines.setdefault(key, []).append(item)

    candidates = []

    for line_chars in lines.values():
        line_chars.sort(key=lambda x: x["bbox"][0])
        current = []

        def flush():
            nonlocal current
            if current:
                if token_is_fancy_or_special(current):
                    candidates.append(current[:])
                current = []

        for item in line_chars:
            ch = item["char"]

            if ch.isspace():
                flush()
            else:
                current.append(item)

        flush()

    return candidates


def special_candidate_text(candidate):
    return "".join(x["char"] for x in candidate)


def replace_exact_text_chars(page, candidate, replacement, doc, temp_files):
    """
    Remove the exact original character sequence and insert the new
    sequence at the original first-character position.

    This is separate from phone-number replacement so phone detection
    remains unchanged.
    """
    if not candidate:
        return 0

    first = candidate[0]
    font_file = get_font_file(doc, page, first["font"])

    if font_file:
        temp_files.append(font_file)

    for char in candidate:
        page.add_redact_annot(
            fitz.Rect(char["bbox"]),
            fill=False,
            cross_out=False
        )

    # The caller applies redactions before this insertion is performed.
    # Store the insertion information on the function result.
    return {
        "origin": first["origin"],
        "size": first["size"],
        "color": first["color"],
        "font": first["font"],
        "font_file": font_file,
        "text": replacement
    }


def insert_exact_text_replacement(page, item):
    origin = item["origin"]
    size = item["size"]
    color_int = item["color"]

    r = ((color_int >> 16) & 255) / 255
    g = ((color_int >> 8) & 255) / 255
    b = (color_int & 255) / 255

    kwargs = {
        "fontsize": size,
        "color": (r, g, b),
        "overlay": True
    }

    if item.get("font_file"):
        kwargs["fontfile"] = item["font_file"]
    else:
        kwargs["fontname"] = "helv"

    page.insert_text(
        origin,
        item["text"],
        **kwargs
    )

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
        special_found = {}
        special_char_found = {}

        for page_index in range(
            len(doc)
        ):

            page = doc[
                page_index
            ]

            candidates = detect_numbers(
                page
            )

            # Detect exact non-alphanumeric text tokens separately from
            # phone numbers. Nothing in the phone-number detector changes.
            for candidate in special_text_candidates(page):
                raw_special = special_candidate_text(candidate)

                if not raw_special:
                    continue

                special_found[raw_special] = (
                    special_found.get(raw_special, 0) + 1
                )

                for ch in raw_special:
                    if is_special_char(ch):
                        special_char_found[ch] = (
                            special_char_found.get(ch, 0) + 1
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

        special_strings = [
            {
                "text": text_value,
                "count": count
            }
            for text_value, count in special_found.items()
        ]

        special_strings.sort(
            key=lambda x: x["count"],
            reverse=True
        )

        special_characters = [
            {
                "text": text_value,
                "count": count
            }
            for text_value, count in special_char_found.items()
        ]

        special_characters.sort(
            key=lambda x: x["count"],
            reverse=True
        )

        return jsonify({
            "success": True,
            "numbers": numbers,
            "specialStrings": special_strings,
            "specialCharacters": special_characters
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

    origin = item[
        "origin"
    ]

    # Keep original extracted
    # font size.
    size = item[
        "size"
    ]

    color_int = item[
        "color"
    ]

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
    if font_file:

        kwargs[
            "fontfile"
        ] = font_file

    else:

        kwargs[
            "fontname"
        ] = "helv"

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

        replacements = body.get(
            "replacements",
            []
        )

        replacement_map = {}
        special_replacement_map = {}

        for item in replacements:

            search_value = str(
                item.get("search", "") or ""
            )
            replacement_value = str(
                item.get("replacement", "") or ""
            )

            item_type = str(
                item.get("type", "") or ""
            ).lower()

            # Existing phone-number payloads are canonical digits only.
            if item_type == "phone" or (
                search_value
                and search_value.isdigit()
                and not any(not c.isdigit() for c in search_value)
            ):
                old = digits_only(search_value)
                new = digits_only(replacement_value)

                if old and new:
                    replacement_map[old] = new

            else:
                # Special/text replacements are exact Unicode strings.
                # Spaces and symbols are intentionally NOT stripped.
                if search_value and replacement_value != "":
                    special_replacement_map[search_value] = replacement_value

        doc = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        changed = 0

        for page_index in range(
            len(doc)
        ):

            page = doc[
                page_index
            ]

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

            # ---------------------------------------------
            # EXACT SPECIAL/TEXT REPLACEMENT
            # ---------------------------------------------
            special_pending = []

            if special_replacement_map:
                for candidate in special_text_candidates(page):
                    raw_special = special_candidate_text(candidate)

                    if raw_special not in special_replacement_map:
                        continue

                    replacement_text = special_replacement_map[
                        raw_special
                    ]

                    item = replace_exact_text_chars(
                        page,
                        candidate,
                        replacement_text,
                        doc,
                        temp_files
                    )

                    if item:
                        special_pending.append(item)
                        changed += len(candidate)

            if pending or special_pending:

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

                for item in special_pending:

                    insert_exact_text_replacement(
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
