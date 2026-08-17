from app.main import app


def test_openapi_documents_runtime_error_envelope() -> None:
    schema = app.openapi()
    operations = [
        operation
        for path_item in schema["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]

    expected_schema = {"$ref": "#/components/schemas/APIErrorResponse"}
    for operation in operations:
        for status_code in ("422", "500"):
            documented = operation["responses"][status_code]["content"]["application/json"]
            assert documented["schema"] == expected_schema

    error_response = schema["components"]["schemas"]["APIErrorResponse"]
    assert set(error_response["required"]) == {"error", "request_id"}
    assert "HTTPValidationError" not in schema["components"]["schemas"]


def test_openapi_distinguishes_optional_and_required_bearer_auth() -> None:
    schema = app.openapi()
    optional_operations = [
        ("post", "/api/v1/auth/register"),
        ("post", "/api/v1/auth/google"),
        ("post", "/api/v1/auth/logout"),
        ("post", "/api/v1/guest-sessions/qr"),
        ("get", "/api/v1/restaurants"),
        ("get", "/api/v1/search"),
        ("get", "/api/v1/restaurants/{restaurant_identifier}/menu"),
        ("get", "/api/v1/restaurants/{restaurant_identifier}/menu-items/{item_identifier}"),
        ("get", "/api/v1/restaurants/{restaurant_identifier}"),
        ("get", "/api/v1/menu-items/{item_identifier}"),
    ]
    for method, path in optional_operations:
        operation = schema["paths"][path][method]
        assert operation["security"] == [{}, {"BearerAuth": []}]
        assert "x-fofu-optional-auth" not in operation

    required_operations = [
        ("get", "/api/v1/auth/me"),
        ("get", "/api/v1/cart"),
        ("get", "/api/v1/me/passport"),
        ("put", "/api/v1/push/devices/{installation_id}"),
        ("delete", "/api/v1/push/devices/{installation_id}"),
    ]
    for method, path in required_operations:
        assert schema["paths"][path][method]["security"] == [{"BearerAuth": []}]

    assert "security" not in schema["paths"]["/api/v1/auth/login"]["post"]


def test_openapi_documents_optional_google_replaced_refresh_token() -> None:
    request_schema = app.openapi()["components"]["schemas"]["GoogleLoginRequest"]

    assert "replaced_refresh_token" in request_schema["properties"]
    assert "replaced_refresh_token" not in request_schema["required"]
