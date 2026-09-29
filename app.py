def detect_numbers(page):
    """
    Detect phone numbers by their DIGITS.

    Any non-digit character between digits is treated
    as formatting and does not break the number.

    Examples:
        +52 — 800 — 461—1544
        +52 (800) 4611544
        +52/800)-(461)#@1544
        +52 ✦ 800 • 461 ~ 1544

    All become:
        528004611544
    """

    chars = page_characters(page)

    # Put characters into visual lines.
    lines = {}

    for item in chars:

        x0, y0, x1, y1 = item["bbox"]

        line_key = int(
            round(y0 / 3.0)
        )

        lines.setdefault(
            line_key,
            []
        ).append(item)

    results = []

    for line_chars in lines.values():

        line_chars.sort(
            key=lambda x: x["bbox"][0]
        )

        current = []
        digit_count = 0

        for item in line_chars:

            # -----------------------------------------
            # DIGIT
            # -----------------------------------------

            if item["digit"]:

                current.append(item)
                digit_count += 1

                # Phone numbers in this tool are
                # limited to 8-15 digits.
                if digit_count == 15:

                    results.append(
                        current[:]
                    )

                    current = []
                    digit_count = 0

                continue

            # -----------------------------------------
            # NON-DIGIT
            # -----------------------------------------
            #
            # IMPORTANT:
            # Do NOT check for "-", "—", "/", "#",
            # "@", brackets, etc.
            #
            # ANY non-digit character is simply
            # formatting.
            #
            # We keep the current number alive.
            # -----------------------------------------

            if current:
                continue

        # -----------------------------------------
        # END OF LINE
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
