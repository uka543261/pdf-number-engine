from flask import Flask, request, jsonify
import fitz
import base64
import io
import os
import re
import tempfile

app = Flask(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024

PHONE_REGEX = re.compile(
    r"(?:\+?\d(?:[^A-Za-z0-9\r\n]{0,8}\d){7,14})"
)

SEPARATORS = set(" +-()./#@_|\\*[]{}:'\",;")

FONT_CACHE = {}


def normalize_digits(value):
    return re.sub(r"\D", "", value or "")


def country_from_digits(digits):
    countries = {
        "52": "Mexico",
        "1": "United States / Canada",
        "44": "United Kingdom",
        "91": "India",
        "971": "United Arab Emirates",
        "92": "Pakistan",
        "61": "Australia",
        "49": "Germany",
        "33": "France",
        "39": "Italy",
        "81": "Japan",
        "86": "China",
    }

    for prefix, country in countries.items():
        if digits.startswith(prefix):
            return country

    return "Unknown"


# =========================================================
# ANALYZE
# =========================================================

def analyze_pdf(pdf_bytes):

    doc = fitz.open(
        stream=pdf_bytes,
        filetype="pdf"
    )

    found = {}

    try:

        for page in doc:

            text = page.get_text(
                "text",
                sort=True
            )

            for match in PHONE_REGEX.finditer(text):

                raw = match.group(0)

                digits = normalize_digits(raw)

                if not 8 <= len(digits) <= 15:
                    continue

                if digits not in found:

                    found[digits] = {
                        "digits": digits,
                        "country": country_from_digits(digits),
                        "count": 0,
                        "formats": []
                    }

                found[digits]["count"] += 1

                if raw not in found[digits]["formats"]:
                    found[digits]["formats"].append(raw)

        return {
            "numbers": list(found.values()),
            "totalPages": len(doc)
        }

    finally:
        doc.close()


# =========================================================
# FAST PAGE FILTER
# =========================================================

def page_has_target(page, targets):

    text = page.get_text(
        "text",
        sort=True
    )

    digits = normalize_digits(text)

    return any(
        target in digits
        for target in targets
    )


# =========================================================
# FONT CACHE
# =========================================================

def get_font_file(doc, page, font_name):

    if not font_name:
        return None

    if font_name in FONT_CACHE:
        return FONT_CACHE[font_name]

    try:

        for font in page.get_fonts(full=True):

            xref = font[0]
            basefont = font[3] or ""
            short_name = font[4] or ""

            if not (
                font_name == basefont
                or font_name == short_name
                or font_name in basefont
                or font_name in short_name
            ):
                continue

            extracted = doc.extract_font(xref)

            if not extracted:
                continue

            name = extracted[0] or "font"
            ext = extracted[1] or "ttf"
            content = extracted[3]

            if not content:
                continue

            safe = re.sub(
                r"[^A-Za-z0-9_-]",
                "_",
                name
            )

            path = os.path.join(
                tempfile.gettempdir(),
                "pdf_font_" +
                str(xref) +
                "_" +
                safe +
                "." +
                ext
            )

            if not os.path.exists(path):

                with open(path, "wb") as f:
                    f.write(content)

            FONT_CACHE[font_name] = path

            return path

    except Exception:
        pass

    FONT_CACHE[font_name] = None

    return None


def color_from_int(value):

    try:

        value = int(value or 0)

        return (
            ((value >> 16) & 255) / 255,
            ((value >> 8) & 255) / 255,
            (value & 255) / 255
        )

    except Exception:

        return (0, 0, 0)


# =========================================================
# FIND COMPLETE PHONE OCCURRENCES
#
# Example:
#
# +52-800-461-1544
#
# Old digits:
# 528004611544
#
# New digits:
# 528004611460
#
# Result:
# +52-800-461-1460
#
# The ORIGINAL separators are copied from the PDF.
# =========================================================

def find_occurrences(page, targets):

    raw = page.get_text(
        "rawdict",
        sort=True
    )

    results = []

    for block in raw.get("blocks", []):

        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):

            chars = []

            for span in line.get("spans", []):

                font = span.get("font")
                size = span.get("size", 10)
                color = span.get("color", 0)

                for ch in span.get("chars", []):

                    value = ch.get("c", "")

                    chars.append({
                        "c": value,
                        "bbox": ch.get("bbox"),
                        "origin": ch.get("origin"),
                        "font": font,
                        "size": size,
                        "color": color
                    })

            if not chars:
                continue

            # -------------------------------------------------
            # Build normalized digit stream.
            # -------------------------------------------------

            digit_positions = []
            digit_string = ""

            for i, item in enumerate(chars):

                c = item["c"]

                if c.isdigit():

                    digit_positions.append(i)
                    digit_string += c

                elif c in SEPARATORS or c.isspace():

                    continue

                else:

                    # Do not join across unrelated text.
                    digit_positions = []
                    digit_string = ""

            if not digit_string:
                continue

            # -------------------------------------------------
            # Find target.
            # -------------------------------------------------

            for target in targets:

                start = 0

                while True:

                    pos = digit_string.find(
                        target,
                        start
                    )

                    if pos == -1:
                        break

                    end = pos + len(target)

                    selected_positions = digit_positions[
                        pos:end
                    ]

                    if len(selected_positions) != len(target):

                        start = pos + 1
                        continue

                    first_pos = selected_positions[0]
                    last_pos = selected_positions[-1]

                    # Complete original visual substring.
                    selected_chars = chars[
                        first_pos:last_pos + 1
                    ]

                    # Make sure nothing unrelated is inside.
                    valid = True

                    for item in selected_chars:

                        c = item["c"]

                        if (
                            not c.isdigit()
                            and not c.isspace()
                            and c not in SEPARATORS
                        ):
                            valid = False
                            break

                    if not valid:

                        start = pos + 1
                        continue

                    # Original formatted string.
                    original_text = "".join(
                        item["c"]
                        for item in selected_chars
                    )

                    first = selected_chars[0]

                    last = selected_chars[-1]

                    # Bounding box of the COMPLETE occurrence.
                    valid_boxes = [
                        item["bbox"]
                        for item in selected_chars
                        if item.get("bbox")
                    ]

                    if not valid_boxes:

                        start = pos + 1
                        continue

                    x0 = min(
                        box[0]
                        for box in valid_boxes
                    )

                    y0 = min(
                        box[1]
                        for box in valid_boxes
                    )

                    x1 = max(
                        box[2]
                        for box in valid_boxes
                    )

                    y1 = max(
                        box[3]
                        for box in valid_boxes
                    )

                    results.append({
                        "old": target,
                        "original_text": original_text,
                        "rect": fitz.Rect(
                            x0,
                            y0,
                            x1,
                            y1
                        ),
                        "origin": first["origin"],
                        "font": first["font"],
                        "size": first["size"],
                        "color": first["color"]
                    })

                    start = pos + len(target)

    return results


