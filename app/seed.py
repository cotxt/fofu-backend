from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Session

from app import models
from app.security import hash_password

DEMO_USER_EMAIL = "demo@fofu.app"
DEMO_USER_PASSWORD = "fofu-demo-password"
HALMONI_QR_RAW_CODE = "halmoni-table-demo"

_NAMESPACE = uuid.UUID("95804daa-b321-4dca-b6b2-4171347d3fb9")
_BASE_TIME = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


def _id(key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, key))


DEMO_USER_ID = _id("user:mia")
OWNER_USER_ID = _id("user:halmoni-owner")
SECOND_REVIEWER_ID = _id("user:julien")
HALMONI_RESTAURANT_ID = _id("restaurant:halmonis-table")


def _merge(db: Session, value: Any) -> None:
    mapper = sqlalchemy_inspect(value).mapper
    identity_values = tuple(getattr(value, column.key) for column in mapper.primary_key)
    if any(identity is None for identity in identity_values):
        raise ValueError(f"Seed rows require explicit primary keys: {type(value).__name__}")
    identity: Any = identity_values[0] if len(identity_values) == 1 else identity_values
    if db.get(type(value), identity) is None:
        db.add(value)


def _seed_users(db: Session) -> None:
    if db.get(models.User, DEMO_USER_ID) is None:
        demo = models.User(
            id=DEMO_USER_ID,
            email=DEMO_USER_EMAIL,
            password_hash=hash_password(DEMO_USER_PASSWORD),
            display_name="Mia Laurent",
            home_country_code="FR",
            locale="fr",
            is_guest=False,
            is_active=True,
            roles=["customer"],
        )
        db.add(demo)

    if db.get(models.User, OWNER_USER_ID) is None:
        owner = models.User(
            id=OWNER_USER_ID,
            email="owner@fofu.app",
            password_hash=hash_password(DEMO_USER_PASSWORD),
            display_name="Park Sun-ja",
            home_country_code="KR",
            locale="ko",
            is_guest=False,
            is_active=True,
            roles=["owner"],
        )
        db.add(owner)

    if db.get(models.User, SECOND_REVIEWER_ID) is None:
        reviewer = models.User(
            id=SECOND_REVIEWER_ID,
            email=None,
            password_hash=None,
            display_name="Julien Moreau",
            home_country_code="FR",
            locale="fr",
            is_guest=True,
            is_active=True,
            roles=["customer"],
        )
        db.add(reviewer)
    db.flush()

    _merge(
        db,
        models.FoodPassport(
            user_id=DEMO_USER_ID,
            diet_codes=["pescatarian"],
            avoid_allergen_codes=["peanut", "milk"],
            avoid_ingredient_codes=["pork"],
            liked_ingredient_codes=["seafood", "tofu", "rice"],
            spice_tolerance=2,
            avoidance_details=[
                {"code": "peanut", "severity": "allergy", "strict": True},
                {"code": "milk", "severity": "preference", "strict": False},
            ],
            disliked_textures=["very-chewy"],
            learned_preferences={"likes": ["savory", "seafood"], "confidence": 0.75},
            version=1,
        ),
    )
    db.flush()


