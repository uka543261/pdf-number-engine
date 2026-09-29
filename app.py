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

# Punctuation/separators allowed inside a phone number.
SEPARATORS = set(" +-()./#@_|\\*[]{}:'\",;")

FONT_CACHE = {}


def normalize_digits(value):
    return re.sub(r"\D", "", value or "")


def country_from_digits(digits):
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


# ---------------------------------------------------------
# ANALYZE
# ---------------------------------------------------------

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

        return {
            "numbers": list(found.values()),
            "totalPages": len(doc)
        }

    finally:
        doc.close()


# ---------------------------------------------------------
# FAST PAGE FILTER
# ---------------------------------------------------------

def page_has_target(page, targets):
    text = page.get_text(
        "text",
        sort=True
    )

    digits = normalize_digits(text)

    for target in targets:
        if target in digits:
            return True

    return False


# ---------------------------------------------------------
# FONT
# ---------------------------------------------------------

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

            font_name_out = extracted[0]
            extension = extracted[1] or "ttf"
            content = extracted[3]

            if not content:
                continue

            safe_name = re.sub(
                r"[^A-Za-z0-9_-]",
                "_",
                font_name_out or "font"
            )

            path = os.path.join(
                tempfile.gettempdir(),
                "pdf_font_" +
                str(xref) +
                "_" +
                safe_name +
                "." +
                extension
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


# ---------------------------------------------------------
# COLOR
# ---------------------------------------------------------

def color_from_int(value):
    try:
        value = int(value or 0)

        r = ((value >> 16) & 255) / 255.0
        g = ((value >> 8) & 255) / 255.0
        b = (value & 255) / 255.0

        return (r, g, b)

    except Exception:
        return (0, 0, 0)


# ---------------------------------------------------------
# FIND PHONE OCCURRENCES
#
# IMPORTANT:
# We create DIGIT RUNS.
#
# Example:
#
# +52-800-461-1544
#
# becomes:
#
# 52 | 800 | 461 | 1544
#
# Punctuation is never included in redaction.
# ---------------------------------------------------------

def find_phone_occurrences(page, targets):

    raw = page.get_text(
        "rawdict",
        sort=True
    )

    occurrences = []

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
            # Build digit sequence while remembering positions.
            # -------------------------------------------------

            digit_positions = []
            digit_string = ""

            broken = False

            for index, item in enumerate(chars):

                c = item["c"]

                if c.isdigit():

                    digit_positions.append(index)
                    digit_string += c

                elif c in SEPARATORS or c.isspace():

                    # Allowed inside number.
                    continue

                else:

                    # Letters / unrelated characters break sequence.
                    broken = True
                    break

            if broken:
                continue

            if not digit_string:
                continue

            # -------------------------------------------------
            # Find every target in this line.
            # -------------------------------------------------

            for target in targets:

                start = 0

                while True:

                    position = digit_string.find(
                        target,
                        start
                    )

                    if position < 0:
                        break

                    end = position + len(target)

                    selected_positions = digit_positions[
                        position:end
                    ]

                    if len(selected_positions) != len(target):
                        start = position + 1
                        continue

                    selected_chars = [
                        chars[p]
                        for p in selected_positions
                    ]

                    # -------------------------------------------------
                    # Build digit RUNS.
                    #
                    # 528004611544
                    #
                    # becomes:
                    #
                    # 52
                    # 800
                    # 461
                    # 1544
                    # -------------------------------------------------

                    runs = []

                    current_run = []

                    previous_position = None

                    for char_info, original_pos in zip(
                        selected_chars,
                        selected_positions
                    ):

                        if previous_position is None:
                            current_run = [
                                char_info
                            ]

                        elif original_pos == previous_position + 1:
                            current_run.append(
                                char_info
                            )

                        else:
                            if current_run:
                                runs.append(
                                    current_run
                                )

                            current_run = [
                                char_info
                            ]

                        previous_position = original_pos

                    if current_run:
                        runs.append(
                            current_run
                        )

                    occurrences.append({
                        "old": target,
                        "runs": runs
                    })

                    start = position + len(target)

    return occurrences


# ---------------------------------------------------------
# APPLY FAST REPLACEMENT
# ---------------------------------------------------------

def replace_on_pages(doc, replacement_map):

    targets = list(
        replacement_map.keys()
    )

    pages_processed = 0
    occurrences_replaced = 0
    runs_replaced = 0

    for page_number in range(len(doc)):

        page = doc[page_number]

        # Very cheap first filter.
        if not page_has_target(
            page,
            targets
        ):
            continue

        occurrences = find_phone_occurrences(
            page,
            targets
        )

        if not occurrences:
            continue

        page_redactions = []
        page_insertions = []

        for occurrence in occurrences:

            old = occurrence["old"]
            new = replacement_map.get(old)

            if not new:
                continue

            # Exact digit count is mandatory.
            if len(old) != len(new):
                continue

            runs = occurrence["runs"]

            # Safety check.
            total_digits = sum(
                len(run)
                for run in runs
            )

            if total_digits != len(old):
                continue

            # -------------------------------------------------
            # Each digit run gets ONE redaction rectangle
            # and ONE text insertion.
            # -------------------------------------------------

            digit_offset = 0

            for run in runs:

                if not run:
                    continue

                run_digits = len(run)

                replacement_run = new[
                    digit_offset:
                    digit_offset + run_digits
                ]

                digit_offset += run_digits

                first_bbox = run[0]["bbox"]
                last_bbox = run[-1]["bbox"]

                if not first_bbox or not last_bbox:
                    continue

                # Rectangle covering ONLY this digit run.
                x0 = min(
                    c["bbox"][0]
                    for c in run
                    if c.get("bbox")
                )

                y0 = min(
                    c["bbox"][1]
                    for c in run
                    if c.get("bbox")
                )

                x1 = max(
                    c["bbox"][2]
                    for c in run
                    if c.get("bbox")
                )

                y1 = max(
                    c["bbox"][3]
                    for c in run
                    if c.get("bbox")
                )

                rect = fitz.Rect(
                    x0,
                    y0,
                    x1,
                    y1
                )

                page_redactions.append(
                    rect
                )

                first = run[0]

                page_insertions.append({
                    "text": replacement_run,
                    "origin": first["origin"],
                    "font": first["font"],
                    "size": first["size"],
                    "color": first["color"]
                })

                runs_replaced += 1

            occurrences_replaced += 1

        if not page_redactions:
            continue

        pages_processed += 1

        # -----------------------------------------------------
        # Add ALL redactions first.
        # -----------------------------------------------------

        for rect in page_redactions:

            page.add_redact_annot(
                rect,
                fill=False,
                cross_out=False
            )

        # -----------------------------------------------------
        # ONE redaction pass per page.
        # -----------------------------------------------------

        page.apply_redactions(
            images=0,
            graphics=0,
            text=0
        )

        # -----------------------------------------------------
        # Reinsert replacement RUNS.
        # -----------------------------------------------------

        for item in page_insertions:

            font_file = get_font_file(
                doc,
                page,
                item["font"]
            )

            inserted = False

            if font_file:

                try:

                    page.insert_text(
                        item["origin"],
                        item["text"],
                        fontfile=font_file,
                        fontsize=item["size"],
                        color=color_from_int(
                            item["color"]
                        ),
                        overlay=True
                    )

                    inserted = True

                except Exception:
                    inserted = False

            if not inserted:

                try:

                    page.insert_text(
                        item["origin"],
                        item["text"],
                        fontname="helv",
                        fontsize=item["size"],
                        color=color_from_int(
                            item["color"]
                        ),
                        overlay=True
                    )

                except Exception:
                    pass

    return (
        pages_processed,
        occurrences_replaced,
        runs_replaced
    )


# ---------------------------------------------------------
# HEALTH
# ---------------------------------------------------------

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "PDF Number Engine running"
    })