# =========================================================
# REPLACE
# =========================================================

def replace_pdf(doc, replacement_map):

    targets = list(
        replacement_map.keys()
    )

    pages_processed = 0
    occurrences_replaced = 0

    for page_number in range(len(doc)):

        page = doc[page_number]

        # Very fast page filter.
        if not page_has_target(
            page,
            targets
        ):
            continue

        occurrences = find_occurrences(
            page,
            targets
        )

        if not occurrences:
            continue

        page_actions = []

        # -------------------------------------------------
        # Prepare each occurrence.
        # -------------------------------------------------

        for occurrence in occurrences:

            old = occurrence["old"]

            new_digits = replacement_map.get(old)

            if not new_digits:
                continue

            if len(old) != len(new_digits):
                continue

            original_text = occurrence[
                "original_text"
            ]

            # -------------------------------------------------
            # Preserve EVERY original non-digit character.
            #
            # Example:
            #
            # +52-800-461-1544
            #
            # becomes:
            #
            # +52-800-461-1460
            # -------------------------------------------------

            digit_index = 0
            new_text_parts = []

            for c in original_text:

                if c.isdigit():

                    if digit_index >= len(new_digits):
                        break

                    new_text_parts.append(
                        new_digits[digit_index]
                    )

                    digit_index += 1

                else:

                    new_text_parts.append(c)

            if digit_index != len(new_digits):
                continue

            new_text = "".join(
                new_text_parts
            )

            page_actions.append({
                "rect": occurrence["rect"],
                "text": new_text,
                "origin": occurrence["origin"],
                "font": occurrence["font"],
                "size": occurrence["size"],
                "color": occurrence["color"]
            })

        if not page_actions:
            continue

        pages_processed += 1

        # -------------------------------------------------
        # ONE REDACTION PER COMPLETE PHONE NUMBER.
        # -------------------------------------------------

        for action in page_actions:

            page.add_redact_annot(
                action["rect"],
                fill=False,
                cross_out=False
            )

        # ONE redaction pass for entire page.
        page.apply_redactions(
            images=0,
            graphics=0,
            text=0
        )

        # -------------------------------------------------
        # ONE INSERTION PER PHONE NUMBER.
        # -------------------------------------------------

        for action in page_actions:

            font_file = get_font_file(
                doc,
                page,
                action["font"]
            )

            inserted = False

            if font_file:

                try:

                    page.insert_text(
                        action["origin"],
                        action["text"],
                        fontfile=font_file,
                        fontsize=action["size"],
                        color=color_from_int(
                            action["color"]
                        ),
                        overlay=True
                    )

                    inserted = True

                except Exception:
                    pass

            if not inserted:

                try:

                    page.insert_text(
                        action["origin"],
                        action["text"],
                        fontname="helv",
                        fontsize=action["size"],
                        color=color_from_int(
                            action["color"]
                        ),
                        overlay=True
                    )

                except Exception:
                    pass

            occurrences_replaced += 1

    return (
        pages_processed,
        occurrences_replaced
    )


