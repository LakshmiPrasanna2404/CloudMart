import base64
import hashlib
import json
import os
import re
import time

import boto3
import bcrypt
import pymysql

from botocore.config import Config


# ============================================================
# AWS CONFIGURATION
# ============================================================

aws_config = Config(
    connect_timeout=2,
    read_timeout=3,
    retries={"max_attempts": 1},
)

ssm = boto3.client(
    "ssm",
    config=aws_config,
)

lambda_client = boto3.client(
    "lambda",
    config=aws_config,
)


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

ENVIRONMENT = os.environ.get(
    "ENVIRONMENT",
    "prod",
)

ADMIN_TOKEN_PARAMETER_NAME = os.environ.get(
    "ADMIN_TOKEN_PARAMETER_NAME",
    "",
)

PRODUCTS_TOKEN_PARAMETER_NAME = os.environ.get(
    "PRODUCTS_TOKEN_PARAMETER_NAME",
    "",
)

PRODUCT_LAMBDA_NAME = os.environ.get(
    "PRODUCT_LAMBDA_NAME"
)

ORDER_LAMBDA_NAME = os.environ.get(
    "ORDER_LAMBDA_NAME"
)

DB_HOST = os.environ.get(
    "DB_HOST"
)

DB_NAME = os.environ.get(
    "DB_NAME",
    "cloudmart",
)

DB_PORT = int(
    os.environ.get(
        "DB_PORT",
        "3306",
    )
)

DB_USERNAME_PARAMETER_NAME = os.environ.get(
    "DB_USERNAME_PARAMETER_NAME",
    f"/cloudmart/{ENVIRONMENT}/db/username",
)

DB_PASSWORD_PARAMETER_NAME = os.environ.get(
    "DB_PASSWORD_PARAMETER_NAME",
    f"/cloudmart/{ENVIRONMENT}/db/password",
)


# ============================================================
# CACHE
# ============================================================

CACHE_TTL_SECONDS = 300

_token_cache = {
    "admin": {
        "value": None,
        "fetched_at": 0,
    },
    "products": {
        "value": None,
        "fetched_at": 0,
    },
}

_db_credentials_cache = {
    "username": None,
    "password": None,
    "fetched_at": 0,
}


# ============================================================
# LOGGING
# ============================================================

def log(level, message, **extra):
    print(
        json.dumps(
            {
                "level": level,
                "message": message,
                **extra,
            }
        )
    )


# ============================================================
# STANDARD RESPONSES
# ============================================================

def json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body),
    }


def unauthorized():
    return json_response(
        401,
        {
            "error": "unauthorized",
            "message": "Missing or invalid token",
        },
    )


def forbidden():
    return json_response(
        403,
        {
            "error": "forbidden",
            "message": "Token does not have permission for this operation",
        },
    )


def internal_error(message="Internal server error"):
    return json_response(
        500,
        {
            "error": "internal_error",
            "message": message,
        },
    )


def not_found():
    return json_response(
        404,
        {
            "error": "not_found",
            "message": "No matching route",
        },
    )


# ============================================================
# SSM APPLICATION TOKEN FUNCTIONS
# ============================================================

def get_application_token(role, parameter_name):
    """
    Reads only admin and products tokens from SSM.

    Customer credentials are NOT stored in SSM.
    Customer credentials are verified through RDS login_access.
    """

    if role not in {"admin", "products"}:
        raise RuntimeError(
            f"Unsupported SSM token role: {role}"
        )

    if not parameter_name:
        raise RuntimeError(
            f"SSM parameter is not configured for role: {role}"
        )

    now = time.time()

    cached = _token_cache[role]

    if (
        cached["value"] is not None
        and now - cached["fetched_at"] < CACHE_TTL_SECONDS
    ):
        return cached["value"]

    response = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True,
    )

    parameter_value = (
        response
        .get("Parameter", {})
        .get("Value")
    )

    if not parameter_value:
        raise RuntimeError(
            f"SSM parameter value is empty for role: {role}"
        )

    cached["value"] = parameter_value
    cached["fetched_at"] = now

    return parameter_value


def authenticate_application_token(incoming_token):
    """
    Authenticates admin and products application tokens.

    Customer token authentication is handled separately through RDS.
    """

    token_sources = [
        (
            "admin",
            ADMIN_TOKEN_PARAMETER_NAME,
        ),
        (
            "products",
            PRODUCTS_TOKEN_PARAMETER_NAME,
        ),
    ]

    for role, parameter_name in token_sources:
        token = get_application_token(
            role,
            parameter_name,
        )

        if incoming_token == token:
            return {
                "role": role,
                "customer_id": None,
            }

    return None


# ============================================================
# DATABASE CREDENTIAL FUNCTIONS
# ============================================================