def _seed_taxonomy(db: Session) -> None:
    ingredients = [
        ("pork-belly", "Pork belly", "삼겹살", "🐷"),
        ("garlic", "Garlic", "마늘", "🧄"),
        ("perilla", "Perilla leaf", "깻잎", "🌿"),
        ("soybean", "Soybean paste", "된장", "🫘"),
        ("rice", "Rice", "쌀", "🍚"),
        ("spinach", "Spinach", "시금치", "🥬"),
        ("carrot", "Carrot", "당근", "🥕"),
        ("mushroom", "Mushroom", "버섯", "🍄"),
        ("rice-cake", "Rice cake", "떡", "🍥"),
        ("gochujang", "Gochujang", "고추장", "🌶️"),
        ("fish-cake", "Fish cake", "어묵", "🐟"),
        ("scallion", "Scallion", "대파", "🧅"),
        ("squid", "Squid", "오징어", "🦑"),
        ("shrimp", "Shrimp", "새우", "🦐"),
        ("egg", "Egg", "달걀", "🥚"),
        ("chive", "Chive", "부추", "🌱"),
        ("kimchi", "Kimchi", "김치", "🥬"),
        ("tofu", "Tofu", "두부", "◻️"),
        ("sweet-potato-noodle", "Sweet potato noodle", "당면", "🍜"),
        ("bell-pepper", "Bell pepper", "피망", "🫑"),
        ("sesame-oil", "Sesame oil", "참기름", "🌰"),
        ("wheat-flour", "Wheat flour", "밀가루", "🌾"),
        ("soy", "Soy", "대두", "🫘"),
        ("buckwheat", "Buckwheat", "메밀", "🌾"),
        ("cucumber", "Cucumber", "오이", "🥒"),
        ("cheese", "Cheese", "치즈", "🧀"),
        ("seaweed-roll", "Seaweed roll", "김말이", "🍙"),
        ("sweet-potato", "Sweet potato", "고구마", "🍠"),
        ("onion", "Onion", "양파", "🧅"),
        ("mackerel", "Mackerel", "고등어", "🐟"),
        ("radish", "Radish", "무", "⚪"),
        ("abalone", "Abalone", "전복", "🐚"),
        ("sesame", "Sesame", "참깨", "🌰"),
    ]
    for code, name_en, name_ko, emoji in ingredients:
        _merge(
            db,
            models.Ingredient(code=code, name_en=name_en, name_ko=name_ko, emoji=emoji),
        )

    allergens = [
        ("pork", "Pork (dietary avoidance)", "돼지고기"),
        ("sesame", "Sesame", "참깨"),
        ("soy", "Soy", "대두"),
        ("gluten", "Gluten", "글루텐"),
        ("fish", "Fish", "생선"),
        ("shellfish", "Shellfish", "갑각류"),
        ("egg", "Egg", "난류"),
        ("peanut", "Peanut", "땅콩"),
        ("tree-nut", "Tree nut", "견과류"),
        ("milk", "Milk", "우유"),
        ("buckwheat", "Buckwheat", "메밀"),
    ]
    for code, name_en, name_ko in allergens:
        _merge(db, models.Allergen(code=code, name_en=name_en, name_ko=name_ko))
    db.flush()


_RESTAURANTS: list[dict[str, Any]] = [
    {
        "key": "halmonis-table",
        "slug": "halmonis-table",
        "name_en": "Halmoni's Table",
        "name_ko": "할머니 식탁",
        "description_en": "Charcoal-grilled Korean family recipes, served warm in Hongdae.",
        "description_ko": "홍대에서 따뜻하게 내는 숯불구이와 한국 가정식입니다.",
        "handle": "@halmonis.table.seoul",
        "category": "Korean BBQ",
        "hero_style": "charcoal",
        "address_en": "12 Hongik-ro 1F, Mapo-gu, Seoul",
        "address_ko": "서울 마포구 홍익로 12, 1층 할머니식당",
        "phone": "+82-2-1234-5678",
        "latitude": 37.5547,
        "longitude": 126.9221,
        "rating": "4.8",
        "rating_count": 320,
        "owner_user_id": OWNER_USER_ID,
        "fr": (
            "La Table de Halmoni",
            "Des recettes familiales coréennes grillées au charbon, au cœur de Hongdae.",
            "12 Hongik-ro, 1er étage, Mapo-gu, Séoul",
        ),
    },
    {
        "key": "green-bowl",
        "slug": "green-bowl",
        "name_en": "Green Bowl",
        "name_ko": "그린 볼",
        "description_en": "Bright plant-based Korean bowls with seasonal produce.",
        "description_ko": "제철 채소로 만드는 산뜻한 식물성 한식 덮밥입니다.",
        "handle": "@green.bowl.seoul",
        "category": "Plant-based",
        "hero_style": "garden",
        "address_en": "41 Donggyo-ro, Mapo-gu, Seoul",
        "address_ko": "서울 마포구 동교로 41",
        "phone": "+82-2-2345-6789",
        "latitude": 37.5585,
        "longitude": 126.9205,
        "rating": "4.7",
        "rating_count": 184,
        "owner_user_id": None,
        "fr": (
            "Green Bowl",
            "Des bols coréens végétaux préparés avec des produits de saison.",
            "41 Donggyo-ro, Mapo-gu, Séoul",
        ),
    },
    {
        "key": "seoul-spice",
        "slug": "seoul-spice",
        "name_en": "Seoul Spice",
        "name_ko": "서울 스파이스",
        "description_en": "Bold Seoul street food cooked fresh for every guest.",
        "description_ko": "주문마다 갓 조리하는 매콤한 서울 길거리 음식입니다.",
        "handle": "@seoul.spice",
        "category": "Street food",
        "hero_style": "spice",
        "address_en": "77 Wausan-ro, Mapo-gu, Seoul",
        "address_ko": "서울 마포구 와우산로 77",
        "phone": "+82-2-3456-7890",
        "latitude": 37.5571,
        "longitude": 126.9275,
        "rating": "4.6",
        "rating_count": 241,
        "owner_user_id": None,
        "fr": (
            "Seoul Spice",
            "Une cuisine de rue séoulite relevée, préparée à la commande.",
            "77 Wausan-ro, Mapo-gu, Séoul",
        ),
    },
    {
        "key": "ocean-table",
        "slug": "ocean-table",
        "name_en": "Ocean Table",
        "name_ko": "오션 테이블",
        "description_en": "Coastal Korean comfort food with fresh seafood and crisp greens.",
        "description_ko": "신선한 해산물과 채소로 만드는 한국식 바다 요리입니다.",
        "handle": "@ocean.table.seoul",
        "category": "Seafood",
        "hero_style": "ocean",
        "address_en": "18 Yanghwa-ro, Mapo-gu, Seoul",
        "address_ko": "서울 마포구 양화로 18",
        "phone": "+82-2-4567-8901",
        "latitude": 37.5548,
        "longitude": 126.9260,
        "rating": "4.5",
        "rating_count": 129,
        "owner_user_id": None,
        "fr": (
            "Ocean Table",
            "Une cuisine coréenne réconfortante aux fruits de mer frais.",
            "18 Yanghwa-ro, Mapo-gu, Séoul",
        ),
    },
]


