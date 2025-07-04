import datetime
import functools
import re
from typing import Union

from openfoodfacts.ocr import (
    OCRField,
    OCRRegex,
    OCRResult,
    get_match_bounding_box,
    get_text,
)

from robotoff.types import JSONType, Prediction, PredictionType

# Increase version ID when introducing breaking change: changes for which we
# want old predictions to be removed in DB and replaced by newer ones
PREDICTOR_VERSION = "1"


def process_full_digits_expiration_date(match, short: bool) -> datetime.date | None:
    day, month, year = match.group(1, 2, 3)

    if short:
        format_str: str = "%d/%m/%y"
    else:
        format_str = "%d/%m/%Y"

    try:
        date = datetime.datetime.strptime(
            "{}/{}/{}".format(day, month, year), format_str
        ).date()
    except ValueError:
        return None

    return date


EXPIRATION_DATE_REGEX: dict[str, OCRRegex] = {
    "full_digits_short": OCRRegex(
        re.compile(r"(?<!\d)(\d{2})[-./](\d{2})[-./](\d{2})(?!\d)"),
        field=OCRField.full_text,
        processing_func=functools.partial(
            process_full_digits_expiration_date, short=True
        ),
    ),
    "full_digits_long": OCRRegex(
        re.compile(r"(?<!\d)(\d{2})[-./](\d{2})[-./](\d{4})(?!\d)"),
        field=OCRField.full_text,
        processing_func=functools.partial(
            process_full_digits_expiration_date, short=False
        ),
    ),
}


def find_expiration_date(content: Union[OCRResult, str]) -> list[Prediction]:
    # Parse expiration date
    #        "À consommer de préférence avant",
    results: list[Prediction] = []

    for type_, ocr_regex in EXPIRATION_DATE_REGEX.items():
        text = get_text(content, ocr_regex)

        if not text:
            continue

        for match in ocr_regex.regex.finditer(text):
            raw = match.group(0)

            if not ocr_regex.processing_func:
                continue

            date = ocr_regex.processing_func(match)

            if date is None:
                continue

            if date.year > 2025 or date.year < 2015:
                continue

            # Format dates according to ISO 8601
            value = date.strftime("%Y-%m-%d")

            data: JSONType = {"raw": raw, "type": type_}
            if (
                bounding_box := get_match_bounding_box(
                    content, match.start(), match.end()
                )
            ) is not None:
                data["bounding_box_absolute"] = bounding_box
            results.append(
                Prediction(
                    value=value,
                    type=PredictionType.expiration_date,
                    data=data,
                    automatic_processing=True,
                    predictor="regex",
                    predictor_version=PREDICTOR_VERSION,
                )
            )

    return results