def get_database_credentials():
    """
    Reads RDS credentials from SSM Parameter Store.
    """

    now = time.time()

    if (
        _db_credentials_cache["username"] is not None
        and _db_credentials_cache["password"] is not None
        and now - _db_credentials_cache["fetched_at"]
        < CACHE_TTL_SECONDS
    ):
        return (
            _db_credentials_cache["username"],
            _db_credentials_cache["password"],
        )

    username_response = ssm.get_parameter(
        Name=DB_USERNAME_PARAMETER_NAME,
        WithDecryption=True,
    )

    password_response = ssm.get_parameter(
        Name=DB_PASSWORD_PARAMETER_NAME,
        WithDecryption=True,
    )

    username = (
        username_response
        .get("Parameter", {})
        .get("Value")
    )

    password = (
        password_response
        .get("Parameter", {})
        .get("Value")
    )

    if not username or not password:
        raise RuntimeError(
            "Database credentials are missing from SSM"
        )

    _db_credentials_cache["username"] = username
    _db_credentials_cache["password"] = password
    _db_credentials_cache["fetched_at"] = now

    return username, password


def get_database_connection():
    """
    Creates a connection to the RDS MySQL database.
    """

    if not DB_HOST:
        raise RuntimeError(
            "DB_HOST environment variable is missing"
        )

    username, password = get_database_credentials()

    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=username,
        password=password,
        database=DB_NAME,
        connect_timeout=3,
        read_timeout=5,
        write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


# ============================================================
# CUSTOMER AUTHENTICATION
# ============================================================

def authenticate_customer(incoming_credential):
    """
    Authenticates a customer using login_access in RDS.

    The raw customer credential is never stored.

    password_lookup:
        SHA-256 hash used to locate the customer record.

    password_hash:
        Bcrypt hash used to verify the credential.
    """

    if not incoming_credential:
        return None

    lookup_hash = hashlib.sha256(
        incoming_credential.encode("utf-8")
    ).hexdigest()

    connection = None

    try:
        connection = get_database_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    login_id,
                    customer_id,
                    password_hash,
                    is_active,
                    revoked_at
                FROM login_access
                WHERE password_lookup = %s
                LIMIT 1
                """,
                (lookup_hash,),
            )

            login_record = cursor.fetchone()

        if not login_record:
            log(
                "WARN",
                "Customer credential was not found",
            )
            return None

        if not login_record["is_active"]:
            log(
                "WARN",
                "Customer login is inactive",
                login_id=login_record["login_id"],
            )
            return None

        if login_record["revoked_at"] is not None:
            log(
                "WARN",
                "Customer login has been revoked",
                login_id=login_record["login_id"],
            )
            return None

        stored_hash = login_record["password_hash"]

        if isinstance(stored_hash, str):
            stored_hash = stored_hash.encode("utf-8")

        credential_bytes = incoming_credential.encode(
            "utf-8"
        )

        is_valid = bcrypt.checkpw(
            credential_bytes,
            stored_hash,
        )

        if not is_valid:
            log(
                "WARN",
                "Customer credential verification failed",
                login_id=login_record["login_id"],
            )
            return None

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_access
                SET last_login_at = CURRENT_TIMESTAMP
                WHERE login_id = %s
                """,
                (login_record["login_id"],),
            )

        log(
            "INFO",
            "Customer authentication successful",
            customer_id=login_record["customer_id"],
        )

        return {
            "role": "customer",
            "customer_id": login_record["customer_id"],
        }

    except Exception as exc:
        log(
            "ERROR",
            "Customer authentication failed",
            error=str(exc),
        )
        raise

    finally:
        if connection:
            connection.close()


# ============================================================
# REQUEST FUNCTIONS
# ============================================================

def extract_bearer_token(event):
    headers = event.get(
        "headers",
        {},
    ) or {}

    auth_header = (
        headers.get("authorization")
        or headers.get("Authorization")
    )

    if not auth_header:
        return None

    if not auth_header.startswith("Bearer "):
        return None

    return auth_header[
        len("Bearer "):
    ].strip()


def get_request_details(event):
    request_context = event.get(
        "requestContext",
        {},
    ) or {}

    http = request_context.get(
        "http",
        {},
    ) or {}

    method = (
        http.get("method")
        or event.get("httpMethod")
        or ""
    ).upper()

    path = (
        http.get("path")
        or event.get("rawPath")
        or event.get("path")
        or "/"
    )

    return method, path


# ============================================================
# ROUTING
# ============================================================

def is_product_route(path):
    return (
        path == "/products"
        or path.startswith("/products/")
    )


def is_order_route(path):
    return (
        path == "/orders"
        or path.startswith("/orders/")
    )


def is_order_id_route(path):
    return re.match(
        r"^/orders/(\d+)$",
        path,
    ) is not None


def is_order_cancel_route(path):
    return re.match(
        r"^/orders/(\d+)/cancel$",
        path,
    ) is not None


def get_route_permission(method, path):
    """
    Returns the resource required by the request.

    Products:
        GET, POST, PUT, DELETE

    Orders:
        GET, POST, PATCH

    Cancellation endpoint:
        POST /orders/{id}/cancel
    """

    if is_product_route(path):
        if method in {
            "GET",
            "POST",
            "PUT",
            "DELETE",
        }:
            return "products"

        return None

    if path == "/orders":
        if method in {
            "GET",
            "POST",
            "PATCH",
        }:
            return "orders"

        return None

    if is_order_id_route(path):
        if method in {
            "GET",
            "PATCH",
        }:
            return "orders"

        return None

    if is_order_cancel_route(path):
        if method == "POST":
            return "orders"

        return None

    return None