def _seed_restaurants(db: Session) -> None:
    for spec in _RESTAURANTS:
        restaurant_id = _id(f"restaurant:{spec['key']}")
        _merge(
            db,
            models.Restaurant(
                id=restaurant_id,
                slug=spec["slug"],
                owner_user_id=spec["owner_user_id"],
                name_en=spec["name_en"],
                name_ko=spec["name_ko"],
                description_en=spec["description_en"],
                description_ko=spec["description_ko"],
                handle=spec["handle"],
                category=spec["category"],
                hero_style=spec["hero_style"],
                address_en=spec["address_en"],
                address_ko=spec["address_ko"],
                phone=spec["phone"],
                latitude=spec["latitude"],
                longitude=spec["longitude"],
                currency="KRW",
                timezone_name="Asia/Seoul",
                rating_avg=Decimal(spec["rating"]),
                rating_count=spec["rating_count"],
                is_verified=True,
                is_open=True,
                is_published=True,
                menu_revision=1,
                cover_image_url=None,
                gallery=[],
            ),
        )
        fr_name, fr_description, fr_address = spec["fr"]
        _merge(
            db,
            models.RestaurantTranslation(
                restaurant_id=restaurant_id,
                locale="fr",
                name=fr_name,
                description=fr_description,
                address=fr_address,
            ),
        )
        for day in range(7):
            _merge(
                db,
                models.OpeningHour(
                    id=_id(f"hours:{spec['key']}:{day}"),
                    restaurant_id=restaurant_id,
                    day_of_week=day,
                    opens_at=time(11, 0),
                    closes_at=time(23, 0) if day >= 5 else time(22, 0),
                    is_closed=False,
                ),
            )
    _merge(
        db,
        models.RestaurantMembership(
            restaurant_id=HALMONI_RESTAURANT_ID,
            user_id=OWNER_USER_ID,
            role="owner",
            status="active",
            created_at=_BASE_TIME,
        ),
    )
    db.flush()


_CATEGORIES: list[dict[str, Any]] = [
    {
        "restaurant": "halmonis-table",
        "slug": "signature",
        "en": "Signature",
        "ko": "대표",
        "fr": "Spécialités",
        "order": 0,
    },
    {
        "restaurant": "halmonis-table",
        "slug": "stews",
        "en": "Stews",
        "ko": "찌개",
        "fr": "Ragoûts",
        "order": 1,
    },
    {
        "restaurant": "halmonis-table",
        "slug": "sides",
        "en": "Sides",
        "ko": "곁들임",
        "fr": "Accompagnements",
        "order": 2,
    },
    {
        "restaurant": "green-bowl",
        "slug": "rice-bowls",
        "en": "Rice bowls",
        "ko": "덮밥",
        "fr": "Bols de riz",
        "order": 0,
    },
    {
        "restaurant": "green-bowl",
        "slug": "noodles",
        "en": "Noodles",
        "ko": "면",
        "fr": "Nouilles",
        "order": 1,
    },
    {
        "restaurant": "seoul-spice",
        "slug": "street-food",
        "en": "Street food",
        "ko": "분식",
        "fr": "Cuisine de rue",
        "order": 0,
    },
    {
        "restaurant": "ocean-table",
        "slug": "seafood",
        "en": "Seafood",
        "ko": "해산물",
        "fr": "Fruits de mer",
        "order": 0,
    },
]


