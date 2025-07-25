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

from robotoff import settings
from robotoff.types import Prediction, PredictionType
from robotoff.utils import text_file_iter
from robotoff.utils.cache import function_cache_register

# Increase version ID when introducing breaking change: changes for which we
# want old predictions to be removed in DB and replaced by newer ones
PREDICTOR_VERSION = "1"


def get_store_tag(store: str) -> str:
    return store.lower().replace(" & ", "-").replace(" ", "-").replace("'", "-")


def store_sort_key(item):
    """Sorting function for STORE_DATA items.
    For the regex to work correctly, we want the longest store names to
    appear first.
    """
    store, _ = item

    return -len(store), store


@functools.cache
def get_sorted_stores() -> list[tuple[str, str]]:
    sorted_stores: dict[str, str] = {}

    for item in text_file_iter(settings.OCR_STORES_DATA_PATH):
        if "||" in item:
            store, regex_str = item.split("||")
        else:
            store = item
            regex_str = re.escape(item.lower())

        sorted_stores[store] = regex_str

    return sorted(sorted_stores.items(), key=store_sort_key)


@functools.cache
def get_store_ocr_regex() -> OCRRegex:
    sorted_stores = get_sorted_stores()
    store_regex_str = "|".join(
        r"((?<!\w){}(?!\w))".format(pattern) for _, pattern in sorted_stores
    )
    return OCRRegex(
        re.compile(store_regex_str, re.I), field=OCRField.full_text_contiguous
    )


def find_stores(content: Union[OCRResult, str]) -> list[Prediction]:
    results = []
    store_ocr_regex = get_store_ocr_regex()
    sorted_stores = get_sorted_stores()
    text = get_text(content, store_ocr_regex)

    if not text:
        return []

    for match in store_ocr_regex.regex.finditer(text):
        groups = match.groups()

        for idx, match_str in enumerate(groups):
            if match_str is not None:
                store, _ = sorted_stores[idx]
                data = {"text": match_str}
                if (
                    bounding_box := get_match_bounding_box(
                        content, match.start(), match.end()
                    )
                ) is not None:
                    data["bounding_box_absolute"] = bounding_box

                results.append(
                    Prediction(
                        type=PredictionType.store,
                        value=store,
                        value_tag=get_store_tag(store),
                        data=data,
                        predictor="regex",
                        predictor_version=PREDICTOR_VERSION,
                    )
                )
                break

    return results


function_cache_register.register(get_sorted_stores)
function_cache_register.register(get_store_ocr_regex)
