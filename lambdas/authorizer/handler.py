import json
import os
import time
import boto3


ssm = boto3.client("ssm")
lambda_client = boto3.client("lambda")


ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")

ADMIN_TOKEN_PARAM = os.environ.get(
    "ADMIN_TOKEN_PARAM",
    f"/cloudmart/{ENVIRONMENT}/auth/admin-token"
)

PRODUCTS_TOKEN_PARAM = os.environ.get(
    "PRODUCTS_TOKEN_PARAM",
    f"/cloudmart/{ENVIRONMENT}/auth/products-token"
)

ORDERS_TOKEN_PARAM = os.environ.get(
    "ORDERS_TOKEN_PARAM",
    f"/cloudmart/{ENVIRONMENT}/auth/orders-token"
)

PRODUCT_LAMBDA_NAME = os.environ.get("PRODUCT_LAMBDA_NAME")
ORDER_LAMBDA_NAME = os.environ.get("ORDER_LAMBDA_NAME")

CACHE_TTL_SECONDS = 300


# ------------------------------------------------------------
# Token cache
# ------------------------------------------------------------

_token_cache = {
    "admin": {"value": None, "fetched_at": 0},
    "products": {"value": None, "fetched_at": 0},
    "orders": {"value": None, "fetched_at": 0},
}


# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------

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


# ------------------------------------------------------------
# Get token from SSM
# ------------------------------------------------------------

def get_token(role, parameter_name):

    now = time.time()

    cached = _token_cache[role]

    if (
        cached["value"] is not None
        and (now - cached["fetched_at"]) < CACHE_TTL_SECONDS
    ):
        log(
            "INFO",
            "Using cached token",
            role=role
        )

        return cached["value"]

    log(
        "INFO",
        "Fetching token from SSM",
        role=role,
        parameter=parameter_name
    )

    response = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True
    )

    token = response["Parameter"]["Value"]

    _token_cache[role] = {
        "value": token,
        "fetched_at": now
    }

    return token


# ------------------------------------------------------------
# Authentication failure
# ------------------------------------------------------------

def unauthorized():

    return {
        "statusCode": 401,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(
            {
                "error": "unauthorized",
                "message": "Missing or invalid token"
            }
        )
    }


# ------------------------------------------------------------
# Authorization failure
# ------------------------------------------------------------

def forbidden():

    return {
        "statusCode": 403,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(
            {
                "error": "forbidden",
                "message": "Token is not authorized for this resource"
            }
        )
    }


# ------------------------------------------------------------
# Extract Bearer token
# ------------------------------------------------------------

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

    return auth_header[len("Bearer "):].strip()


# ------------------------------------------------------------
# Identify role from token
# ------------------------------------------------------------

def identify_role(incoming_token):

    tokens = {
        "admin": (
            ADMIN_TOKEN_PARAM
        ),
        "products": (
            PRODUCTS_TOKEN_PARAM
        ),
        "orders": (
            ORDERS_TOKEN_PARAM
        ),
    }

    for role, parameter_name in tokens.items():

        try:

            valid_token = get_token(
                role,
                parameter_name
            )

            if incoming_token == valid_token:

                return role

        except Exception as e:

            log(
                "ERROR",
                "Failed to fetch role token from SSM",
                role=role,
                error=str(e)
            )

    return None


# ------------------------------------------------------------
# Get request path
# ------------------------------------------------------------

def get_path(event):

    return (
        event
        .get("requestContext", {})
        .get("http", {})
        .get("path", "")
    )


# ------------------------------------------------------------
# Check role permission
# ------------------------------------------------------------

def is_allowed(role, path):

    if role == "admin":

        return (
            path.startswith("/products")
            or
            path.startswith("/orders")
        )

    if role == "products":

        return path.startswith("/products")

    if role == "orders":

        return path.startswith("/orders")

    return False


# ------------------------------------------------------------
# Decide downstream Lambda
# ------------------------------------------------------------

def route_target(event):

    path = get_path(event)

    if path.startswith("/products"):

        return PRODUCT_LAMBDA_NAME

    if path.startswith("/orders"):

        return ORDER_LAMBDA_NAME

    return None


# ------------------------------------------------------------
# Lambda handler
# ------------------------------------------------------------

def lambda_handler(event, context):

    incoming_token = extract_bearer_token(event)

    # --------------------------------------------------------
    # Authentication
    # --------------------------------------------------------

    if not incoming_token:

        log(
            "WARN",
            "Request missing Authorization header"
        )

        return unauthorized()


    # --------------------------------------------------------
    # Identify role
    # --------------------------------------------------------

    role = identify_role(incoming_token)

    if not role:

        log(
            "WARN",
            "Request had an invalid token"
        )

        return unauthorized()


    # --------------------------------------------------------
    # Get path
    # --------------------------------------------------------

    path = get_path(event)


    # --------------------------------------------------------
    # Authorization
    # --------------------------------------------------------

    if not is_allowed(role, path):

        log(
            "WARN",
            "Role is not authorized for path",
            role=role,
            path=path
        )

        return forbidden()


    # --------------------------------------------------------
    # Find Lambda
    # --------------------------------------------------------

    target_function = route_target(event)

    if not target_function:

        log(
            "WARN",
            "No route matched for path",
            path=path
        )

        return {
            "statusCode": 404,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(
                {
                    "error": "not_found",
                    "message": "No matching route"
                }
            )
        }


    # --------------------------------------------------------
    # Invoke downstream Lambda
    # --------------------------------------------------------

    log(
        "INFO",
        "Authorized request, invoking downstream Lambda",
        role=role,
        path=path,
        target=target_function
    )

    try:

        response = lambda_client.invoke(
            FunctionName=target_function,
            InvocationType="RequestResponse",
            Payload=json.dumps(event).encode("utf-8")
        )

        payload = json.loads(
            response["Payload"].read()
        )

        return payload

    except Exception as e:

        log(
            "ERROR",
            "Failed to invoke downstream Lambda",
            error=str(e),
            target=target_function
        )

        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(
                {
                    "error": "internal_error",
                    "message": "Downstream service failed"
                }
            )
        }