def _category_id(restaurant_key: str, slug: str) -> str:
    return _id(f"category:{restaurant_key}:{slug}")


def _seed_categories(db: Session) -> None:
    for spec in _CATEGORIES:
        category_id = _category_id(spec["restaurant"], spec["slug"])
        _merge(
            db,
            models.MenuCategory(
                id=category_id,
                restaurant_id=_id(f"restaurant:{spec['restaurant']}"),
                slug=spec["slug"],
                name_en=spec["en"],
                name_ko=spec["ko"],
                sort_order=spec["order"],
                is_active=True,
            ),
        )
        _merge(
            db,
            models.MenuCategoryTranslation(
                category_id=category_id,
                locale="fr",
                name=spec["fr"],
            ),
        )
    db.flush()


_ITEMS: list[dict[str, Any]] = [
    {
        "restaurant": "halmonis-table",
        "category": "signature",
        "slug": "samgyeopsal",
        "en": "Samgyeopsal",
        "ko": "삼겹살",
        "fr": "Poitrine de porc grillée",
        "pronunciation": "sam-gyeop-sal",
        "description_en": "Grill-your-own pork belly wrapped in lettuce and perilla.",
        "description_ko": "상추와 깻잎에 싸 먹는 두툼한 돼지 삼겹살입니다.",
        "description_fr": "Poitrine de porc à griller, servie avec laitue et feuille de périlla.",
        "price": 16_000,
        "serving": "One serving",
        "spice": 0,
        "taste": {"smoky": 0.9, "rich": 0.8, "crispy": 0.7},
        "tips": [{"title": "Wrap it", "body": "Add garlic and soybean paste to a perilla wrap."}],
        "badge": "popular",
        "ingredients": [
            ("pork-belly", True),
            ("garlic", False),
            ("perilla", False),
            ("soybean", False),
        ],
        "allergens": [("pork", "contains"), ("soy", "contains"), ("sesame", "may_contain")],
        "claims": [],
        "order": 0,
    },
    {
        "restaurant": "halmonis-table",
        "category": "stews",
        "slug": "kimchi-jjigae",
        "en": "Kimchi Jjigae",
        "ko": "김치찌개",
        "fr": "Ragoût de kimchi",
        "pronunciation": "kim-chi-jji-gae",
        "description_en": "Spicy, sour kimchi stew with pork and tofu.",
        "description_ko": "돼지고기와 두부를 넣어 얼큰하고 새콤하게 끓인 김치찌개입니다.",
        "description_fr": "Ragoût relevé et acidulé au kimchi, porc et tofu.",
        "price": 11_000,
        "serving": "One bowl",
        "spice": 3,
        "taste": {"spicy": 0.8, "sour": 0.8, "rich": 0.7},
        "tips": [{"title": "With rice", "body": "Mix a spoonful of broth into steamed rice."}],
        "badge": "spicy",
        "ingredients": [
            ("kimchi", True),
            ("pork-belly", False),
            ("tofu", False),
            ("gochujang", False),
            ("scallion", False),
        ],
        "allergens": [("pork", "contains"), ("soy", "contains"), ("fish", "may_contain")],
        "claims": [],
        "order": 0,
    },
    {
        "restaurant": "halmonis-table",
        "category": "sides",
        "slug": "japchae",
        "en": "Japchae",
        "ko": "잡채",
        "fr": "Japchae",
        "pronunciation": "jap-chae",
        "description_en": "Sweet potato noodles tossed with vegetables and sesame oil.",
        "description_ko": "채소와 참기름으로 버무린 당면 요리입니다.",
        "description_fr": "Nouilles de patate douce aux légumes et à l'huile de sésame.",
        "price": 13_000,
        "serving": "Share plate",
        "spice": 0,
        "taste": {"light": 0.7, "savory": 0.6},
        "tips": [],
        "badge": "vegetarian",
        "ingredients": [
            ("sweet-potato-noodle", True),
            ("spinach", False),
            ("bell-pepper", False),
            ("sesame-oil", False),
        ],
        "allergens": [("sesame", "contains"), ("soy", "may_contain")],
        "claims": ["vegetarian"],
        "order": 0,
    },
    {
        "restaurant": "green-bowl",
        "category": "rice-bowls",
        "slug": "vegan-bibimbap",
        "en": "Vegan Bibimbap",
        "ko": "비건 비빔밥",
        "fr": "Bibimbap végétalien",
        "pronunciation": "bi-bim-bap",
        "description_en": "Seasonal vegetables, mushroom, and rice with a plant-based sauce.",
        "description_ko": "제철 채소와 버섯, 밥을 비건 소스에 비벼 먹는 메뉴입니다.",
        "description_fr": "Riz, champignons et légumes de saison avec une sauce végétale.",
        "price": 12_000,
        "serving": "One bowl",
        "spice": 0,
        "taste": {"light": 0.8, "fresh": 0.9},
        "tips": [],
        "badge": "plant-based",
        "ingredients": [("rice", True), ("spinach", False), ("carrot", False), ("mushroom", False)],
        "allergens": [("soy", "contains"), ("sesame", "contains")],
        "claims": ["vegan", "vegetarian", "halal"],
        "order": 0,
    },
    {
        "restaurant": "seoul-spice",
        "category": "street-food",
        "slug": "tteokbokki",
        "en": "Tteokbokki",
        "ko": "떡볶이",
        "fr": "Tteokbokki",
        "pronunciation": "tteok-bok-ki",
        "description_en": "Chewy rice cakes and fish cake in a bold gochujang sauce.",
        "description_ko": "쫄깃한 떡과 어묵을 고추장 양념에 끓인 분식입니다.",
        "description_fr": "Gâteaux de riz et pâte de poisson dans une sauce au gochujang.",
        "price": 9_000,
        "serving": "One plate",
        "spice": 4,
        "taste": {"spicy": 0.95, "sweet": 0.6, "chewy": 0.9},
        "tips": [],
        "badge": "spicy",
        "ingredients": [
            ("rice-cake", True),
            ("gochujang", False),
            ("fish-cake", False),
            ("scallion", False),
        ],
        "allergens": [("gluten", "contains"), ("fish", "contains")],
        "claims": ["pescatarian"],
        "order": 0,
    },
    {
        "restaurant": "ocean-table",
        "category": "seafood",
        "slug": "haemul-pajeon",
        "en": "Haemul Pajeon",
        "ko": "해물파전",
        "fr": "Crêpe coréenne aux fruits de mer",
        "pronunciation": "hae-mul pa-jeon",
        "description_en": "Crisp scallion pancake with squid and shrimp.",
        "description_ko": "오징어와 새우, 부추를 넣어 바삭하게 부친 해물파전입니다.",
        "description_fr": "Crêpe croustillante à la ciboule, au calmar et aux crevettes.",
        "price": 18_000,
        "serving": "Share plate",
        "spice": 0,
        "taste": {"crispy": 0.9, "savory": 0.8},
        "tips": [],
        "badge": "popular",
        "ingredients": [
            ("squid", True),
            ("shrimp", True),
            ("egg", False),
            ("chive", False),
            ("wheat-flour", False),
        ],
        "allergens": [("shellfish", "contains"), ("egg", "contains"), ("gluten", "contains")],
        "claims": ["pescatarian"],
        "order": 0,
    },
    {
        "restaurant": "green-bowl",
        "category": "rice-bowls",
        "slug": "mushroom-bulgogi",
        "en": "Mushroom Bulgogi Bowl",
        "ko": "버섯 불고기 덮밥",
        "fr": "Bol de bulgogi aux champignons",
        "pronunciation": "beo-seot bul-go-gi",
        "description_en": "Soy-glazed mushrooms with crisp greens over rice.",
        "description_ko": "간장 양념 버섯과 아삭한 채소를 올린 덮밥입니다.",
        "description_fr": "Champignons laqués au soja, légumes croquants et riz.",
        "price": 13_000,
        "serving": "One bowl",
        "spice": 0,
        "taste": {"savory": 0.8, "rich": 0.6},
        "tips": [],
        "badge": "popular",
        "ingredients": [("mushroom", True), ("soy", False), ("scallion", False), ("rice", False)],
        "allergens": [("soy", "contains")],
        "claims": ["vegan", "vegetarian", "halal"],
        "order": 1,
    },
    {
        "restaurant": "green-bowl",
        "category": "noodles",
        "slug": "perilla-noodles",
        "en": "Perilla Buckwheat Noodles",
        "ko": "들기름 메밀국수",
        "fr": "Nouilles de sarrasin au périlla",
        "pronunciation": "deul-gi-reum me-mil-guk-su",
        "description_en": "Chilled buckwheat noodles with a fragrant perilla sauce.",
        "description_ko": "향긋한 들기름 소스에 비빈 차가운 메밀국수입니다.",
        "description_fr": "Nouilles froides de sarrasin, sauce parfumée au périlla.",
        "price": 11_000,
        "serving": "One bowl",
        "spice": 0,
        "taste": {"light": 0.8, "nutty": 0.8},
        "tips": [],
        "badge": "plant-based",
        "ingredients": [
            ("buckwheat", True),
            ("perilla", False),
            ("cucumber", False),
            ("sesame-oil", False),
        ],
        "allergens": [("buckwheat", "contains"), ("sesame", "contains")],
        "claims": ["vegan", "vegetarian", "halal"],
        "order": 0,
    },
    {
        "restaurant": "seoul-spice",
        "category": "street-food",
        "slug": "cheese-tteokbokki",
        "en": "Cheese Tteokbokki",
        "ko": "치즈 떡볶이",
        "fr": "Tteokbokki au fromage",
        "pronunciation": "chi-jeu tteok-bok-ki",
        "description_en": "Creamy melted cheese over spicy rice cakes.",
        "description_ko": "매콤한 떡볶이에 부드러운 치즈를 올렸습니다.",
        "description_fr": "Fromage fondu sur des gâteaux de riz épicés.",
        "price": 11_000,
        "serving": "One plate",
        "spice": 3,
        "taste": {"spicy": 0.8, "rich": 0.9},
        "tips": [],
        "badge": "popular",
        "ingredients": [
            ("rice-cake", True),
            ("cheese", False),
            ("gochujang", False),
            ("fish-cake", False),
        ],
        "allergens": [("milk", "contains"), ("fish", "contains"), ("gluten", "contains")],
        "claims": ["pescatarian"],
        "order": 1,
    },
    {
        "restaurant": "seoul-spice",
        "category": "street-food",
        "slug": "twigim-basket",
        "en": "Twigim Basket",
        "ko": "튀김 모둠",
        "fr": "Assortiment de fritures",
        "pronunciation": "twi-gim",
        "description_en": "Crisp vegetables and seaweed rolls.",
        "description_ko": "채소와 김말이를 바삭하게 튀긴 모둠입니다.",
        "description_fr": "Légumes croquants et rouleaux d'algue frits.",
        "price": 8_000,
        "serving": "Share basket",
        "spice": 0,
        "taste": {"crispy": 0.95, "light": 0.5},
        "tips": [],
        "badge": None,
        "ingredients": [
            ("seaweed-roll", True),
            ("sweet-potato", False),
            ("onion", False),
            ("wheat-flour", False),
        ],
        "allergens": [("gluten", "contains")],
        "claims": ["vegetarian"],
        "order": 2,
    },
    {
        "restaurant": "ocean-table",
        "category": "seafood",
        "slug": "grilled-mackerel",
        "en": "Grilled Mackerel",
        "ko": "고등어구이",
        "fr": "Maquereau grillé",
        "pronunciation": "go-deung-eo gu-i",
        "description_en": "Salt-grilled mackerel with rice and radish.",
        "description_ko": "소금에 구운 고등어를 밥과 무와 함께 냅니다.",
        "description_fr": "Maquereau grillé au sel, servi avec riz et radis.",
        "price": 16_000,
        "serving": "One plate",
        "spice": 0,
        "taste": {"savory": 0.9, "smoky": 0.7},
        "tips": [],
        "badge": None,
        "ingredients": [("mackerel", True), ("radish", False), ("rice", False)],
        "allergens": [("fish", "contains")],
        "claims": ["pescatarian"],
        "order": 1,
    },
    {
        "restaurant": "ocean-table",
        "category": "seafood",
        "slug": "abalone-porridge",
        "en": "Abalone Porridge",
        "ko": "전복죽",
        "fr": "Bouillie de riz à l'ormeau",
        "pronunciation": "jeon-bok-juk",
        "description_en": "Slow-cooked rice porridge with tender abalone.",
        "description_ko": "부드러운 전복과 쌀을 천천히 끓인 죽입니다.",
        "description_fr": "Bouillie de riz mijotée avec de tendres morceaux d'ormeau.",
        "price": 17_000,
        "serving": "One bowl",
        "spice": 0,
        "taste": {"light": 0.8, "savory": 0.7},
        "tips": [],
        "badge": None,
        "ingredients": [("abalone", True), ("rice", False), ("sesame", False)],
        "allergens": [("shellfish", "contains"), ("sesame", "contains")],
        "claims": ["pescatarian"],
        "order": 2,
    },
]


