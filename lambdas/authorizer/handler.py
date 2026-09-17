import base64
import hashlib
import json
import os
import re
import time

import bcrypt
import boto3
import pymysql

from botocore.config import Config


# ============================================================
# AWS CONFIGURATION
# ============================================================

aws_config = Config(
    connect_timeout=3,
    read_timeout=5,
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

ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")

ADMIN_TOKEN_PARAMETER_NAME = os.environ.get(
    "ADMIN_TOKEN_PARAMETER_NAME",
    f"/cloudmart/{ENVIRONMENT}/auth/admin-token",
)

PRODUCTS_TOKEN_PARAMETER_NAME = os.environ.get(
    "PRODUCTS_TOKEN_PARAMETER_NAME",
    f"/cloudmart/{ENVIRONMENT}/auth/products-token",
)

PRODUCT_LAMBDA_NAME = os.environ.get(
    "PRODUCT_LAMBDA_NAME",
)

ORDER_LAMBDA_NAME = os.environ.get(
    "ORDER_LAMBDA_NAME",
)

DB_HOST = os.environ.get("DB_HOST")
DB_NAME = os.environ.get("DB_NAME", "cloudmart")

DB_USERNAME_PARAMETER_NAME = os.environ.get(
    "DB_USERNAME_PARAMETER_NAME",
    f"/cloudmart/{ENVIRONMENT}/db/username",
)

DB_PASSWORD_PARAMETER_NAME = os.environ.get(
    "DB_PASSWORD_PARAMETER_NAME",
    f"/cloudmart/{ENVIRONMENT}/db/password",
)


# ============================================================
# TOKEN CACHE
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
# COMMON RESPONSES
# ============================================================

def json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body),
    }


def unauthorized(message="Missing or invalid token"):
    return json_response(
        401,
        {
            "error": "unauthorized",
            "message": message,
        },
    )


def forbidden():
    return json_response(
        403,
        {
            "error": "forbidden",
            "message": "You do not have permission to perform this operation",
        },
    )


def bad_request(message):
    return json_response(
        400,
        {
            "error": "bad_request",
            "message": message,
        },
    )


# ============================================================
# REQUEST HELPERS
# ============================================================

def get_request_details(event):
    request_context = event.get("requestContext", {}) or {}
    http = request_context.get("http", {}) or {}

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


def get_request_body(event):
    body = event.get("body")

    if not body:
        return {}

    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, dict):
        return body

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


def extract_bearer_token(event):
    headers = event.get("headers", {}) or {}

    authorization_header = (
        headers.get("authorization")
        or headers.get("Authorization")
        or ""
    )

    if not authorization_header:
        return None

    if not authorization_header.lower().startswith("bearer "):
        return None

    token = authorization_header[7:].strip()

    return token if token else None


# ============================================================
# SSM TOKEN FUNCTIONS
# ============================================================

def get_application_token(role, parameter_name):
    now = time.time()

    cached_token = _token_cache.get(role)

    if cached_token:
        cached_value = cached_token.get("value")
        fetched_at = cached_token.get("fetched_at", 0)

        if (
            cached_value is not None
            and now - fetched_at < CACHE_TTL_SECONDS
        ):
            return cached_value

    if not parameter_name:
        raise RuntimeError(
            f"SSM parameter is not configured for role: {role}"
        )

    response = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True,
    )

    parameter_value = (
        response.get("Parameter", {}).get("Value")
    )

    if not parameter_value:
        raise RuntimeError(
            f"SSM parameter value is empty for role: {role}"
        )

    _token_cache[role] = {
        "value": parameter_value,
        "fetched_at": now,
    }

    return parameter_value


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

def get_ssm_parameter(parameter_name):
    response = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True,
    )

    value = response.get("Parameter", {}).get("Value")

    if not value:
        raise RuntimeError(
            f"SSM parameter has no value: {parameter_name}"
        )

    return value


