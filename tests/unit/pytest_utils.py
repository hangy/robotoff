import json

import pytest
from diskcache import Cache
from openfoodfacts import OCRResult

from robotoff import settings
from robotoff.utils.download import AssetLoadingException, cache_asset_from_url

test_cache = Cache(settings.TESTS_DISKCACHE_DIR)


def get_asset(asset_path: str) -> bytes | None:
    """Get an asset from the test-data repository.

    The asset is cached locally.

    :param asset_path: the full asset path (starting with /)
    :return: the asset content (as bytes) or None if the asset cannot be
        fetched
    """
    asset_url = f"https://raw.githubusercontent.com/openfoodfacts/test-data/refs/heads/main{asset_path}"
    try:
        return cache_asset_from_url(
            key=asset_url, cache=test_cache, tag="test", asset_url=asset_url
        )
    except AssetLoadingException as e:
        pytest.skip(f"Cannot download asset {asset_path}: {e}")


def get_ocr_result_asset(asset_path: str) -> OCRResult | None:
    """Get an OCRResult from the test-data repository.

    :param asset_path: the full asset path (starting with /)
    :return: the OCRResult or None if the asset cannot be fetched
    """
    r_bytes = get_asset(asset_path)
    assert r_bytes is not None
    ocr_json = json.loads(r_bytes)
    return OCRResult.from_json(ocr_json)