def _item_id(restaurant_key: str, slug: str) -> str:
    return _id(f"item:{restaurant_key}:{slug}")


def _seed_items(db: Session) -> None:
    for spec in _ITEMS:
        item_id = _item_id(spec["restaurant"], spec["slug"])
        _merge(
            db,
            models.MenuItem(
                id=item_id,
                restaurant_id=_id(f"restaurant:{spec['restaurant']}"),
                category_id=_category_id(spec["restaurant"], spec["category"]),
                slug=spec["slug"],
                name_en=spec["en"],
                name_ko=spec["ko"],
                pronunciation=spec["pronunciation"],
                description_en=spec["description_en"],
                description_ko=spec["description_ko"],
                price_amount=spec["price"],
                currency="KRW",
                serving_description=spec["serving"],
                spice_level=spec["spice"],
                taste_profile=spec["taste"],
                local_tips=spec["tips"],
                badge=spec["badge"],
                image_url=None,
                media=[],
                is_available=True,
                sort_order=spec["order"],
            ),
        )
        _merge(
            db,
            models.MenuItemTranslation(
                menu_item_id=item_id,
                locale="fr",
                name=spec["fr"],
                description=spec["description_fr"],
                pronunciation=spec["pronunciation"],
            ),
        )
    db.flush()

    for spec in _ITEMS:
        item_id = _item_id(spec["restaurant"], spec["slug"])
        for sort_order, (ingredient_code, is_primary) in enumerate(spec["ingredients"]):
            _merge(
                db,
                models.MenuItemIngredient(
                    menu_item_id=item_id,
                    ingredient_code=ingredient_code,
                    detail_en=None,
                    detail_ko=None,
                    is_primary=is_primary,
                    sort_order=sort_order,
                ),
            )
        for allergen_code, relation_type in spec["allergens"]:
            _merge(
                db,
                models.MenuItemAllergen(
                    menu_item_id=item_id,
                    allergen_code=allergen_code,
                    relation_type=relation_type,
                    verification_status="merchant_reported",
                    source="demo menu review",
                    verified_at=_BASE_TIME,
                ),
            )
        declared_allergens = {code for code, _ in spec["allergens"]}
        for allergen_code in ("peanut", "milk"):
            if allergen_code in declared_allergens:
                continue
            _merge(
                db,
                models.MenuItemAllergen(
                    menu_item_id=item_id,
                    allergen_code=allergen_code,
                    relation_type="free_from",
                    verification_status="merchant_reported",
                    source="demo menu review",
                    verified_at=_BASE_TIME,
                ),
            )
        for claim in spec["claims"]:
            _merge(
                db,
                models.MenuItemDietaryClaim(
                    menu_item_id=item_id,
                    code=claim,
                    verification_status="merchant_reported",
                ),
            )
    db.flush()


