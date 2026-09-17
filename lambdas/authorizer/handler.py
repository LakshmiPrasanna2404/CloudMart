import json
import os
import time
import hashlib
import re

import boto3
import pymysql
import bcrypt

from botocore.config import Config


# ============================================================
# AWS CONFIGURATION
# ============================================================

aws_config = Config(
    connect_timeout=5,
    read_timeout=5,
    retries={"max_attempts": 2},
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
    "/cloudmart/prod/auth/admin-token",
)

PRODUCTS_TOKEN_PARAMETER_NAME = os.environ.get(
    "PRODUCTS_TOKEN_PARAMETER_NAME",
    "/cloudmart/prod/auth/products-token",
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
# STANDARD RESPONSES
# ============================================================

def json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
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
            "message": "You do not have permission for this operation",
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
        import base64

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
# SSM HELPERS
# ============================================================

def get_ssm_parameter(parameter_name):
    if not parameter_name:
        raise RuntimeError("SSM parameter name is empty")

    response = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True,
    )

    value = (
        response.get("Parameter", {})
        .get("Value")
    )

    if not value:
        raise RuntimeError(
            f"SSM parameter is empty: {parameter_name}"
        )

    return value


def get_application_token(role, parameter_name):
    now = time.time()

    cached = _token_cache.get(role)

    if cached is None:
        raise RuntimeError(
            f"Unsupported application role: {role}"
        )

    if (
        cached["value"] is not None
        and now - cached["fetched_at"] < CACHE_TTL_SECONDS
    ):
        return cached["value"]

    token = get_ssm_parameter(parameter_name)

    cached["value"] = token
    cached["fetched_at"] = now

    return token


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_database_credentials():
    username = get_ssm_parameter(
        DB_USERNAME_PARAMETER_NAME
    )

    password = get_ssm_parameter(
        DB_PASSWORD_PARAMETER_NAME
    )

    return username, password


def get_db_connection():
    if not DB_HOST:
        raise RuntimeError(
            "DB_HOST environment variable is not configured"
        )

    username, password = get_database_credentials()

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


# ============================================================
# PASSWORD HELPERS
# ============================================================

def get_password_lookup(password):
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def hash_password(password):
    password_bytes = password.encode("utf-8")

    if len(password_bytes) > 72:
        raise ValueError(
            "Password must not exceed 72 bytes"
        )

    return bcrypt.hashpw(
        password_bytes,
        bcrypt.gensalt()
    ).decode("utf-8")


def verify_password(password, password_hash):
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except Exception:
        return False


# ============================================================
# APPLICATION TOKEN AUTHENTICATION
# ============================================================

def identify_application_role(incoming_token):
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

    for role, parameter_name in application_tokens:
        try:
            stored_token = get_application_token(
                role,
                parameter_name,
            )

            if incoming_token == stored_token:
                return {
                    "role": role,
                    "customer_id": None,
                }

        except Exception as error:
            log(
                "ERROR",
                "Failed to fetch application token",
                role=role,
                error=str(error),
            )

            raise

    return None


# ============================================================
# CUSTOMER AUTHENTICATION
# ============================================================

def authenticate_customer(incoming_token):
    """
    Customer credentials are verified against login_access.

    The incoming Bearer token is treated as the customer's
    password-like credential.

    The raw credential is never stored in the database.
    """

    if not incoming_token:
        return None

    password_lookup = get_password_lookup(
        incoming_token
    )

    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            sql = """
                SELECT
                    la.login_id,
                    la.customer_id,
                    la.password_hash,
                    la.is_active
                FROM login_access la
                WHERE la.password_lookup = %s
                  AND la.is_active = TRUE
                LIMIT 1
            """

            cursor.execute(
                sql,
                (password_lookup,),
            )

            login_record = cursor.fetchone()

            if not login_record:
                return None

            password_hash = login_record["password_hash"]

            if not verify_password(
                incoming_token,
                password_hash,
            ):
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

    finally:
        if connection:
            connection.close()


def authenticate_token(incoming_token):
    """
    Authentication order:

    1. Admin application token
    2. Products application token
    3. Individual customer credential
    """

    application_identity = identify_application_role(
        incoming_token
    )

    if application_identity:
        return application_identity

    customer_identity = authenticate_customer(
        incoming_token
    )

    if customer_identity:
        return customer_identity

    return None


# ============================================================
# ROUTE HELPERS
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


def is_public_route(method, path):
    return (
        method == "POST"
        and path in {"/register", "/login"}
    )


