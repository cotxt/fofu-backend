from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import API_V1, DEMO_PASSWORD
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import models
from app.database import SessionLocal
from app.security import hash_password

ADMIN_PASSWORD = "admin-test-password-with-enough-entropy"


@dataclass(frozen=True)
class ApplicationFixture:
    application_id: str
    applicant_user_id: str
    applicant_email: str
    license_media_id: str
    storage_key: str


def _new_id() -> str:
    return str(uuid.uuid4())


def _create_user(*, roles: list[str], label: str) -> tuple[str, str]:
    user_id = _new_id()
    email = f"{label}-{uuid.uuid4().hex}@example.com"
    with SessionLocal() as db:
        db.add(
            models.User(
                id=user_id,
                email=email,
                password_hash=hash_password(ADMIN_PASSWORD),
                display_name=label.replace("-", " ").title(),
                locale="en",
                is_guest=False,
                is_active=True,
                roles=roles,
            )
        )
        db.commit()
    return user_id, email


def _create_admin() -> tuple[str, str]:
    return _create_user(roles=["admin"], label="admin-test")


def _login(
    client: TestClient,
    email: str,
    *,
    password: str = ADMIN_PASSWORD,
) -> dict[str, str]:
    response = client.post(
        f"{API_V1}/auth/login",
        json={"email": email, "password": password, "client_type": "ios"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_restaurant() -> str:
    restaurant_id = _new_id()
    suffix = uuid.uuid4().hex
    with SessionLocal() as db:
        db.add(
            models.Restaurant(
                id=restaurant_id,
                slug=f"admin-review-{suffix}",
                handle=f"admin-review-{suffix}",
                name_en=f"Admin Review Restaurant {suffix[:8]}",
                category="Test",
                address_en="1 Admin Test Street",
                latitude=37.5665,
                longitude=126.9780,
                is_verified=False,
                is_published=False,
            )
        )
        db.commit()
    return restaurant_id


def _create_application(
    *,
    restaurant_id: str,
    storage_key: str | None = None,
) -> ApplicationFixture:
    applicant_user_id = _new_id()
    applicant_email = f"applicant-{uuid.uuid4().hex}@example.com"
    license_media_id = _new_id()
    application_id = _new_id()
    contents = b"%PDF-1.4\nadmin test license\n%%EOF\n"
    storage_key = storage_key or f"admin-tests/{uuid.uuid4()}.pdf"

    with SessionLocal() as db:
        applicant = models.User(
            id=applicant_user_id,
            email=applicant_email,
            password_hash=hash_password(ADMIN_PASSWORD),
            display_name="Admin Test Applicant",
            locale="en",
            is_guest=False,
            is_active=True,
            roles=["customer"],
        )
        db.add(applicant)
        db.flush()
        license_asset = models.MediaAsset(
            id=license_media_id,
            owner_user_id=applicant_user_id,
            purpose="business_license",
            storage_key=storage_key,
            original_filename="business-license.pdf",
            content_type="application/pdf",
            size_bytes=len(contents),
            sha256=hashlib.sha256(contents).hexdigest(),
            status="uploaded",
        )
        db.add(license_asset)
        db.flush()
        application = models.OwnerApplication(
            id=application_id,
            applicant_user_id=applicant_user_id,
            restaurant_id=restaurant_id,
            business_name="Admin Test Kitchen",
            registration_number=f"ADM-{uuid.uuid4().hex[:12]}",
            address="1 Admin Test Street",
            phone="+82 2-1234-5678",
            license_media_id=license_media_id,
            agreed_to_terms_at=models.utcnow(),
            terms_version="2026-08",
            status="pending",
        )
        db.add(application)
        db.commit()

    return ApplicationFixture(
        application_id=application_id,
        applicant_user_id=applicant_user_id,
        applicant_email=applicant_email,
        license_media_id=license_media_id,
        storage_key=storage_key,
    )


def _overview_counts() -> dict[str, int]:
    with SessionLocal() as db:
        return {
            "users_total": db.scalar(select(func.count()).select_from(models.User)) or 0,
            "users_active": db.scalar(
                select(func.count()).select_from(models.User).where(models.User.is_active.is_(True))
            )
            or 0,
            "restaurants_total": db.scalar(select(func.count()).select_from(models.Restaurant))
            or 0,
            "restaurants_published": db.scalar(
                select(func.count())
                .select_from(models.Restaurant)
                .where(models.Restaurant.is_published.is_(True))
            )
            or 0,
            "owner_applications_pending": db.scalar(
                select(func.count())
                .select_from(models.OwnerApplication)
                .where(models.OwnerApplication.status.in_(("pending", "submitted")))
            )
            or 0,
            "owner_applications_under_review": db.scalar(
                select(func.count())
                .select_from(models.OwnerApplication)
                .where(models.OwnerApplication.status == "under_review")
            )
            or 0,
            "audit_events_total": db.scalar(select(func.count()).select_from(models.AuditEvent))
            or 0,
        }


def test_admin_endpoints_require_current_database_admin_role(client: TestClient) -> None:
    _, admin_email = _create_admin()
    admin_headers = _login(client, admin_email)
    customer_headers = _login(client, "demo@fofu.app", password=DEMO_PASSWORD)
    owner_headers = _login(client, "owner@fofu.app", password=DEMO_PASSWORD)
    endpoints = (
        f"{API_V1}/admin/overview",
        f"{API_V1}/admin/users",
        f"{API_V1}/admin/restaurants",
        f"{API_V1}/admin/owner-applications",
        f"{API_V1}/admin/audit-events",
    )

    for endpoint in endpoints:
        unauthenticated = client.get(endpoint)
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["error"]["code"] == "authentication_required"

        for forbidden_headers in (customer_headers, owner_headers):
            forbidden = client.get(endpoint, headers=forbidden_headers)
            assert forbidden.status_code == 403
            assert forbidden.json()["error"]["code"] == "admin_access_required"

        assert client.get(endpoint, headers=admin_headers).status_code == 200


def test_admin_browser_session_is_same_origin_and_uses_an_isolated_cookie(
    client: TestClient,
) -> None:
    _, admin_email = _create_admin()
    login_url = f"{API_V1}/admin/auth/login"
    payload = {"email": admin_email, "password": ADMIN_PASSWORD}

    consumer_origin = "http://localhost:3000"
    cross_origin = client.post(
        login_url,
        headers={"Origin": consumer_origin},
        json=payload,
    )
    assert cross_origin.status_code == 403
    assert cross_origin.json()["error"]["code"] == "admin_same_origin_required"
    assert cross_origin.headers["access-control-allow-origin"] == consumer_origin

    logged_in = client.post(
        login_url,
        headers={"Origin": "http://testserver"},
        json=payload,
    )
    assert logged_in.status_code == 200
    cookie = logged_in.headers["set-cookie"].lower()
    assert "fofu_admin_refresh_token=" in cookie
    assert "path=/api/v1/admin/auth" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert logged_in.json()["scope"] == "admin"

    access_headers = {"Authorization": f"Bearer {logged_in.json()['access_token']}"}
    assert client.get(f"{API_V1}/admin/overview", headers=access_headers).status_code == 200

    blocked_refresh = client.post(
        f"{API_V1}/admin/auth/refresh",
        headers={"Origin": consumer_origin},
    )
    assert blocked_refresh.status_code == 403
    refreshed = client.post(
        f"{API_V1}/admin/auth/refresh",
        headers={"Origin": "http://testserver"},
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["scope"] == "admin"

    generic_web_login = client.post(
        f"{API_V1}/auth/login",
        json={
            "email": admin_email,
            "password": ADMIN_PASSWORD,
            "client_type": "web",
        },
    )
    assert generic_web_login.status_code == 403
    assert generic_web_login.json()["error"]["code"] == "admin_login_separated"

    customer_login = client.post(
        login_url,
        headers={"Origin": "http://testserver"},
        json={"email": "demo@fofu.app", "password": DEMO_PASSWORD},
    )
    assert customer_login.status_code == 403
    assert customer_login.json()["error"]["code"] == "admin_access_required"

    logged_out = client.post(
        f"{API_V1}/admin/auth/logout",
        headers={"Origin": "http://testserver"},
    )
    assert logged_out.status_code == 200


def test_revoking_admin_role_takes_effect_before_access_token_expires(
    client: TestClient,
) -> None:
    admin_user_id, admin_email = _create_admin()
    admin_headers = _login(client, admin_email)
    assert client.get(f"{API_V1}/admin/overview", headers=admin_headers).status_code == 200

    with SessionLocal() as db:
        admin = db.get(models.User, admin_user_id)
        assert admin is not None
        admin.roles = ["customer"]
        db.commit()

    revoked = client.get(f"{API_V1}/admin/overview", headers=admin_headers)
    assert revoked.status_code == 403
    assert revoked.json()["error"]["code"] == "admin_access_required"


def test_admin_overview_and_read_only_lists_reflect_database_state(
    client: TestClient,
) -> None:
    admin_user_id, admin_email = _create_admin()
    restaurant_id = _create_restaurant()
    application = _create_application(restaurant_id=restaurant_id)
    audit_action = f"admin.test.{uuid.uuid4().hex}"
    audit_id = _new_id()
    with SessionLocal() as db:
        db.add(
            models.AuditEvent(
                id=audit_id,
                actor_user_id=admin_user_id,
                action=audit_action,
                resource_type="admin_test",
                resource_id=application.application_id,
                details={"source": "admin API integration test"},
            )
        )
        db.commit()

    expected_overview = _overview_counts()
    headers = _login(client, admin_email)

    overview = client.get(f"{API_V1}/admin/overview", headers=headers)
    assert overview.status_code == 200
    assert overview.json() == expected_overview

    users = client.get(f"{API_V1}/admin/users", headers=headers, params={"q": admin_email})
    assert users.status_code == 200
    assert users.json()["total"] == 1
    user = users.json()["items"][0]
    assert user["id"] == admin_user_id
    assert user["email"] == admin_email
    assert "admin" in user["roles"]
    assert "password_hash" not in json.dumps(user)

    with SessionLocal() as db:
        restaurant = db.get(models.Restaurant, restaurant_id)
        assert restaurant is not None
        restaurant_query = restaurant.name_en
    restaurants = client.get(
        f"{API_V1}/admin/restaurants", headers=headers, params={"q": restaurant_query}
    )
    assert restaurants.status_code == 200
    assert restaurants.json()["total"] == 1
    assert restaurants.json()["items"][0]["id"] == restaurant_id

    applications = client.get(
        f"{API_V1}/admin/owner-applications",
        headers=headers,
        params={"status": "pending"},
    )
    assert applications.status_code == 200
    assert applications.json()["total"] >= 1
    assert any(item["id"] == application.application_id for item in applications.json()["items"])
    assert "storage_key" not in json.dumps(applications.json())

    events = client.get(f"{API_V1}/admin/audit-events", headers=headers, params={"limit": 100})
    assert events.status_code == 200
    assert events.json()["total"] >= 1
    event = next(item for item in events.json()["items"] if item["id"] == audit_id)
    assert event["action"] == audit_action
    assert event["actor_user_id"] == admin_user_id

    first_page = client.get(
        f"{API_V1}/admin/users",
        headers=headers,
        params={"limit": 1, "offset": 0},
    ).json()
    second_page = client.get(
        f"{API_V1}/admin/users",
        headers=headers,
        params={"limit": 1, "offset": 1},
    ).json()
    assert first_page["total"] == second_page["total"]
    assert first_page["items"][0]["id"] != second_page["items"][0]["id"]


def test_admin_can_moderate_restaurant_without_changing_staff_access(
    client: TestClient,
) -> None:
    admin_user_id, admin_email = _create_admin()
    owner_user_id, _ = _create_user(roles=["owner"], label="moderation-owner")
    manager_user_id, _ = _create_user(roles=["manager"], label="moderation-manager")
    restaurant_id = _create_restaurant()
    with SessionLocal() as db:
        restaurant = db.get(models.Restaurant, restaurant_id)
        assert restaurant is not None
        restaurant.owner_user_id = owner_user_id
        db.add_all(
            [
                models.RestaurantMembership(
                    restaurant_id=restaurant_id,
                    user_id=owner_user_id,
                    role="owner",
                    status="active",
                ),
                models.RestaurantMembership(
                    restaurant_id=restaurant_id,
                    user_id=manager_user_id,
                    role="manager",
                    status="active",
                ),
            ]
        )
        db.commit()

    url = f"{API_V1}/admin/restaurants/{restaurant_id}"
    headers = _login(client, admin_email)
    customer_headers = _login(client, "demo@fofu.app", password=DEMO_PASSWORD)

    assert client.patch(url, json={"is_published": True}).status_code == 401
    forbidden = client.patch(
        url,
        headers=customer_headers,
        json={"is_published": True},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "admin_access_required"

    empty = client.patch(url, headers=headers, json={})
    assert empty.status_code == 422
    explicit_null = client.patch(url, headers=headers, json={"is_published": None})
    assert explicit_null.status_code == 422
    unknown = client.patch(
        f"{API_V1}/admin/restaurants/{_new_id()}",
        headers=headers,
        json={"is_published": True},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "restaurant_not_found"

    updated = client.patch(
        url,
        headers=headers,
        json={"is_published": True, "is_verified": True, "is_open": False},
    )
    assert updated.status_code == 200
    assert updated.json()["id"] == restaurant_id
    assert updated.json()["owner_user_id"] == owner_user_id
    assert updated.json()["is_published"] is True
    assert updated.json()["is_verified"] is True
    assert updated.json()["is_open"] is False

    with SessionLocal() as db:
        restaurant = db.get(models.Restaurant, restaurant_id)
        memberships = list(
            db.scalars(
                select(models.RestaurantMembership).where(
                    models.RestaurantMembership.restaurant_id == restaurant_id
                )
            ).all()
        )
        audit_event = db.scalar(
            select(models.AuditEvent).where(
                models.AuditEvent.actor_user_id == admin_user_id,
                models.AuditEvent.action == "restaurant.moderation_updated",
                models.AuditEvent.resource_type == "restaurant",
                models.AuditEvent.resource_id == restaurant_id,
            )
        )

        assert restaurant is not None and restaurant.owner_user_id == owner_user_id
        assert {(item.user_id, item.role, item.status) for item in memberships} == {
            (owner_user_id, "owner", "active"),
            (manager_user_id, "manager", "active"),
        }
        assert audit_event is not None
        assert audit_event.details == {
            "changes": {
                "is_published": {"from": False, "to": True},
                "is_verified": {"from": False, "to": True},
                "is_open": {"from": True, "to": False},
            }
        }

    openapi = client.get(f"{API_V1}/openapi.json").json()
    assert "patch" in openapi["paths"]["/api/v1/admin/restaurants/{restaurant_id}"]
    moderation_schema = openapi["components"]["schemas"][
        "AdminRestaurantModerationUpdate"
    ]
    assert moderation_schema["minProperties"] == 1
    assert set(moderation_schema["properties"]) == {
        "is_published",
        "is_verified",
        "is_open",
    }

    # The integration test database is shared across this module/session. Restore
    # catalog visibility so unrelated public-catalog count assertions remain stable.
    with SessionLocal() as db:
        restaurant = db.get(models.Restaurant, restaurant_id)
        assert restaurant is not None
        restaurant.is_published = False
        restaurant.is_verified = False
        restaurant.is_open = True
        db.commit()


def test_admin_can_review_and_approve_application_with_existing_restaurant(
    client: TestClient,
) -> None:
    admin_user_id, admin_email = _create_admin()
    restaurant_id = _create_restaurant()
    application = _create_application(restaurant_id=restaurant_id)
    headers = _login(client, admin_email)
    review_url = f"{API_V1}/admin/owner-applications/{application.application_id}/review"

    under_review = client.patch(
        review_url,
        headers=headers,
        json={"status": "under_review", "review_note": "Documents are being checked."},
    )
    assert under_review.status_code == 200
    assert under_review.json()["status"] == "under_review"
    assert under_review.json()["reviewed_at"] is None

    unknown_restaurant = client.patch(
        review_url,
        headers=headers,
        json={"status": "approved", "restaurant_id": _new_id()},
    )
    assert unknown_restaurant.status_code == 404

    approved = client.patch(
        review_url,
        headers=headers,
        json={
            "status": "approved",
            "review_note": "Business information verified.",
        },
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["restaurant_id"] == restaurant_id
    assert approved.json()["reviewed_at"] is not None

    with SessionLocal() as db:
        stored_application = db.get(models.OwnerApplication, application.application_id)
        applicant = db.get(models.User, application.applicant_user_id)
        restaurant = db.get(models.Restaurant, restaurant_id)
        membership = db.get(
            models.RestaurantMembership,
            {
                "restaurant_id": restaurant_id,
                "user_id": application.applicant_user_id,
            },
        )
        audit_events = db.scalars(
            select(models.AuditEvent).where(
                models.AuditEvent.resource_type == "owner_application",
                models.AuditEvent.resource_id == application.application_id,
                models.AuditEvent.actor_user_id == admin_user_id,
            )
        ).all()

        assert stored_application is not None
        assert stored_application.status == "approved"
        assert stored_application.restaurant_id == restaurant_id
        assert stored_application.reviewed_at is not None
        assert applicant is not None and "owner" in applicant.roles
        assert restaurant is not None
        assert restaurant.owner_user_id == application.applicant_user_id
        assert restaurant.is_verified is True
        assert membership is not None
        assert membership.role == "owner"
        assert membership.status == "active"
        assert len(audit_events) >= 2


def test_rejection_does_not_grant_access_and_final_review_is_immutable(
    client: TestClient,
) -> None:
    admin_user_id, admin_email = _create_admin()
    restaurant_id = _create_restaurant()
    application = _create_application(restaurant_id=restaurant_id)
    headers = _login(client, admin_email)
    review_url = f"{API_V1}/admin/owner-applications/{application.application_id}/review"

    rejected = client.patch(
        review_url,
        headers=headers,
        json={"status": "rejected", "review_note": "The document could not be verified."},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["reviewed_at"] is not None

    second_review = client.patch(
        review_url,
        headers=headers,
        json={"status": "approved", "restaurant_id": restaurant_id},
    )
    assert second_review.status_code == 409

    with SessionLocal() as db:
        stored_application = db.get(models.OwnerApplication, application.application_id)
        applicant = db.get(models.User, application.applicant_user_id)
        restaurant = db.get(models.Restaurant, restaurant_id)
        membership = db.get(
            models.RestaurantMembership,
            {
                "restaurant_id": restaurant_id,
                "user_id": application.applicant_user_id,
            },
        )
        audit_event = db.scalar(
            select(models.AuditEvent).where(
                models.AuditEvent.resource_type == "owner_application",
                models.AuditEvent.resource_id == application.application_id,
                models.AuditEvent.actor_user_id == admin_user_id,
            )
        )

        assert stored_application is not None and stored_application.status == "rejected"
        assert applicant is not None and "owner" not in applicant.roles
        assert restaurant is not None
        assert restaurant.owner_user_id is None
        assert restaurant.is_verified is False
        assert membership is None
        assert audit_event is not None


def test_database_allows_only_one_active_owner_membership() -> None:
    first_user_id, _ = _create_user(roles=["owner"], label="first-owner")
    second_user_id, _ = _create_user(roles=["owner"], label="second-owner")
    restaurant_id = _create_restaurant()

    with SessionLocal() as db:
        db.add_all(
            [
                models.RestaurantMembership(
                    restaurant_id=restaurant_id,
                    user_id=first_user_id,
                    role="owner",
                    status="active",
                ),
                models.RestaurantMembership(
                    restaurant_id=restaurant_id,
                    user_id=second_user_id,
                    role="owner",
                    status="active",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_business_license_download_is_admin_only_private_and_path_contained(
    client: TestClient,
    upload_root: Path,
) -> None:
    _, admin_email = _create_admin()
    application = _create_application(restaurant_id=_create_restaurant())
    contents = b"%PDF-1.4\nadmin test license\n%%EOF\n"
    license_path = upload_root / application.storage_key
    upload_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    upload_root.chmod(0o700)
    license_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    license_path.parent.chmod(0o700)
    license_path.write_bytes(contents)
    headers = _login(client, admin_email)
    customer_headers = _login(client, "demo@fofu.app", password=DEMO_PASSWORD)
    license_url = f"{API_V1}/admin/owner-applications/{application.application_id}/license"

    unauthenticated = client.get(license_url)
    assert unauthenticated.status_code == 401
    forbidden = client.get(license_url, headers=customer_headers)
    assert forbidden.status_code == 403

    downloaded = client.get(license_url, headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == contents
    assert downloaded.headers["content-type"].startswith("application/pdf")
    assert "business-license.pdf" in downloaded.headers["content-disposition"]
    assert "no-store" in downloaded.headers["cache-control"]

    outside_filename = f"outside-admin-license-{uuid.uuid4().hex}.pdf"
    outside_path = upload_root.parent / outside_filename
    outside_path.write_bytes(b"must never be returned")
    escaping_application = _create_application(
        restaurant_id=_create_restaurant(), storage_key=f"../{outside_filename}"
    )
    escaping_url = (
        f"{API_V1}/admin/owner-applications/{escaping_application.application_id}/license"
    )
    escaped = client.get(escaping_url, headers=headers)
    assert escaped.status_code in {404, 409}
    assert escaped.content != outside_path.read_bytes()


def test_admin_web_app_uses_private_assets_and_hardened_browser_headers(
    client: TestClient,
) -> None:
    page = client.get("/admin")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "no-store" in page.headers["cache-control"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["referrer-policy"] == "no-referrer"

    content_security_policy = page.headers["content-security-policy"]
    assert "default-src 'self'" in content_security_policy
    assert "object-src 'none'" in content_security_policy
    assert "frame-ancestors 'none'" in content_security_policy
    assert "'unsafe-inline'" not in content_security_policy

    asset_urls = set(
        re.findall(r"(?:href|src)=[\"']([^\"']*/admin/assets/[^\"']+)[\"']", page.text)
    )
    assert asset_urls
    asset_bodies: list[str] = []
    for asset_url in asset_urls:
        assert asset_url.startswith("/")
        asset = client.get(asset_url)
        assert asset.status_code == 200
        assert "no-store" in asset.headers["cache-control"]
        assert asset.headers["x-content-type-options"] == "nosniff"
        asset_bodies.append(asset.text)

    browser_bundle = "\n".join([page.text, *asset_bodies])
    assert f"{API_V1}/admin/auth/login" in browser_bundle
    assert f"{API_V1}/admin/auth/refresh" in browser_bundle
    assert "mobile-logout-button" in browser_bundle
    assert "updateRestaurantModeration" in browser_bundle
    assert 'method: "PATCH"' in browser_bundle
    assert "공개 전에는 점주 승인" in browser_bundle
    assert DEMO_PASSWORD not in browser_bundle
    assert ADMIN_PASSWORD not in browser_bundle
    assert "localStorage" not in browser_bundle
    assert "sessionStorage" not in browser_bundle
