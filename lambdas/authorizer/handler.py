import hashlib
import json
import os
import re
import time

import bcrypt
import boto3
import pymysql

from botocore.config import Config


aws_config = Config(
    connect_timeout=2,
    read_timeout=3,
    retries={"max_attempts": 1}
)


ssm = boto3.client(
    "ssm",
    config=aws_config
)


lambda_client = boto3.client(
    "lambda",
    config=aws_config
)


ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")

ADMIN_TOKEN_PARAMETER_NAME = os.environ.get(
    "ADMIN_TOKEN_PARAMETER_NAME",
    ""
)

PRODUCTS_TOKEN_PARAMETER_NAME = os.environ.get(
    "PRODUCTS_TOKEN_PARAMETER_NAME",
    ""
)

PRODUCT_LAMBDA_NAME = os.environ.get(
    "PRODUCT_LAMBDA_NAME"
)

ORDER_LAMBDA_NAME = os.environ.get(
    "ORDER_LAMBDA_NAME"
)

DB_HOST = os.environ.get("DB_HOST")
DB_NAME = os.environ.get("DB_NAME", "cloudmart")


CACHE_TTL_SECONDS = 300


_token_cache = {
    "admin": {
        "value": None,
        "fetched_at": 0
    },

    "products": {
        "value": None,
        "fetched_at": 0
    }
}


_db_credentials_cache = {
    "username": None,
    "password": None,
    "fetched_at": 0
}


def log(level, message, **extra):

    print(
        json.dumps(
            {
                "level": level,
                "message": message,
                **extra
            }
        )
    )


# ============================================================
# SSM
# ============================================================

def get_ssm_token(role, parameter_name):

    now = time.time()

    cached = _token_cache[role]

    if (
        cached["value"] is not None
        and now - cached["fetched_at"] < CACHE_TTL_SECONDS
    ):
        return cached["value"]

    if not parameter_name:
        raise RuntimeError(
            f"SSM parameter is not configured for role: {role}"
        )

    result = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True
    )

    value = result["Parameter"]["Value"]

    if not value:
        raise RuntimeError(
            f"SSM parameter is empty for role: {role}"
        )

    cached["value"] = value
    cached["fetched_at"] = now

    return value


# ============================================================
# DATABASE
# ============================================================

def get_db_credentials():

    now = time.time()

    if (
        _db_credentials_cache["username"]
        and _db_credentials_cache["password"]
        and now - _db_credentials_cache["fetched_at"]
        < CACHE_TTL_SECONDS
    ):
        return (
            _db_credentials_cache["username"],
            _db_credentials_cache["password"]
        )

    username = ssm.get_parameter(
        Name=f"/cloudmart/{ENVIRONMENT}/db/username",
        WithDecryption=True
    )["Parameter"]["Value"]

    password = ssm.get_parameter(
        Name=f"/cloudmart/{ENVIRONMENT}/db/password",
        WithDecryption=True
    )["Parameter"]["Value"]

    _db_credentials_cache["username"] = username
    _db_credentials_cache["password"] = password
    _db_credentials_cache["fetched_at"] = now

    return username, password


def get_db_connection():

    username, password = get_db_credentials()

    return pymysql.connect(
        host=DB_HOST,
        user=username,
        password=password,
        database=DB_NAME,
        connect_timeout=3,
        read_timeout=3,
        write_timeout=3,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor
    )


# ============================================================
# HTTP RESPONSES
# ============================================================

def response(status_code, body):

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body)
    }


def unauthorized():

    return response(
        401,
        {
            "error": "unauthorized",
            "message": "Missing or invalid credential"
        }
    )


def forbidden():

    return response(
        403,
        {
            "error": "forbidden",
            "message": "Credential does not have permission"
        }
    )


# ============================================================
# REQUEST PARSING
# ============================================================

