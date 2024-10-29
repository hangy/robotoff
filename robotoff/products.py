import abc
import datetime
import enum
import functools
import gzip
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Iterator, Optional, Union

import duckdb
import requests
from huggingface_hub import HfApi
from pymongo import MongoClient

from robotoff import settings
from robotoff.types import JSONType, ProductIdentifier, ServerType
from robotoff.utils import get_logger, gzip_jsonl_iter, http_session, jsonl_iter

logger = get_logger(__name__)

MONGO_SELECTION_TIMEOUT_MS = 10_0000


@functools.cache
def get_mongo_client() -> MongoClient:
    return MongoClient(
        settings.MONGO_URI, serverSelectionTimeoutMS=MONGO_SELECTION_TIMEOUT_MS
    )


def get_image_id(image_path: str) -> Optional[str]:
    """Return the image ID from an image path.

    :param image_path: the image path (ex: /322/247/762/7888/2.jpg)
    :return: the image ID ("2" in the previous example) or None if the image
    is not "raw" (not digit-numbered)
    """
    image_id = Path(image_path).stem

    if image_id.isdigit():
        return image_id

    return None


def is_valid_image(images: JSONType, image_path: str) -> bool:
    image_id = Path(image_path).stem
    if not image_id.isdigit():
        return False

    return image_id in images


def is_nutrition_image(
    images: JSONType, image_path: str, lang: Optional[str] = None
) -> bool:
    return is_special_image(images, image_path, "nutrition", lang)


def has_nutrition_image(images: JSONType, lang: Optional[str] = None) -> bool:
    return has_special_image(images, "nutrition", lang)


def has_special_image(images: JSONType, key: str, lang: Optional[str] = None) -> bool:
    field_name = "{}_".format(key) if lang is None else "{}_{}".format(key, lang)
    for image_key in images:
        if image_key.startswith(field_name):
            return True

    return False


def is_special_image(
    images: JSONType, image_path: str, image_type: str, lang: Optional[str] = None
) -> bool:
    if not is_valid_image(images, image_path):
        return False

    image_id = Path(image_path).stem

    for image_key, image_data in images.items():
        if (
            image_key.startswith(image_type)
            and str(image_data.get("imgid")) == image_id
        ):
            if lang is None:
                return True

            elif image_key.endswith("_{}".format(lang)):
                return True

    return False


def minify_product_dataset(dataset_path: Path, output_path: Path):
    if dataset_path.suffix == ".gz":
        jsonl_iter_func = gzip_jsonl_iter
    else:
        jsonl_iter_func = jsonl_iter

    with gzip.open(output_path, "wt", encoding="utf-8") as output_:
        for item in jsonl_iter_func(dataset_path):
            available_fields = Product.get_fields()

            minified_item = dict(
                (
                    (field, value)
                    for (field, value) in item.items()
                    if field in available_fields
                )
            )
            output_.write(json.dumps(minified_item) + "\n")


def get_product_dataset_etag() -> Optional[str]:
    if not settings.JSONL_DATASET_ETAG_PATH.is_file():
        return None

    with open(settings.JSONL_DATASET_ETAG_PATH, "r") as f:
        return f.readline()


def save_product_dataset_etag(etag: str):
    with open(settings.JSONL_DATASET_ETAG_PATH, "w") as f:
        return f.write(etag)


def fetch_dataset(minify: bool = True) -> bool:
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_dir = Path(tmp_dir)
        output_path = output_dir / "products.jsonl.gz"
        etag = download_dataset(output_path)

        logger.info("Checking dataset file integrity")
        if not is_valid_dataset(output_path):
            return False

        if minify:
            minify_path = output_dir / "products-min.jsonl.gz"
            logger.info("Minifying product JSONL")
            minify_product_dataset(output_path, minify_path)

        logger.info("Moving file(s) to dataset directory")
        shutil.copy(str(output_path), settings.JSONL_DATASET_PATH)

        if minify:
            shutil.copy(str(minify_path), settings.JSONL_MIN_DATASET_PATH)

        save_product_dataset_etag(etag)
        logger.info("Dataset fetched")
        return True


def has_dataset_changed() -> bool:
    etag = get_product_dataset_etag()

    if etag is not None:
        r = requests.head(settings.JSONL_DATASET_URL)

        current_etag = r.headers.get("ETag", "").strip("'\"")

        if current_etag == etag:
            logger.info("Dataset ETag has not changed")
            return False

    return True