def get_route_permission(method, path):
    """
    Returns the resource associated with a request.

    Products:
      GET, POST, PUT, DELETE

    Orders:
      GET, POST, PATCH
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
        }:
            return "orders"

        return None

    if re.match(
        r"^/orders/\d+$",
        path,
    ):
        if method in {
            "GET",
            "PATCH",
        }:
            return "orders"

        return None

    if re.match(
        r"^/orders/\d+/cancel$",
        path,
    ):
        if method == "POST":
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


def get_target_lambda(path):
    if is_product_route(path):
        return PRODUCT_LAMBDA_NAME

    if is_order_route(path):
        return ORDER_LAMBDA_NAME

    return None


# ============================================================
# CUSTOMER REGISTRATION
# ============================================================

def register_customer(event):
    body = get_request_body(event)

    name = str(body.get("name", "")).strip()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))

    if not name or not email or not password:
        return json_response(
            400,
            {
                "error": "validation_error",
                "message": (
                    "name, email and password are required"
                ),
            },
        )

    if len(password.encode("utf-8")) > 72:
        return json_response(
            400,
            {
                "error": "validation_error",
                "message": (
                    "Password must not exceed 72 bytes"
                ),
            },
        )

    if "@" not in email:
        return json_response(
            400,
            {
                "error": "validation_error",
                "message": "Invalid email address",
            },
        )

    password_lookup = get_password_lookup(password)
    password_hash = hash_password(password)

    connection = None

    try:
        connection = get_db_connection()

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
                        "message": (
                            "A customer with this email already exists"
                        ),
                    },
                )

            cursor.execute(
                """
                INSERT INTO customers
                (
                    name,
                    email
                )
                VALUES
                (
                    %s,
                    %s
                )
                """,
                (
                    name,
                    email,
                ),
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
                (
                    %s,
                    %s,
                    %s,
                    TRUE
                )
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
                },
            )

    except pymysql.err.IntegrityError:
        if connection:
            connection.rollback()

        return json_response(
            409,
            {
                "error": "registration_failed",
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
# CUSTOMER LOGIN
# ============================================================

def login_customer(event):
    body = get_request_body(event)

    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))

    if not email or not password:
        return json_response(
            400,
            {
                "error": "validation_error",
                "message": "Email and password are required",
            },
        )

    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    c.customer_id,
                    c.email,
                    la.password_hash
                FROM customers c
                INNER JOIN login_access la
                    ON c.customer_id = la.customer_id
                WHERE c.email = %s
                  AND la.is_active = TRUE
                LIMIT 1
                """,
                (email,),
            )

            customer = cursor.fetchone()

            if not customer:
                return unauthorized()

            if not verify_password(
                password,
                customer["password_hash"],
            ):
                return unauthorized()

            cursor.execute(
                """
                UPDATE login_access
                SET last_login_at = CURRENT_TIMESTAMP
                WHERE customer_id = %s
                  AND is_active = TRUE
                """,
                (customer["customer_id"],),
            )

            connection.commit()

            return json_response(
                200,
                {
                    "message": "Login successful",
                    "customer_id": customer["customer_id"],
                    "email": customer["email"],
                    "access_token": password,
                    "token_type": "Bearer",
                },
            )

    except Exception as error:
        log(
            "ERROR",
            "Customer login failed",
            error=str(error),
        )

        return json_response(
            500,
            {
                "error": "internal_error",
                "message": "Login failed",
            },
        )

    finally:
        if connection:
            connection.close()


# ============================================================
# INVOKE DOWNSTREAM LAMBDA
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

    payload = response.get("Payload")

    if not payload:
        return json_response(
            500,
            {
                "error": "internal_error",
                "message": "Empty downstream response",
            },
        )

    raw_payload = payload.read()

    if isinstance(raw_payload, bytes):
        raw_payload = raw_payload.decode("utf-8")

    return json.loads(raw_payload)


# ============================================================
# MAIN LAMBDA HANDLER
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

        # ----------------------------------------------------
        # PUBLIC REGISTRATION AND LOGIN
        # ----------------------------------------------------

        if method == "POST" and path == "/register":
            return register_customer(event)

        if method == "POST" and path == "/login":
            return login_customer(event)

        # ----------------------------------------------------
        # AUTHORIZATION HEADER
        # ----------------------------------------------------

        incoming_token = extract_bearer_token(event)

        if not incoming_token:
            log(
                "WARN",
                "Request missing Authorization header",
            )

            return unauthorized()

        # ----------------------------------------------------
        # AUTHENTICATE TOKEN
        # ----------------------------------------------------

        identity = authenticate_token(
            incoming_token
        )

        if not identity:
            log(
                "WARN",
                "Request had an invalid token",
            )

            return unauthorized()

        role = identity["role"]
        customer_id = identity.get("customer_id")

        # ----------------------------------------------------
        # CHECK ROUTE PERMISSION
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

        # ----------------------------------------------------
        # FIND TARGET LAMBDA
        # ----------------------------------------------------

        target_function = get_target_lambda(path)

        if not target_function:
            return json_response(
                404,
                {
                    "error": "not_found",
                    "message": "Target Lambda not found",
                },
            )

        # ----------------------------------------------------
        # PASS AUTHENTICATED IDENTITY DOWNSTREAM
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

        # ----------------------------------------------------
        # INVOKE PRODUCT OR ORDER LAMBDA
        # ----------------------------------------------------

        return invoke_downstream_lambda(
            target_function,
            event,
        )

    except Exception as error:
        log(
            "ERROR",
            "Authorizer execution failed",
            error=str(error),
        )

        return json_response(
            500,
            {
                "error": "internal_error",
                "message": "Authentication or authorization failed",
            },
        )
