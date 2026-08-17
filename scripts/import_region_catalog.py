from __future__ import annotations

from scripts.import_jeju_catalog import (
    MAX_MENU_PRICE_AMOUNT,
    REGION_DATABASES,
    RegionImportError,
    RegionImportSummary,
    import_region_catalog,
    load_region_sources,
    menu_category_id_for_region,
    menu_item_id_for_region,
    normalize_region,
    region_main,
    restaurant_handle_for_region,
    restaurant_id_for_region,
    restaurant_slug_for_region,
    validate_region_environment,
)

__all__ = [
    "MAX_MENU_PRICE_AMOUNT",
    "REGION_DATABASES",
    "RegionImportError",
    "RegionImportSummary",
    "import_region_catalog",
    "load_region_sources",
    "menu_category_id_for_region",
    "menu_item_id_for_region",
    "normalize_region",
    "restaurant_handle_for_region",
    "restaurant_id_for_region",
    "restaurant_slug_for_region",
    "validate_region_environment",
]


if __name__ == "__main__":
    raise SystemExit(region_main())