def _seed_reviews(db: Session) -> None:
    item_id = _item_id("halmonis-table", "kimchi-jjigae")
    reviews = [
        models.Review(
            id=_id("review:kimchi-jjigae:mia"),
            menu_item_id=item_id,
            author_user_id=DEMO_USER_ID,
            rating=5,
            body=(
                "Tastes like nothing we have at home — sour and deeply savory. "
                "The tofu balances the heat. Ask for extra rice!"
            ),
            author_display_name="Mia Laurent",
            author_country_code="FR",
            tags=["savory", "spicy", "traveler"],
            is_published=True,
            created_at=_BASE_TIME + timedelta(days=2),
            updated_at=_BASE_TIME + timedelta(days=2),
        ),
        models.Review(
            id=_id("review:kimchi-jjigae:julien"),
            menu_item_id=item_id,
            author_user_id=SECOND_REVIEWER_ID,
            rating=4,
            body=(
                "Careful if you don't eat pork — the staff explained the broth and offered "
                "a seafood version. They were very kind about it."
            ),
            author_display_name="Julien Moreau",
            author_country_code="FR",
            tags=["helpful-staff", "allergen-context"],
            is_published=True,
            created_at=_BASE_TIME + timedelta(days=1),
            updated_at=_BASE_TIME + timedelta(days=1),
        ),
    ]
    for review in reviews:
        _merge(db, review)
    db.flush()