def get_request_details(event):

    request_context = event.get(
        "requestContext",
        {}
    )

    http = request_context.get(
        "http",
        {}
    )

    method = (
        http.get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()

    path = (
        http.get("path")
        or event.get("rawPath")
        or event.get("path")
        or "/"
    )

    return method, path


def extract_bearer_credential(event):

    headers = event.get(
        "headers",
        {}
    ) or {}

    auth_header = (
        headers.get("authorization")
        or headers.get("Authorization")
        or ""
    )

    if not auth_header:
        return None

    if not auth_header.lower().startswith("bearer "):
        return None

    credential = auth_header[7:].strip()

    if not credential:
        return None

    return credential


def parse_body(event):

    body = event.get("body")

    if not body:
        return {}

    if isinstance(body, dict):
        return body

    try:
        return json.loads(body)

    except json.JSONDecodeError:
        return None


# ============================================================
# ADMIN / PRODUCTS AUTHENTICATION
# ============================================================

def authenticate_application_token(incoming):

    try:

        admin_token = get_ssm_token(
            "admin",
            ADMIN_TOKEN_PARAMETER_NAME
        )

        if incoming == admin_token:
            return {
                "role": "admin",
                "customer_id": None
            }

        products_token = get_ssm_token(
            "products",
            PRODUCTS_TOKEN_PARAMETER_NAME
        )

        if incoming == products_token:
            return {
                "role": "products",
                "customer_id": None
            }

    except Exception as exc:

        log(
            "ERROR",
            "Application authentication failed",
            error=str(exc)
        )

        raise

    return None


# ============================================================
# CUSTOMER AUTHENTICATION
# ============================================================

def authenticate_customer_credential(credential):

    if not credential:
        return None

    lookup = hashlib.sha256(
        credential.encode("utf-8")
    ).hexdigest()

    conn = None

    try:

        conn = get_db_connection()

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    login_id,
                    customer_id,
                    password_hash,
                    is_active
                FROM login_access
                WHERE password_lookup = %s
                  AND is_active = TRUE
                  AND revoked_at IS NULL
                LIMIT 1
                """,
                (lookup,)
            )

            login = cur.fetchone()

            if not login:
                return None

            stored_hash = login["password_hash"]

            if isinstance(stored_hash, str):
                stored_hash = stored_hash.encode("utf-8")

            if not bcrypt.checkpw(
                credential.encode("utf-8"),
                stored_hash
            ):
                return None

            cur.execute(
                """
                UPDATE login_access
                SET last_login_at = CURRENT_TIMESTAMP
                WHERE login_id = %s
                """,
                (login["login_id"],)
            )

            return {
                "role": "customer",
                "customer_id": int(login["customer_id"])
            }

    finally:

        if conn:
            conn.close()


# ============================================================
# ROUTING / RBAC
# ============================================================

def is_product_route(path):

    return (
        path == "/products"
        or path.startswith("/products/")
    )


def is_order_collection_route(path):

    return path == "/orders"


def is_order_id_route(path):

    return re.match(
        r"^/orders/\d+$",
        path
    ) is not None


def role_allowed(role, method, path):

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    if role == "admin":

        if is_product_route(path):

            return method in {
                "GET",
                "POST",
                "PUT",
                "DELETE"
            }

        if is_order_collection_route(path):

            return method in {
                "GET",
                "POST"
            }

        if is_order_id_route(path):

            return method in {
                "GET",
                "PATCH"
            }

        return False

    # --------------------------------------------------------
    # PRODUCTS
    # --------------------------------------------------------

    if role == "products":

        if not is_product_route(path):
            return False

        return method in {
            "GET",
            "POST",
            "PUT",
            "DELETE"
        }

    # --------------------------------------------------------
    # CUSTOMER
    # --------------------------------------------------------

    if role == "customer":

        # Customer can browse products.
        if is_product_route(path):

            return method == "GET"

        # Customer can create/list orders.
        if is_order_collection_route(path):

            return method in {
                "GET",
                "POST"
            }

        # Customer can view/cancel a specific order.
        if is_order_id_route(path):

            return method in {
                "GET",
                "PATCH"
            }

        return False

    return False


def get_target_lambda(path):

    if is_product_route(path):

        return PRODUCT_LAMBDA_NAME

    if (
        is_order_collection_route(path)
        or is_order_id_route(path)
    ):

        return ORDER_LAMBDA_NAME

    return None


# ============================================================
# DOWNSTREAM INVOCATION
# ============================================================

def invoke_lambda(function_name, event):

    result = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode("utf-8")
    )

    payload = result.get("Payload")

    if not payload:

        return response(
            500,
            {
                "error": "internal_error",
                "message": "Empty downstream Lambda response"
            }
        )

    raw = payload.read().decode("utf-8")

    if not raw:

        return response(
            500,
            {
                "error": "internal_error",
                "message": "Empty downstream Lambda payload"
            }
        )

    return json.loads(raw)


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
            path=path
        )

        credential = extract_bearer_credential(event)

        if not credential:

            log(
                "WARN",
                "Missing customer/application credential"
            )

            return unauthorized()

        # ----------------------------------------------------
        # First check admin/products application credentials.
        # ----------------------------------------------------

        identity = authenticate_application_token(
            credential
        )

        # ----------------------------------------------------
        # If not admin/products, check customer credential.
        # ----------------------------------------------------

        if identity is None:

            identity = authenticate_customer_credential(
                credential
            )

        # ----------------------------------------------------
        # No valid identity.
        # ----------------------------------------------------

        if identity is None:

            log(
                "WARN",
                "Invalid authentication credential"
            )

            return unauthorized()

        role = identity["role"]
        customer_id = identity["customer_id"]

        log(
            "INFO",
            "Authentication successful",
            role=role,
            customer_id=customer_id
        )

        # ----------------------------------------------------
        # RBAC
        # ----------------------------------------------------

        if not role_allowed(
            role,
            method,
            path
        ):

            log(
                "WARN",
                "Authorization denied",
                role=role,
                method=method,
                path=path
            )

            return forbidden()

        # ----------------------------------------------------
        # Determine target Lambda.
        # ----------------------------------------------------

        target_lambda = get_target_lambda(path)

        if not target_lambda:

            return response(
                404,
                {
                    "error": "not_found",
                    "message": "Route not found"
                }
            )

        # ----------------------------------------------------
        # Pass authenticated identity downstream.
        # ----------------------------------------------------

        request_context = event.setdefault(
            "requestContext",
            {}
        )

        request_context["authorizer"] = {
            "role": role,
            "customer_id": customer_id
        }

        log(
            "INFO",
            "Invoking downstream Lambda",
            target=target_lambda,
            role=role,
            customer_id=customer_id
        )

        return invoke_lambda(
            target_lambda,
            event
        )

    except Exception as exc:

        log(
            "ERROR",
            "Authorizer error",
            error=str(exc)
        )

        return response(
            500,
            {
                "error": "internal_error",
                "message": "Authentication service error"
            }
        )