def get_database_connection():
    if not DB_HOST:
        raise RuntimeError("DB_HOST environment variable is missing")

    db_username = get_ssm_parameter(
        DB_USERNAME_PARAMETER_NAME
    )

    db_password = get_ssm_parameter(
        DB_PASSWORD_PARAMETER_NAME
    )

    return pymysql.connect(
        host=DB_HOST,
        user=db_username,
        password=db_password,
        database=DB_NAME,
        port=3306,
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


# ============================================================
# CUSTOMER AUTHENTICATION
# ============================================================

def get_customer_from_credential(incoming_credential):
    """
    Customer credentials are stored in RDS.

    The raw credential is never stored.

    password_lookup:
        SHA-256 hash of the raw customer credential.

    password_hash:
        Bcrypt hash used for verification.
    """

    if not incoming_credential:
        return None

    password_lookup = hashlib.sha256(
        incoming_credential.encode("utf-8")
    ).hexdigest()

    connection = None

    try:
        connection = get_database_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    la.login_id,
                    la.customer_id,
                    la.password_hash,
                    la.is_active,
                    c.name,
                    c.email
                FROM login_access la
                INNER JOIN customers c
                    ON c.customer_id = la.customer_id
                WHERE la.password_lookup = %s
                  AND la.is_active = TRUE
                LIMIT 1
                """,
                (password_lookup,),
            )

            customer = cursor.fetchone()

            if not customer:
                return None

            stored_hash = customer.get("password_hash")

            if not stored_hash:
                return None

            if isinstance(stored_hash, str):
                stored_hash = stored_hash.encode("utf-8")

            credential_bytes = incoming_credential.encode("utf-8")

            if not bcrypt.checkpw(
                credential_bytes,
                stored_hash,
            ):
                return None

            cursor.execute(
                """
                UPDATE login_access
                SET last_login_at = CURRENT_TIMESTAMP
                WHERE login_id = %s
                """,
                (customer["login_id"],),
            )

            connection.commit()

            return {
                "role": "customer",
                "customer_id": customer["customer_id"],
                "name": customer.get("name"),
                "email": customer.get("email"),
            }

    finally:
        if connection:
            connection.close()


def authenticate_token(incoming_token):
    """
    Authentication order:

    1. Check admin token from SSM.
    2. Check products token from SSM.
    3. Check individual customer credential from RDS.
    """

    if not incoming_token:
        return None

    try:
        admin_token = get_application_token(
            "admin",
            ADMIN_TOKEN_PARAMETER_NAME,
        )

        if incoming_token == admin_token:
            return {
                "role": "admin",
                "customer_id": None,
            }

    except Exception as error:
        log(
            "ERROR",
            "Failed to fetch admin token",
            error=str(error),
        )
        raise

    try:
        products_token = get_application_token(
            "products",
            PRODUCTS_TOKEN_PARAMETER_NAME,
        )

        if incoming_token == products_token:
            return {
                "role": "products",
                "customer_id": None,
            }

    except Exception as error:
        log(
            "ERROR",
            "Failed to fetch products token",
            error=str(error),
        )
        raise

    try:
        customer = get_customer_from_credential(
            incoming_token
        )

        if customer:
            return customer

    except Exception as error:
        log(
            "ERROR",
            "Failed to authenticate customer from RDS",
            error=str(error),
        )
        raise

    return None


# ============================================================
# CUSTOMER REGISTRATION
# ============================================================

def register_customer(event):
    body = get_request_body(event)

    name = str(body.get("name", "")).strip()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))

    if not name:
        return bad_request("Name is required")

    if not email:
        return bad_request("Email is required")

    if not password:
        return bad_request("Password is required")

    if len(password.encode("utf-8")) > 72:
        return bad_request(
            "Password must not exceed 72 bytes"
        )

    if len(password) < 8:
        return bad_request(
            "Password must contain at least 8 characters"
        )

    password_lookup = hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()

    password_hash = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")

    connection = None

    try:
        connection = get_database_connection()

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT customer_id
                FROM customers
                WHERE email = %s
                LIMIT 1
                """,
                (email,),
            )

            existing_customer = cursor.fetchone()

            if existing_customer:
                connection.rollback()

                return json_response(
                    409,
                    {
                        "error": "customer_exists",
                        "message": "Email is already registered",
                    },
                )

            cursor.execute(
                """
                INSERT INTO customers
                    (name, email)
                VALUES
                    (%s, %s)
                """,
                (name, email),
            )

            customer_id = cursor.lastrowid

            cursor.execute(
                """
                INSERT INTO login_access
                    (
                        customer_id,
                        password_lookup,
                        password_hash,
                        is_active
                    )
                VALUES
                    (%s, %s, %s, TRUE)
                """,
                (
                    customer_id,
                    password_lookup,
                    password_hash,
                ),
            )

            connection.commit()

            return json_response(
                201,
                {
                    "message": "Customer registered successfully",
                    "customer_id": customer_id,
                    "email": email,
                    "next_step": (
                        "Use the registration password as "
                        "the Bearer credential"
                    ),
                },
            )

    except pymysql.err.IntegrityError:
        if connection:
            connection.rollback()

        return json_response(
            409,
            {
                "error": "registration_failed",
                "message": "Email or credential already exists",
            },
        )

    except Exception as error:
        if connection:
            connection.rollback()

        log(
            "ERROR",
            "Customer registration failed",
            error=str(error),
        )

        return json_response(
            500,
            {
                "error": "internal_error",
                "message": "Customer registration failed",
            },
        )

    finally:
        if connection:
            connection.close()


