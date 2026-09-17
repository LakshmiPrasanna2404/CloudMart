import json
import os
import time
import re
import hashlib

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
    "PRODUCT_LAMBDA_NAME",
)

ORDER_LAMBDA_NAME = os.environ.get(
    "ORDER_LAMBDA_NAME",
)

DB_HOST = os.environ.get("DB_HOST", "")
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


def internal_error(message="Auth check failed"):
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
# SSM TOKEN FUNCTIONS
# ============================================================

def get_application_token(role, parameter_name):
    """
    Fetch admin or products token from SSM Parameter Store.

    Customer credentials are NOT stored in SSM.
    Customer credentials are verified using RDS login_access.
    """

    if role not in _token_cache:
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
        response.get("Parameter", {}).get("Value")
    )

    if not parameter_value:
        raise RuntimeError(
            f"SSM parameter value is empty for role: {role}"
        )

    cached["value"] = parameter_value
    cached["fetched_at"] = now

    return parameter_value


# ============================================================
# DATABASE CREDENTIALS
# ============================================================

def get_database_credentials():
    """
    Read the RDS username and password from SSM.
    Credentials are cached for five minutes.
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
        username_response.get("Parameter", {}).get("Value")
    )

    password = (
        password_response.get("Parameter", {}).get("Value")
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
    Create a connection to the RDS MySQL database.
    """

    if not DB_HOST:
        raise RuntimeError(
            "DB_HOST environment variable is missing"
        )

    username, password = get_database_credentials()

    return pymysql.connect(
        host=DB_HOST,
        user=username,
        password=password,
        database=DB_NAME,
        port=3306,
        connect_timeout=3,
        read_timeout=3,
        write_timeout=3,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


# ============================================================
# CUSTOMER AUTHENTICATION
# ============================================================

def get_customer_from_credential(incoming_token):
    """
    Authenticate a customer using the login_access table.

    The raw customer credential is never stored in the database.

    password_lookup:
        SHA-256 hash used to locate the customer.

    password_hash:
        Bcrypt hash used to verify the credential.
    """

    if not incoming_token:
        return None

    password_lookup = hashlib.sha256(
        incoming_token.encode("utf-8")
    ).hexdigest()

    connection = None

    try:
        connection = get_database_connection()

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
            stored_hash = stored_hash.encode("utf-8")

        is_valid = bcrypt.checkpw(
            incoming_token.encode("utf-8"),
            stored_hash,
        )

        if not is_valid:
            return None

        return {
            "role": "customer",
            "customer_id": login_record["customer_id"],
        }

    finally:
        if connection:
            connection.close()


# ============================================================
# TOKEN EXTRACTION
# ============================================================

def extract_bearer_token(event):
    headers = event.get("headers", {}) or {}

    auth_header = (
        headers.get("authorization")
        or headers.get("Authorization")
    )

    if not auth_header:
        return None

    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[
        len("Bearer "):
    ].strip()

    if not token:
        return None

    return token


# ============================================================
# REQUEST DETAILS
# ============================================================

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
# ROLE IDENTIFICATION
# ============================================================

def get_role_for_token(incoming_token):
    """
    Authentication order:

    1. Check admin SSM token.
    2. Check products SSM token.
    3. Check individual customer credential in RDS.
    """

    application_tokens = [
        (
            "admin",
            ADMIN_TOKEN_PARAMETER_NAME,
        ),
        (
            "products",
            PRODUCTS_TOKEN_PARAMETER_NAME,
        ),
    ]

    # --------------------------------------------------------
    # ADMIN AND PRODUCTS AUTHENTICATION
    # --------------------------------------------------------

    for role, parameter_name in application_tokens:
        try:
            configured_token = get_application_token(
                role,
                parameter_name,
            )

            if incoming_token == configured_token:
                return {
                    "role": role,
                    "customer_id": None,
                }

        except Exception as error:
            log(
                "ERROR",
                "Failed to fetch application token from SSM",
                role=role,
                error=str(error),
            )

            raise

    # --------------------------------------------------------
    # CUSTOMER AUTHENTICATION
    # --------------------------------------------------------

    try:
        customer_identity = (
            get_customer_from_credential(
                incoming_token
            )
        )

        if customer_identity:
            return customer_identity

    except Exception as error:
        log(
            "ERROR",
            "Failed to authenticate customer using RDS",
            error=str(error),
        )

        raise

    return None


# ============================================================
# ROUTE IDENTIFICATION
# ============================================================

def is_products_route(path):
    return (
        path == "/products"
        or path.startswith("/products/")
    )


def is_orders_route(path):
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


# ============================================================
# ROUTE PERMISSIONS
# ============================================================

def get_route_permission(method, path):
    """
    Return the logical resource.

    Products:
        GET, POST, PUT, DELETE /products

    Orders:
        GET, POST /orders
        GET /orders/{id}
        POST /orders/{id}/cancel
    """

    # --------------------------------------------------------
    # PRODUCTS
    # --------------------------------------------------------

    if is_products_route(path):

        if method in {
            "GET",
            "POST",
            "PUT",
            "DELETE",
        }:
            return "products"

        return None

    # --------------------------------------------------------
    # ORDER COLLECTION
    # --------------------------------------------------------

    if path == "/orders":

        if method in {
            "GET",
            "POST",
        }:
            return "orders"

        return None

    # --------------------------------------------------------
    # ORDER CANCELLATION
    # --------------------------------------------------------

    if is_order_cancel_route(path):

        if method == "POST":
            return "orders"

        return None

    # --------------------------------------------------------
    # INDIVIDUAL ORDER
    # --------------------------------------------------------

    if is_order_id_route(path):

        if method in {
            "GET",
            "PATCH",
        }:
            return "orders"

        return None

    return None


def role_allows(role, resource, method):
    """
    Admin:
        Full product and order access.

    Products:
        Product operations only.

    Customer:
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

    return False


# ============================================================
# TARGET LAMBDA
# ============================================================

def route_target(path):
    if is_products_route(path):
        return PRODUCT_LAMBDA_NAME

    if is_orders_route(path):
        return ORDER_LAMBDA_NAME

    return None


# ============================================================
# MAIN LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):

    incoming_token = extract_bearer_token(event)

    if not incoming_token:
        log(
            "WARN",
            "Request missing Authorization header",
        )

        return unauthorized()

    # --------------------------------------------------------
    # AUTHENTICATION
    # --------------------------------------------------------

    try:
        identity = get_role_for_token(
            incoming_token
        )

    except Exception as error:
        log(
            "ERROR",
            "Authentication failed",
            error=str(error),
        )

        return internal_error(
            "Auth check failed"
        )

    if identity is None:
        log(
            "WARN",
            "Request had an invalid token",
        )

        return unauthorized()

    role = identity["role"]
    customer_id = identity.get("customer_id")

    # --------------------------------------------------------
    # REQUEST DETAILS
    # --------------------------------------------------------

    method, path = get_request_details(event)

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

    # --------------------------------------------------------
    # AUTHORIZATION
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # TARGET LAMBDA
    # --------------------------------------------------------

    target_function = route_target(path)

    if not target_function:
        return not_found()

    # --------------------------------------------------------
    # PASS AUTHENTICATION DETAILS DOWNSTREAM
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # INVOKE DOWNSTREAM LAMBDA
    # --------------------------------------------------------

    try:
        response = lambda_client.invoke(
            FunctionName=target_function,
            InvocationType="RequestResponse",
            Payload=json.dumps(event).encode("utf-8"),
        )

        payload = response["Payload"].read()

        downstream_response = json.loads(payload)

        return downstream_response

    except Exception as error:
        log(
            "ERROR",
            "Failed to invoke downstream Lambda",
            target=target_function,
            error=str(error),
        )

        return internal_error(
            "Downstream service failed"
        )