# =========================================================
# HOME
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "PDF Number Engine running"
    })


# =========================================================
# ANALYZE
# =========================================================

@app.route("/analyze", methods=["POST"])
def analyze():

    try:

        data = request.get_json(
            force=True
        )

        if not data or not data.get("data"):

            return jsonify({
                "error": "PDF data missing."
            }), 400

        pdf_bytes = base64.b64decode(
            data["data"]
        )

        if len(pdf_bytes) > MAX_UPLOAD_BYTES:

            return jsonify({
                "error": "PDF too large. Maximum 25 MB."
            }), 400

        return jsonify(
            analyze_pdf(pdf_bytes)
        )

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


# =========================================================
# REPLACE API
# =========================================================

@app.route("/replace", methods=["POST"])
def replace():

    global FONT_CACHE

    FONT_CACHE = {}

    try:

        data = request.get_json(
            force=True
        )

        if not data or not data.get("data"):

            return jsonify({
                "error": "PDF data missing."
            }), 400

        pdf_bytes = base64.b64decode(
            data["data"]
        )

        if len(pdf_bytes) > MAX_UPLOAD_BYTES:

            return jsonify({
                "error": "PDF too large. Maximum 25 MB."
            }), 400

        replacement_map = {}

        for item in (
            data.get("replacements")
            or []
        ):

            old = normalize_digits(
                item.get("old")
                or item.get("digits")
                or item.get("original")
                or ""
            )

            new = normalize_digits(
                item.get("new")
                or item.get("replacement")
                or item.get("replacementDigits")
                or ""
            )

            if not old or not new:
                continue

            if len(old) != len(new):
                continue

            if old == new:
                continue

            replacement_map[old] = new

        if not replacement_map:

            return jsonify({
                "error": "No valid replacements supplied."
            }), 400

        # -------------------------------------------------
        # OPEN ONCE
        # -------------------------------------------------

        doc = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        total_pages = len(doc)

        pages_processed, numbers_replaced = (
            replace_pdf(
                doc,
                replacement_map
            )
        )

        # -------------------------------------------------
        # FAST SAVE
        # -------------------------------------------------

        output = io.BytesIO()

        doc.save(
            output,
            garbage=0,
            deflate=True
        )

        doc.close()

        output.seek(0)

        result_bytes = output.read()

        encoded = base64.b64encode(
            result_bytes
        ).decode("ascii")

        original_name = data.get(
            "fileName",
            "uploaded.pdf"
        )

        base_name = os.path.splitext(
            original_name
        )[0]

        return jsonify({

            "success": True,

            "data": encoded,

            "fileName":
                base_name +
                "_replaced.pdf",

            "pagesProcessed":
                pages_processed,

            "totalPages":
                total_pages,

            "numbersReplaced":
                numbers_replaced

        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