_EXPLORE_VIDEOS = [
    ("ViCC3Pu2p5I", "Bibimbap", "samseats", ["trending", "rice"]),
    ("ooCdYU2E5rY", "Korean street burger", "seagull food", ["trending", "street"]),
    ("IP5rIWGcm3c", "Five ramen and one tteokbokki", "CVS & Mart Food", ["noodles", "spicy"]),
    ("eqxk2rVPbdA", "Street food recipes", "Cooking Little", ["street"]),
    ("1CTJXk4JmR8", "Six ways to upgrade instant ramen", "Budget Bytes", ["noodles"]),
    ("oHhNOceJ25A", "Rose tteokbokki", "Daily365", ["spicy", "street"]),
    ("x99ORt3WolA", "Ramen with egg and wagyu", "Lisa Nguyen", ["noodles"]),
    ("xl-PdRtSsxo", "Fire-roasted short rib ramen", "Chef Adam Glick", ["noodles"]),
    ("Kynnnq37GAM", "Gourmet burger", "samseats", ["trending"]),
    ("bTQ3bnyW4ZI", "Chicken gyros", "samseats", ["trending"]),
    ("GPz3TOiSbEk", "Lobster mac and cheese", "samseats", ["seafood"]),
    ("RDojUqxt3W8", "Butter chicken", "samseats", ["trending", "spicy"]),
]


