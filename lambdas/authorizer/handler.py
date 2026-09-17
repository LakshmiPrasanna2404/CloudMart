import json
import os
import time
import hashlib
import pymysql
import bcrypt
import boto3

from botocore.config import Config


# ---------------------------------------------------------
# AWS CLIENT CONFIGURATION
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# ENVIRONMENT VARIABLES
# ---------------------------------------------------------

ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")

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


# ---------------------------------------------------------
# TOKEN CACHE
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# HTTP RESPONSES
# ---------------------------------------------------------

def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body),
    }


def unauthorized():
    return response(
        401,
        {
            "error": "unauthorized",
            "message": "Missing or invalid token",
        },
    )


def forbidden():
    return response(
        403,
        {
            "error": "forbidden",
            "message": "Token does not have permission",
        },
    )


# ---------------------------------------------------------
# REQUEST HELPERS
# ---------------------------------------------------------

def extract_bearer_token(event):
    headers = event.get("headers") or {}

    auth_header = (
        headers.get("authorization")
        or headers.get("Authorization")
    )

    if not auth_header:
        return None

    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[len("Bearer "):].strip()

    return token if token else None


def get_request_details(event):
    request_context = event.get("requestContext") or {}
    http = request_context.get("http") or {}

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

    if isinstance(body, dict):
        return body

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------
# SSM TOKEN FUNCTIONS
# Only ADMIN and PRODUCTS use SSM
# ---------------------------------------------------------

def get_application_token(role, parameter_name):
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

    result = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True,
    )

    token_value = (
        result.get("Parameter", {}).get("Value")
    )

    if not token_value:
        raise RuntimeError(
            f"SSM token value is empty for role: {role}"
        )

    cached["value"] = token_value
    cached["fetched_at"] = now

    return token_value


def get_application_role(incoming_token):
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
        token_value = get_application_token(
            role,
            parameter_name,
        )

        if incoming_token == token_value:
            return {
                "role": role,
                "customer_id": None,
            }

    return None


# ---------------------------------------------------------
# DATABASE CONNECTION
# ---------------------------------------------------------

def get_ssm_parameter(parameter_name):
    result = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True,
    )

    return result["Parameter"]["Value"]