def download_dataset(output_path: os.PathLike) -> str:
    r = http_session.get(settings.JSONL_DATASET_URL, stream=True)
    current_etag = r.headers.get("ETag", "").strip("'\"")

    logger.info("Dataset has changed, downloading file")
    logger.debug("Saving temporary file in %s", output_path)

    with open(output_path, "wb") as f:
        shutil.copyfileobj(r.raw, f)

    return current_etag


def is_valid_dataset(dataset_path: Path) -> bool:
    """Check the dataset integrity: readable end to end and with a minimum
    number of products.

    This is used to spot corrupted archive files."""
    dataset = ProductDataset(dataset_path)
    try:
        count = dataset.count()
    except Exception as e:
        logger.error("Exception raised during dataset iteration", exc_info=e)
        return False

    if count < settings.DATASET_CHECK_MIN_PRODUCT_COUNT:
        logger.error(
            "Dataset has %s products, less than minimum of %s products",
            count,
            settings.DATASET_CHECK_MIN_PRODUCT_COUNT,
        )
        return False

    return True


class ComparisonOperator(enum.Enum):
    eq = 1
    gt = 2
    geq = 3
    lt = 4
    leq = 5

    @classmethod
    def get_from_string(cls, value: str):
        for operator in cls:
            if operator.name == value:
                return operator

        raise ValueError("unknown operator: {}".format(value))


def apply_comparison_operator(
    value_1, value_2, comparison_operator: ComparisonOperator
) -> bool:
    try:
        if comparison_operator == ComparisonOperator.eq:
            return value_1 == value_2

        elif comparison_operator == ComparisonOperator.gt:
            return value_1 > value_2

        elif comparison_operator == ComparisonOperator.geq:
            return value_1 >= value_2

        elif comparison_operator == ComparisonOperator.lt:
            return value_1 < value_2

        else:
            return value_1 <= value_2
    except TypeError:
        return False


class ProductStream:
    """Starting for a stream of dict representing products,
    provides a stream with filter methods to narrow down data.
    """

    def __init__(self, iterator: Iterable[JSONType]):
        self.iterator: Iterable[JSONType] = iterator

    def __iter__(self) -> Iterator[JSONType]:
        yield from self.iterator

    def filter_by_country_tag(self, country_tag: str) -> "ProductStream":
        filtered = (
            product
            for product in self.iterator
            if country_tag in (product.get("countries_tags") or [])
        )
        return ProductStream(filtered)

    def filter_by_state_tag(self, state_tag: str) -> "ProductStream":
        filtered = (
            product
            for product in self.iterator
            if state_tag in (product.get("states_tags") or [])
        )
        return ProductStream(filtered)

    def filter_text_field(self, field: str, value: str):
        filtered = (
            product for product in self.iterator if product.get(field, "") == value
        )
        return ProductStream(filtered)

    def filter_number_field(
        self,
        field: str,
        ref: Union[int, float],
        default: Union[int, float],
        operator: str = "eq",
    ) -> "ProductStream":
        operator_ = ComparisonOperator.get_from_string(operator)
        filtered = (
            product
            for product in self.iterator
            if apply_comparison_operator(product.get(field, default), ref, operator_)
        )
        return ProductStream(filtered)

    def filter_nonempty_text_field(self, field: str) -> "ProductStream":
        filtered = (
            product for product in self.iterator if (product.get(field) or "") != ""
        )
        return ProductStream(filtered)

    def filter_empty_text_field(self, field: str) -> "ProductStream":
        filtered = (
            product for product in self.iterator if not (product.get(field) or "") != ""
        )
        return ProductStream(filtered)

    def filter_nonempty_tag_field(self, field: str) -> "ProductStream":
        filtered = (product for product in self.iterator if (product.get(field) or []))
        return ProductStream(filtered)

    def filter_empty_tag_field(self, field: str) -> "ProductStream":
        filtered = (
            product for product in self.iterator if not (product.get(field) or [])
        )
        return ProductStream(filtered)

    def filter_by_modified_datetime(
        self,
        from_t: Optional[datetime.datetime] = None,
        to_t: Optional[datetime.datetime] = None,
    ):
        if from_t is None and to_t is None:
            raise ValueError("one of `from_t` or `to_t` must be provided")

        if from_t:
            from_timestamp = from_t.timestamp()
            filtered = (
                product
                for product in self.iterator
                if "last_modified_t" in product
                and product["last_modified_t"] >= from_timestamp
            )

        elif to_t:
            to_timestamp = to_t.timestamp()
            filtered = (
                product
                for product in self.iterator
                if "last_modified_t" in product
                and product["last_modified_t"] <= to_timestamp
            )

        return ProductStream(filtered)

    def take(self, count: int):
        for i, item in enumerate(self):
            if i >= count:
                break

            yield item

    def iter(self) -> Iterable[JSONType]:
        return iter(self)

    def iter_product(
        self, projection: Optional[list[str]] = None
    ) -> Iterable["Product"]:
        for item in self:
            projected_item = item
            if projection:
                projected_item = {k: item[k] for k in projection if k in item}
                if projection is not None and "image_ids" in projection:
                    # image_ids field is infered from `images` field, and
                    # `images` is not necessarily in projection, so compute it
                    # here
                    projected_item["image_ids"] = list(
                        key
                        for key in (item.get("images") or {}).keys()
                        if key.isdigit()
                    )
            yield Product(projected_item)

    def collect(self) -> list[JSONType]:
        return list(self)