# ---------------------------------------------------------
# ANALYZE API
# ---------------------------------------------------------

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

        result = analyze_pdf(
            pdf_bytes
        )

        return jsonify(result)

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


# ---------------------------------------------------------
# REPLACE API
# ---------------------------------------------------------

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

        replacements = (
            data.get("replacements")
            or []
        )

        replacement_map = {}

        for item in replacements:

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

            # Same number of digits only.
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
        # OPEN PDF ONCE.
        # -------------------------------------------------

        doc = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        total_pages = len(doc)

        (
            pages_processed,
            occurrences_replaced,
            runs_replaced
        ) = replace_on_pages(
            doc,
            replacement_map
        )

        # -------------------------------------------------
        # FAST SAVE.
        # -------------------------------------------------

        output = io.BytesIO()

        doc.save(
            output,
            garbage=1,
            deflate=True
        )

        doc.close()

        output.seek(0)

        result_bytes = output.read()

        encoded = base64.b64encode(
            result_bytes
        ).decode("ascii")

        original_name = (
            data.get(
                "fileName",
                "uploaded.pdf"
            )
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
                occurrences_replaced,

            "digitRunsReplaced":
                runs_replaced

        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


# ---------------------------------------------------------
# LOCAL SERVER
# ---------------------------------------------------------

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