def get_db_connection():
    if not DB_HOST:
        raise RuntimeError("DB_HOST is not configured")

    username = get_ssm_parameter(
        DB_USERNAME_PARAMETER_NAME
    )

    password = get_ssm_parameter(
        DB_PASSWORD_PARAMETER_NAME
    )

    return pymysql.connect(
        host=DB_HOST,
        user=username,
        password=password,
        database=DB_NAME,
        port=3306,
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


# ---------------------------------------------------------
# CUSTOMER AUTHENTICATION
# Customer credentials are checked in login_access
# ---------------------------------------------------------

def authenticate_customer(incoming_token):
    """
    The customer credential is not stored as plain text.

    password_lookup:
        SHA-256 hash of the original credential.

    password_hash:
        bcrypt hash used for verification.
    """

    if not incoming_token:
        return None

    password_lookup = hashlib.sha256(
        incoming_token.encode("utf-8")
    ).hexdigest()

    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            sql = """
                SELECT
                    login_id,
                    customer_id,
                    password_hash,
                    is_active
                FROM login_access
                WHERE password_lookup = %s
                LIMIT 1
            """

            cursor.execute(
                sql,
                (password_lookup,),
            )

            login_record = cursor.fetchone()

            if not login_record:
                return None

            if not login_record["is_active"]:
                return None

            stored_hash = login_record["password_hash"]

            if isinstance(stored_hash, str):
                stored_hash = stored_hash.encode(
                    "utf-8"
                )

            is_valid = bcrypt.checkpw(
                incoming_token.encode("utf-8"),
                stored_hash,
            )

            if not is_valid:
                return None

            cursor.execute(
                """
                UPDATE login_access
                SET last_login_at = CURRENT_TIMESTAMP
                WHERE login_id = %s
                """,
                (login_record["login_id"],),
            )

            connection.commit()

            return {
                "role": "customer",
                "customer_id": login_record["customer_id"],
            }

    except Exception as error:
        if connection:
            connection.rollback()

        log(
            "ERROR",
            "Customer authentication failed",
            error=str(error),
        )

        raise

    finally:
        if connection:
            connection.close()


def identify_user(incoming_token):
    """
    First check application tokens.

    If not admin/products, check the credential
    against the login_access table.
    """

    application_user = get_application_role(
        incoming_token
    )

    if application_user:
        return application_user

    return authenticate_customer(
        incoming_token
    )


# ---------------------------------------------------------
# CUSTOMER REGISTRATION
# /register is public
# ---------------------------------------------------------

def register_customer(event):
    body = get_request_body(event)

    name = body.get("name")
    email = body.get("email")
    password = body.get("password")

    if not name or not email or not password:
        return response(
            400,
            {
                "error": "validation_error",
                "message": (
                    "name, email and password are required"
                ),
            },
        )

    password_bytes = password.encode("utf-8")

    if len(password_bytes) < 8:
        return response(
            400,
            {
                "error": "validation_error",
                "message": (
                    "Password must contain at least 8 bytes"
                ),
            },
        )

    if len(password_bytes) > 72:
        return response(
            400,
            {
                "error": "validation_error",
                "message": (
                    "Password must not exceed 72 bytes"
                ),
            },
        )

    password_lookup = hashlib.sha256(
        password_bytes
    ).hexdigest()

    password_hash = bcrypt.hashpw(
        password_bytes,
        bcrypt.gensalt(),
    ).decode("utf-8")

    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
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
                        password_hash
                    )
                VALUES
                    (%s, %s, %s)
                """,
                (
                    customer_id,
                    password_lookup,
                    password_hash,
                ),
            )

        connection.commit()

        log(
            "INFO",
            "Customer registered successfully",
            customer_id=customer_id,
        )

        return response(
            201,
            {
                "message": "Customer registered successfully",
                "customer_id": customer_id,
            },
        )

    except pymysql.err.IntegrityError:
        if connection:
            connection.rollback()

        return response(
            409,
            {
                "error": "conflict",
                "message": (
                    "Email or customer credential already exists"
                ),
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

        return response(
            500,
            {
                "error": "internal_error",
                "message": "Customer registration failed",
            },
        )

    finally:
        if connection:
            connection.close()


# ---------------------------------------------------------
# ROUTING AND PERMISSIONS
# ---------------------------------------------------------

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


def is_cancel_route(path):
    import re

    return re.match(
        r"^/orders/\d+/cancel$",
        path,
    ) is not None


def is_order_id_route(path):
    import re

    return re.match(
        r"^/orders/\d+$",
        path,
    ) is not None


def get_route_permission(method, path):
    # Product routes
    if is_product_route(path):
        if method in {
            "GET",
            "POST",
            "PUT",
            "DELETE",
        }:
            return "products"

        return None

    # Order collection routes
    if path == "/orders":
        if method in {
            "GET",
            "POST",
        }:
            return "orders"

        return None

    # Order cancellation route
    if is_cancel_route(path):
        if method in {
            "POST",
            "PATCH",
        }:
            return "orders"

        return None

    # Single order route
    if is_order_id_route(path):
        if method in {
            "GET",
            "PATCH",
        }:
            return "orders"

        return None

    return None


def role_allows(role, resource, method):
    # Admin can access products and orders
    if role == "admin":
        return resource in {
            "products",
            "orders",
        }

    # Products role can only access products
    if role == "products":
        return resource == "products"

    # Customer can browse products
    # and access their own orders
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


# ---------------------------------------------------------
# DOWNSTREAM LAMBDA INVOCATION
# ---------------------------------------------------------

def invoke_downstream_lambda(
    target_function,
    event,
):
    result = lambda_client.invoke(
        FunctionName=target_function,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode("utf-8"),
    )

    payload = result["Payload"].read()

    return json.loads(payload)


# ---------------------------------------------------------
# MAIN HANDLER
# ---------------------------------------------------------

def lambda_handler(event, context):
    method, path = get_request_details(event)

    log(
        "INFO",
        "Incoming request",
        method=method,
        path=path,
    )

    # Registration is public.
    # It must happen before token validation.
    if method == "POST" and path == "/register":
        return register_customer(event)

    incoming_token = extract_bearer_token(event)

    if not incoming_token:
        log(
            "WARN",
            "Request missing Authorization header",
        )

        return unauthorized()

    try:
        authenticated_user = identify_user(
            incoming_token
        )

    except Exception:
        return response(
            500,
            {
                "error": "internal_error",
                "message": "Authentication check failed",
            },
        )

    if not authenticated_user:
        log(
            "WARN",
            "Request had an invalid token",
        )

        return unauthorized()

    role = authenticated_user["role"]
    customer_id = authenticated_user.get(
        "customer_id"
    )

    permission = get_route_permission(
        method,
        path,
    )

    if permission is None:
        return response(
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
        )

        return forbidden()

    target_function = route_target(path)

    if not target_function:
        return response(
            404,
            {
                "error": "not_found",
                "message": "No target Lambda found",
            },
        )

    # Pass authenticated information to downstream Lambda
    request_context = event.setdefault(
        "requestContext",
        {},
    )

    authorizer_context = request_context.setdefault(
        "authorizer",
        {},
    )

    authorizer_context["role"] = role

    if customer_id is not None:
        authorizer_context["customer_id"] = (
            customer_id
        )

    log(
        "INFO",
        "RBAC authorized",
        role=role,
        customer_id=customer_id,
        method=method,
        path=path,
        target=target_function,
    )

    try:
        return invoke_downstream_lambda(
            target_function,
            event,
        )

    except Exception as error:
        log(
            "ERROR",
            "Failed to invoke downstream Lambda",
            error=str(error),
            target=target_function,
        )

        return response(
            500,
            {
                "error": "internal_error",
                "message": "Downstream service failed",
            },
        )