def role_allows(role, resource, method):
    """
    Role-based access control.

    admin:
        Full product and order access.

    products:
        Product API access only.

    customer:
        GET products.
        GET, POST, PATCH orders.
    """

    if role == "admin":
        return resource in {
            "products",
            "orders",
        }

    if role == "products":
        return resource == "products"

    if role == "customer":
        if resource == "products":
            return method == "GET"

        if resource == "orders":
            return method in {
                "GET",
                "POST",
                "PATCH",
            }

    return False


def route_target(path):
    if is_product_route(path):
        return PRODUCT_LAMBDA_NAME

    if is_order_route(path):
        return ORDER_LAMBDA_NAME

    return None


# ============================================================
# DOWNSTREAM LAMBDA INVOCATION
# ============================================================

def invoke_downstream_lambda(
    target_function,
    event,
):
    response = lambda_client.invoke(
        FunctionName=target_function,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode("utf-8"),
    )

    payload_stream = response.get("Payload")

    if not payload_stream:
        return internal_error(
            "Empty response from downstream Lambda"
        )

    payload_bytes = payload_stream.read()

    if not payload_bytes:
        return internal_error(
            "Empty payload from downstream Lambda"
        )

    payload = json.loads(
        payload_bytes.decode("utf-8")
    )

    if isinstance(payload, dict):
        return payload

    return json_response(
        200,
        payload,
    )


# ============================================================
# MAIN HANDLER
# ============================================================

def lambda_handler(event, context):
    try:
        method, path = get_request_details(event)

        log(
            "INFO",
            "Incoming request",
            method=method,
            path=path,
        )

        incoming_credential = extract_bearer_token(
            event
        )

        if not incoming_credential:
            log(
                "WARN",
                "Request missing Authorization header",
            )
            return unauthorized()

        # ----------------------------------------------------
        # ADMIN / PRODUCTS AUTHENTICATION
        # ----------------------------------------------------

        identity = None

        try:
            identity = authenticate_application_token(
                incoming_credential
            )

        except Exception as exc:
            log(
                "ERROR",
                "Application token authentication failed",
                error=str(exc),
            )
            return internal_error(
                "Application authentication failed"
            )

        # ----------------------------------------------------
        # CUSTOMER AUTHENTICATION THROUGH RDS
        # ----------------------------------------------------

        if identity is None:
            try:
                identity = authenticate_customer(
                    incoming_credential
                )

            except Exception:
                return internal_error(
                    "Customer authentication failed"
                )

        # ----------------------------------------------------
        # INVALID CREDENTIAL
        # ----------------------------------------------------

        if identity is None:
            log(
                "WARN",
                "Request had an invalid credential",
            )
            return unauthorized()

        role = identity["role"]

        customer_id = identity.get(
            "customer_id"
        )

        log(
            "INFO",
            "Authentication successful",
            role=role,
            customer_id=customer_id,
        )

        # ----------------------------------------------------
        # AUTHORIZATION
        # ----------------------------------------------------

        permission = get_route_permission(
            method,
            path,
        )

        if permission is None:
            log(
                "WARN",
                "No matching route or method",
                method=method,
                path=path,
                role=role,
            )
            return not_found()

        if not role_allows(
            role,
            permission,
            method,
        ):
            log(
                "WARN",
                "RBAC permission denied",
                role=role,
                method=method,
                path=path,
                resource=permission,
            )
            return forbidden()

        # ----------------------------------------------------
        # ROUTING
        # ----------------------------------------------------

        target_function = route_target(path)

        if not target_function:
            return not_found()

        # ----------------------------------------------------
        # PASS IDENTITY TO DOWNSTREAM LAMBDA
        # ----------------------------------------------------

        request_context = event.setdefault(
            "requestContext",
            {},
        )

        authorizer_context = request_context.setdefault(
            "authorizer",
            {},
        )

        authorizer_context["role"] = role
        authorizer_context["customer_id"] = customer_id

        log(
            "INFO",
            "RBAC authorized, invoking downstream Lambda",
            role=role,
            customer_id=customer_id,
            method=method,
            path=path,
            target=target_function,
        )

        # ----------------------------------------------------
        # INVOKE DOWNSTREAM LAMBDA
        # ----------------------------------------------------

        try:
            return invoke_downstream_lambda(
                target_function,
                event,
            )

        except Exception as exc:
            log(
                "ERROR",
                "Failed to invoke downstream Lambda",
                error=str(exc),
                target=target_function,
            )

            return internal_error(
                "Downstream service failed"
            )

    except Exception as exc:
        log(
            "ERROR",
            "Authorizer error",
            error=str(exc),
        )

        return internal_error(
            "Authentication service error"
        )