# ============================================================
# ROUTING AND PERMISSIONS
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


def is_register_route(path):
    return path in {
        "/register",
        "/customers/register",
    }


def get_route_permission(method, path):
    if is_register_route(path):
        if method == "POST":
            return "register"

        return None

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

    if re.match(r"^/orders/\d+$", path):
        if method in {
            "GET",
            "PATCH",
        }:
            return "orders"

        return None

    if re.match(r"^/orders/\d+/cancel$", path):
        if method in {
            "POST",
            "PATCH",
        }:
            return "orders"

        return None

    return None


def role_allows(role, resource, method):
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

    return False


def get_target_lambda(path):
    if is_product_route(path):
        return PRODUCT_LAMBDA_NAME

    if is_order_route(path):
        return ORDER_LAMBDA_NAME

    return None


# ============================================================
# DOWNSTREAM LAMBDA INVOCATION
# ============================================================

def invoke_lambda(function_name, event):
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode("utf-8"),
    )

    payload = response.get("Payload")

    if not payload:
        return json_response(
            500,
            {
                "error": "internal_error",
                "message": "Empty Lambda response",
            },
        )

    raw_payload = payload.read()

    if isinstance(raw_payload, bytes):
        raw_payload = raw_payload.decode("utf-8")

    result = json.loads(raw_payload)

    return result


# ============================================================
# MAIN LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):
    method, path = get_request_details(event)

    log(
        "INFO",
        "Incoming request",
        method=method,
        path=path,
    )

    # --------------------------------------------------------
    # PUBLIC CUSTOMER REGISTRATION
    # --------------------------------------------------------

    if is_register_route(path):
        if method != "POST":
            return json_response(
                405,
                {
                    "error": "method_not_allowed",
                    "message": "Only POST is allowed for registration",
                },
            )

        return register_customer(event)

    # --------------------------------------------------------
    # AUTHENTICATION
    # --------------------------------------------------------

    incoming_token = extract_bearer_token(event)

    if not incoming_token:
        log(
            "WARN",
            "Request missing Authorization header",
        )

        return unauthorized()

    try:
        authenticated_user = authenticate_token(
            incoming_token
        )

    except Exception as error:
        log(
            "ERROR",
            "Authentication failed",
            error=str(error),
        )

        return json_response(
            500,
            {
                "error": "internal_error",
                "message": "Authentication service failed",
            },
        )

    if not authenticated_user:
        log(
            "WARN",
            "Request had an invalid token",
        )

        return unauthorized()

    role = authenticated_user.get("role")
    customer_id = authenticated_user.get("customer_id")

    # --------------------------------------------------------
    # AUTHORIZATION
    # --------------------------------------------------------

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

        return json_response(
            404,
            {
                "error": "not_found",
                "message": "No matching route",
            },
        )

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

    target_function = get_target_lambda(path)

    if not target_function:
        return json_response(
            404,
            {
                "error": "not_found",
                "message": "No target Lambda found",
            },
        )

    # --------------------------------------------------------
    # PASS AUTHENTICATION INFORMATION DOWNSTREAM
    # --------------------------------------------------------

    event.setdefault(
        "requestContext",
        {},
    )

    event["requestContext"].setdefault(
        "authorizer",
        {},
    )

    event["requestContext"]["authorizer"].update(
        {
            "role": role,
            "customer_id": customer_id,
        }
    )

    log(
        "INFO",
        "RBAC authorized, invoking downstream Lambda",
        role=role,
        customer_id=customer_id,
        method=method,
        path=path,
        target=target_function,
    )

    # --------------------------------------------------------
    # INVOKE PRODUCT OR ORDER LAMBDA
    # --------------------------------------------------------

    try:
        return invoke_lambda(
            target_function,
            event,
        )

    except Exception as error:
        log(
            "ERROR",
            "Failed to invoke downstream Lambda",
            target=target_function,
            error=str(error),
        )

        return json_response(
            500,
            {
                "error": "internal_error",
                "message": "Downstream service failed",
            },
        )