def _seed_explore(db: Session) -> None:
    for index, (video_id, title, creator, categories) in enumerate(_EXPLORE_VIDEOS):
        created_at = _BASE_TIME - timedelta(minutes=index)
        _merge(
            db,
            models.ExploreVideo(
                id=_id(f"explore:youtube:{video_id}"),
                provider="youtube",
                provider_video_id=video_id,
                title=title,
                creator=creator,
                thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/oar2.jpg",
                categories=categories,
                is_published=True,
                created_at=created_at,
                updated_at=created_at,
            ),
        )
    db.flush()


def _seed_qr(db: Session) -> None:
    digest = hashlib.sha256(HALMONI_QR_RAW_CODE.encode("utf-8")).hexdigest()
    _merge(
        db,
        models.QRCode(
            id=_id("qr:halmonis-table:demo"),
            code_hash=digest,
            public_hint="halmoni-demo",
            restaurant_id=HALMONI_RESTAURANT_ID,
            label="Halmoni's Table demo QR",
            table_label=None,
            menu_revision=1,
            is_active=True,
            expires_at=None,
        ),
    )
    db.flush()


def _seed_conversation(db: Session) -> None:
    conversation_id = _id("conversation:halmonis-table:mia-owner")
    first_message_at = datetime(2026, 8, 4, 7, 12, tzinfo=timezone.utc)
    messages = [
        (
            "mia-question",
            DEMO_USER_ID,
            "Hi! Does the kimchi jjigae contain pork?",
            first_message_at,
        ),
        (
            "owner-answer",
            OWNER_USER_ID,
            "Hi! Our regular broth does contain pork.",
            first_message_at + timedelta(minutes=1),
        ),
        (
            "owner-alternative",
            OWNER_USER_ID,
            ("But we can prepare it with anchovy broth instead. Just let us know when you order."),
            first_message_at + timedelta(minutes=1, seconds=20),
        ),
        (
            "mia-thanks",
            DEMO_USER_ID,
            "Perfect, thank you!",
            first_message_at + timedelta(minutes=2),
        ),
    ]
    last_message_at = messages[-1][3]

    _merge(
        db,
        models.Conversation(
            id=conversation_id,
            kind="restaurant",
            restaurant_id=HALMONI_RESTAURANT_ID,
            title="Halmoni's Table",
            last_message_at=last_message_at,
            created_at=first_message_at,
            updated_at=last_message_at,
        ),
    )
    db.flush()

    for user_id, last_read_at in (
        (DEMO_USER_ID, first_message_at),
        (OWNER_USER_ID, last_message_at),
    ):
        _merge(
            db,
            models.ConversationParticipant(
                conversation_id=conversation_id,
                user_id=user_id,
                joined_at=first_message_at,
                last_read_at=last_read_at,
            ),
        )

    for key, sender_user_id, body, created_at in messages:
        _merge(
            db,
            models.Message(
                id=_id(f"message:halmonis-table:{key}"),
                conversation_id=conversation_id,
                sender_user_id=sender_user_id,
                body=body,
                client_message_id=f"seed-halmoni-{key}",
                kind="text",
                media_asset_id=None,
                created_at=created_at,
                edited_at=None,
                deleted_at=None,
            ),
        )
    db.flush()


def seed_demo_data(db: Session) -> None:
    """Idempotently install the shared iOS/web demo catalog into an empty or reused DB."""

    try:
        _seed_users(db)
        _seed_taxonomy(db)
        _seed_restaurants(db)
        _seed_categories(db)
        _seed_items(db)
        _seed_reviews(db)
        _seed_explore(db)
        _seed_qr(db)
        _seed_conversation(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
