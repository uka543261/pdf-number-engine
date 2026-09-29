from flask import Flask, request, jsonify, send_file
import fitz
import base64
import io
import re
import os
import tempfile

app = Flask(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Phone-like sequences:
PHONE_REGEX = re.compile(
    r"(?:\+?\d(?:[^A-Za-z0-9\r\n]{0,8}\d){7,14})"
)

# Characters allowed between phone digits.
ALLOWED_SEPARATORS = set(" +-()./#@_|\\*[]{}:'\",;")

# Cache extracted fonts during the request.
FONT_CACHE = {}


def normalize_digits(value):
    return re.sub(r"\D", "", value or "")


def country_from_digits(digits):
    """
    Basic country detection.
    Mexico +52 is handled explicitly.
    """
    if digits.startswith("52"):
        return "Mexico"

    if digits.startswith("1"):
        return "United States / Canada"

    if digits.startswith("44"):
        return "United Kingdom"

    if digits.startswith("91"):
        return "India"

    if digits.startswith("971"):
        return "United Arab Emirates"

    if digits.startswith("92"):
        return "Pakistan"

    if digits.startswith("61"):
        return "Australia"

    if digits.startswith("49"):
        return "Germany"

    if digits.startswith("33"):
        return "France"

    if digits.startswith("39"):
        return "Italy"

    if digits.startswith("81"):
        return "Japan"

    if digits.startswith("86"):
        return "China"

    return "Unknown"


def analyze_pdf(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    found = {}

    for page in doc:
        text = page.get_text("text", sort=True)

        for match in PHONE_REGEX.finditer(text):
            raw = match.group(0)
            digits = normalize_digits(raw)

            if len(digits) < 8 or len(digits) > 15:
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

    total_pages = len(doc)
    doc.close()

    return {
        "numbers": list(found.values()),
        "totalPages": total_pages
    }


def page_contains_target(page, targets):
    """
    Fast check before expensive rawdict processing.
    """
    text = page.get_text("text", sort=True)
    digits = normalize_digits(text)

    for target in targets:
        if target in digits:
            return True

    return False


def get_font_file(doc, page, font_name):
    """
    Extract and cache embedded font for accurate replacement.
    """
    if not font_name:
        return None

    if font_name in FONT_CACHE:
        return FONT_CACHE[font_name]

    try:
        fonts = page.get_fonts(full=True)

        for font in fonts:
            xref = font[0]
            basefont = font[3] or ""
            short_name = font[4] or ""

            if (
                font_name == basefont
                or font_name == short_name
                or font_name in basefont
                or font_name in short_name
            ):
                extracted = doc.extract_font(xref)

                if not extracted:
                    continue

                # PyMuPDF returns:
                # name, extension, type, content
                name = extracted[0]
                ext = extracted[1]
                content = extracted[3]

                if not content:
                    continue

                safe_ext = ext or "ttf"

                path = os.path.join(
                    tempfile.gettempdir(),
                    "pdf_replace_" +
                    str(xref) +
                    "_" +
                    re.sub(r"[^A-Za-z0-9]", "_", name or "font") +
                    "." +
                    safe_ext
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


def find_number_characters(page, targets):
    """
    Locate exact digit characters for target numbers.

    Returns:
        [
            {
                "old": "528004611544",
                "chars": [
                    {
                        "char": "5",
                        "bbox": (...),
                        "origin": (...),
                        "font": "...",
                        "size": ...
                    }
                ]
            }
        ]
    """

    raw = page.get_text("rawdict", sort=True)

    results = []

    # Each line is processed separately first.
    # This avoids accidentally matching unrelated numbers.
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            chars = []

            for span in line.get("spans", []):
                span_font = span.get("font")
                span_size = span.get("size", 10)

                for ch in span.get("chars", []):
                    value = ch.get("c", "")

                    chars.append({
                        "c": value,
                        "bbox": ch.get("bbox"),
                        "origin": ch.get("origin"),
                        "font": span_font,
                        "size": span_size,
                        "flags": span.get("flags", 0),
                        "color": span.get("color", 0)
                    })

            if not chars:
                continue

            # Search inside this line.
            for target in targets:
                target_len = len(target)

                digit_positions = []
                digit_string = ""

                for i, item in enumerate(chars):
                    c = item["c"]

                    if c.isdigit():
                        digit_positions.append(i)
                        digit_string += c

                    elif c in ALLOWED_SEPARATORS or c.isspace():
                        continue

                    else:
                        # Letters / unrelated symbols break a number.
                        digit_positions = []
                        digit_string = ""

                if len(digit_string) < target_len:
                    continue

                # Find target inside the normalized digit sequence.
                start = 0

                while True:
                    pos = digit_string.find(target, start)

                    if pos == -1:
                        break

                    selected_positions = digit_positions[
                        pos:pos + target_len
                    ]

                    selected_chars = [
                        chars[p] for p in selected_positions
                    ]

                    if len(selected_chars) == target_len:
                        results.append({
                            "old": target,
                            "chars": selected_chars
                        })

                    start = pos + 1

    return results


def apply_character_replacements(doc, replacement_map):
    """
    Character-level replacement.

    Old digits are redacted individually.
    New digits are inserted at the exact original digit origins.
    Punctuation / spaces / brackets are untouched.
    """

    targets = list(replacement_map.keys())

    pages_processed = 0
    total_replaced = 0

    for page in doc:
        # FAST page-level filter.
        if not page_contains_target(page, targets):
            continue

        matches = find_number_characters(page, targets)

        if not matches:
            continue

        pages_processed += 1

        redactions = []
        insertions = []

        for match in matches:
            old = match["old"]
            new = replacement_map.get(old)

            if not new:
                continue

            # Safety: only same digit count.
            if len(old) != len(new):
                continue

            chars = match["chars"]

            if len(chars) != len(old):
                continue

            for index, new_digit in enumerate(new):
                char_info = chars[index]

                bbox = char_info.get("bbox")
                origin = char_info.get("origin")

                if not bbox or not origin:
                    continue

                # Tiny padding only around the actual digit.
                rect = fitz.Rect(bbox)

                # Avoid touching neighboring punctuation.
                redactions.append(rect)

                insertions.append({
                    "digit": new_digit,
                    "origin": origin,
                    "font": char_info.get("font"),
                    "size": char_info.get("size", 10),
                    "color": char_info.get("color", 0)
                })

            total_replaced += 1

        if not redactions:
            continue

        # IMPORTANT:
        # Apply ALL digit redactions on this page at once.
        for rect in redactions:
            page.add_redact_annot(
                rect,
                fill=False,
                cross_out=False
            )

        # Text-only redaction.
        # Images and graphics remain untouched.
        page.apply_redactions(
            images=0,
            graphics=0,
            text=0
        )

        # Reinsert replacement digits.
        for item in insertions:
            font_file = get_font_file(
                doc,
                page,
                item["font"]
            )

            try:
                if font_file:
                    page.insert_text(
                        item["origin"],
                        item["digit"],
                        fontfile=font_file,
                        fontsize=item["size"],
                        color=_color_from_int(item["color"]),
                        overlay=True
                    )
                else:
                    page.insert_text(
                        item["origin"],
                        item["digit"],
                        fontname="helv",
                        fontsize=item["size"],
                        color=_color_from_int(item["color"]),
                        overlay=True
                    )

            except Exception:
                # Fallback if original font cannot be used.
                try:
                    page.insert_text(
                        item["origin"],
                        item["digit"],
                        fontname="helv",
                        fontsize=item["size"],
                        color=_color_from_int(item["color"]),
                        overlay=True
                    )
                except Exception:
                    pass

    return pages_processed, total_replaced


def _color_from_int(value):
    """
    Convert PDF integer color to RGB floats.
    """
    try:
        value = int(value or 0)

        r = ((value >> 16) & 255) / 255.0
        g = ((value >> 8) & 255) / 255.0
        b = (value & 255) / 255.0

        return (r, g, b)

    except Exception:
        return (0, 0, 0)


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "PDF Number Engine running"
    })


@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        data = request.get_json(force=True)

        if not data or not data.get("data"):
            return jsonify({
                "error": "PDF data missing."
            }), 400

        pdf_bytes = base64.b64decode(data["data"])

        if len(pdf_bytes) > MAX_UPLOAD_BYTES:
            return jsonify({
                "error": "PDF too large. Maximum 25 MB."
            }), 400

        result = analyze_pdf(pdf_bytes)

        return jsonify(result)

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500


@app.route("/replace", methods=["POST"])
def replace():
    global FONT_CACHE

    try:
        FONT_CACHE = {}

        data = request.get_json(force=True)

        if not data or not data.get("data"):
            return jsonify({
                "error": "PDF data missing."
            }), 400

        pdf_bytes = base64.b64decode(data["data"])

        if len(pdf_bytes) > MAX_UPLOAD_BYTES:
            return jsonify({
                "error": "PDF too large. Maximum 25 MB."
            }), 400

        replacements = data.get("replacements") or []

        replacement_map = {}

        for item in replacements:
            old = normalize_digits(
                item.get("old") or
                item.get("digits") or
                item.get("original") or ""
            )

            new = normalize_digits(
                item.get("new") or
                item.get("replacement") or
                item.get("replacementDigits") or ""
            )

            if not old or not new:
                continue

            # Exact digit count is mandatory.
            if len(old) != len(new):
                continue

            if old == new:
                continue

            replacement_map[old] = new

        if not replacement_map:
            return jsonify({
                "error": "No valid replacements supplied."
            }), 400

        doc = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        total_pages = len(doc)

        pages_processed, total_replaced = (
            apply_character_replacements(
                doc,
                replacement_map
            )
        )

        # FAST save.
        #
        # Do NOT use garbage=4 / clean=True here.
        # Those options can make a large PDF save unnecessarily slow.
        output = io.BytesIO()

        doc.save(
            output,
            garbage=1,
            deflate=True
        )

        doc.close()

        output.seek(0)

        output_bytes = output.read()

        encoded = base64.b64encode(
            output_bytes
        ).decode("ascii")

        return jsonify({
            "success": True,
            "data": encoded,
            "fileName": (
                data.get("fileName", "uploaded.pdf")
                .rsplit(".", 1)[0]
                + "_replaced.pdf"
            ),
            "pagesProcessed": pages_processed,
            "totalPages": total_pages,
            "numbersReplaced": total_replaced
        })

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500


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