class ProductDataset:
    """Handles the iteration over products dataset
    contained in an eventually gziped file with one json by line.
    """

    def __init__(self, jsonl_path):
        self.jsonl_path = jsonl_path

    def stream(self) -> ProductStream:
        if str(self.jsonl_path).endswith(".gz"):
            iterator = gzip_jsonl_iter(self.jsonl_path)
        else:
            iterator = jsonl_iter(self.jsonl_path)

        return ProductStream(iterator)

    def count(self) -> int:
        count = 0
        for _ in self.stream():
            count += 1

        return count

    @classmethod
    def load(cls):
        return cls(settings.JSONL_DATASET_PATH)


class Product:
    """Product class."""

    __slots__ = (
        "barcode",
        "countries_tags",
        "categories_tags",
        "emb_codes_tags",
        "labels_tags",
        "quantity",
        "expiration_date",
        "brands_tags",
        "stores_tags",
        "unique_scans_n",
        "images",
        "image_ids",
        "packagings",
        "lang",
        "ingredients_text",
        "nutriments",
    )

    def __init__(self, product: JSONType):
        self.barcode: Optional[str] = product.get("code")
        self.countries_tags: list[str] = product.get("countries_tags") or []
        self.categories_tags: list[str] = product.get("categories_tags") or []
        self.emb_codes_tags: list[str] = product.get("emb_codes_tags") or []
        self.labels_tags: list[str] = product.get("labels_tags") or []
        self.quantity: Optional[str] = product.get("quantity") or None
        self.expiration_date: Optional[str] = product.get("expiration_date") or None
        self.brands_tags: list[str] = product.get("brands_tags") or []
        self.stores_tags: list[str] = product.get("stores_tags") or []
        self.packagings: list = product.get("packagings") or []
        self.unique_scans_n: int = product.get("unique_scans_n") or 0
        self.images: JSONType = product.get("images") or {}
        # list of raw image IDs
        self.image_ids: list[str] = (
            product["image_ids"]
            if "image_ids" in product
            else list(key for key in self.images.keys() if key.isdigit())
        )
        self.lang: Optional[str] = product.get("lang")
        self.ingredients_text: Optional[str] = product.get("ingredients_text")
        self.nutriments: JSONType = product.get("nutriments") or {}

    @staticmethod
    def get_fields():
        return {
            "code",
            "countries_tags",
            "categories_tags",
            "emb_codes_tags",
            "labels_tags",
            "quantity",
            "expiration_date",
            "brands_tags",
            "stores_tags",
            "unique_scans_n",
            "images",
            "lang",
            "ingredients_text",
            "nutriments",
        }


class ProductStore(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def __len__(self):
        pass

    @abc.abstractmethod
    def __getitem__(self, item):
        pass


class MemoryProductStore(ProductStore):
    def __init__(self, store: dict[str, Product]):
        self.store: dict[str, Product] = store

    def __len__(self):
        return len(self.store)

    @classmethod
    def load_from_path(cls, path: Path, projection: Optional[list[str]] = None):
        logger.info("Loading product store")

        if projection is not None and "code" not in projection:
            raise ValueError("at least `code` must be in projection")

        ds = ProductDataset(path)
        stream = ds.stream()

        store: dict[str, Product] = {}

        for product in stream.iter_product(projection):
            if product.barcode:
                store[product.barcode] = product

        return cls(store)

    @classmethod
    def load_min(cls, projection: Optional[list[str]] = None) -> "MemoryProductStore":
        return cls.load_from_path(settings.JSONL_MIN_DATASET_PATH, projection)

    @classmethod
    def load_full(cls) -> "MemoryProductStore":
        return cls.load_from_path(settings.JSONL_DATASET_PATH)

    def __getitem__(self, item) -> Optional[Product]:
        return self.store.get(item)

    def __iter__(self) -> Iterator[Product]:
        return iter(self.store.values())


class DBProductStore(ProductStore):
    def __init__(self, server_type: ServerType, client: MongoClient):
        self.client = client
        self.server_type = server_type
        db_name = server_type.value
        self.db = self.client[db_name]
        self.collection = self.db.products

    def __len__(self):
        return len(self.collection.estimated_document_count())

    def get_product(
        self, product_id: ProductIdentifier, projection: Optional[list[str]] = None
    ) -> Optional[JSONType]:
        if not settings.ENABLE_MONGODB_ACCESS:
            # if `ENABLE_MONGODB_ACCESS=False`, we don't disable MongoDB
            # access and return None
            return None
        # We use `_id` instead of `code` field, as `_id` contains org ID +
        # barcode for pro platform, which is also the case for
        # `product_id.barcode`
        return self.collection.find_one({"_id": product_id.barcode}, projection)

    def __getitem__(self, product_id: ProductIdentifier) -> Optional[Product]:
        product = self.get_product(product_id)

        if product:
            return Product(product)

        return None

    def iter_product(self, projection: Optional[list[str]] = None):
        if self.collection is not None:
            yield from (Product(p) for p in self.collection.find(projection=projection))


def get_min_product_store(projection: Optional[list[str]] = None) -> MemoryProductStore:
    logger.info("Loading product store in memory...")
    ps = MemoryProductStore.load_min(projection)
    logger.info("product store loaded (%s items)", len(ps))
    return ps


def get_product_store(server_type: ServerType) -> DBProductStore:
    return DBProductStore(server_type, client=get_mongo_client())


def get_product(
    product_id: ProductIdentifier, projection: Optional[list[str]] = None
) -> Optional[JSONType]:
    """Get product from MongoDB.

    :param product_id: identifier of the product to fetch
    :param projection: list of fields to retrieve, if not provided all fields
    are queried
    :return: the product as a dict or None if it was not found
    """
    return get_product_store(product_id.server_type).get_product(product_id, projection)


def convert_jsonl_to_parquet(
    output_file_path: str,
    dataset_path: Path = settings.JSONL_DATASET_PATH,
    query_path: Path = settings.JSONL_TO_PARQUET_SQL_QUERY,
) -> None:
    logger.info("Start JSONL to Parquet conversion process.")
    if not dataset_path.exists() or not query_path.exists():
        raise FileNotFoundError(
            f"{str(dataset_path)} or {str(query_path)} was not found."
        )
    query = (
        query_path.read_text()
        .replace("{dataset_path}", str(dataset_path))
        .replace("{output_path}", output_file_path)
    )
    try:
        duckdb.sql(query)
    except duckdb.Error as e:
        logger.error(f"Error executing query: {query}\nError message: {e}")
        raise
    logger.info("JSONL successfully converted into Parquet file.")


def push_data_to_hf(
    data_path: str,
    repo_id: str = "openfoodfacts/product-database",
    revision: str = "main",
    commit_message: str = "Database updated",
) -> None:
    logger.info(f"Start pushing data to Hugging Face at {repo_id}")
    if not os.path.exists(data_path):
        raise FileNotFoundError(f"Data is missing: {data_path}")
    if os.path.splitext(data_path)[-1] != ".parquet":
        raise ValueError(
            f"A parquet file is expected. Got {os.path.splitext(data_path)[-1]} instead."
        )
    # We use the HF_Hub api since it gives us way more flexibility than push_to_hub()
    HfApi().upload_file(
        path_or_fileobj=data_path,
        repo_id=repo_id,
        revision=revision,
        repo_type="dataset",
        path_in_repo="products.parquet",
        commit_message=commit_message,
    )
    logger.info(f"Data succesfully pushed to Hugging Face at {repo_id}")
