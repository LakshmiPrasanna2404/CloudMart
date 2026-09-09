import json
import os
import time
import boto3
from botocore.config import Config


aws_config = Config(
    connect_timeout=2,
    read_timeout=3,
    retries={"max_attempts": 1},
)


# SSM Parameter Store
ssm = boto3.client(
    "ssm",
    config=aws_config,
)


# Lambda invocation
lambda_client = boto3.client(
    "lambda",
    config=aws_config,
)


ENVIRONMENT = os.environ.get(
    "ENVIRONMENT",
    "prod",
)


# SSM SecureString parameter names
ADMIN_TOKEN_PARAMETER_NAME = os.environ.get(
    "ADMIN_TOKEN_PARAMETER_NAME",
    "",
)

PRODUCTS_TOKEN_PARAMETER_NAME = os.environ.get(
    "PRODUCTS_TOKEN_PARAMETER_NAME",
    "",
)

ORDERS_TOKEN_PARAMETER_NAME = os.environ.get(
    "ORDERS_TOKEN_PARAMETER_NAME",
    "",
)


PRODUCT_LAMBDA_NAME = os.environ.get(
    "PRODUCT_LAMBDA_NAME"
)

ORDER_LAMBDA_NAME = os.environ.get(
    "ORDER_LAMBDA_NAME"
)


# Cache tokens for 5 minutes
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
    "orders": {
        "value": None,
        "fetched_at": 0,
    },
}


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


def get_token(role, parameter_name):
    """
    Read an RBAC token from AWS Systems Manager
    Parameter Store.

    The parameters are SecureString values, so
    WithDecryption=True is required.
    """

    now = time.time()

    cached = _token_cache[role]

    # Return cached token if still valid
    if (
        cached["value"] is not None
        and now - cached["fetched_at"] < CACHE_TTL_SECONDS
    ):
        return cached["value"]

    if not parameter_name:
        raise RuntimeError(
            f"SSM parameter is not configured for role: {role}"
        )

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

    return cached["value"]


def unauthorized():
    return {
        "statusCode": 401,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(
            {
                "error": "unauthorized",
                "message": "Missing or invalid token",
            }
        ),
    }


def forbidden():
    return {
        "statusCode": 403,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(
            {
                "error": "forbidden",
                "message": (
                    "Token does not have permission "
                    "for this operation"
                ),
            }
        ),
    }


def extract_bearer_token(event):
    headers = event.get(
        "headers",
        {}
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
    http = (
        event
        .get("requestContext", {})
        .get("http", {})
    )

    method = http.get(
        "method",
        ""
    )

    path = http.get(
        "path",
        ""
    )

    return method.upper(), path


def get_role_for_token(incoming_token):

    token_sources = [
        (
            "admin",
            ADMIN_TOKEN_PARAMETER_NAME,
        ),
        (
            "products",
            PRODUCTS_TOKEN_PARAMETER_NAME,
        ),
        (
            "orders",
            ORDERS_TOKEN_PARAMETER_NAME,
        ),
    ]

    for role, parameter_name in token_sources:

        try:

            expected_token = get_token(
                role,
                parameter_name,
            )

            if incoming_token == expected_token:
                return role

        except Exception as e:

            log(
                "ERROR",
                "Failed to fetch RBAC token from SSM Parameter Store",
                role=role,
                parameter=parameter_name,
                error=str(e),
            )

            raise

    return None


def get_route_permission(method, path):
    """
    Return the logical resource for the request.

    Admin:
        Full access to Products and Orders.

    Products role:
        Product API only.

    Orders role:
        Customer/order operations.

        GET /products is intentionally allowed so
        customers can browse products before ordering.
    """

    # -----------------------------------------
    # PRODUCTS
    # -----------------------------------------

    if (
        path == "/products"
        or path.startswith("/products/")
    ):

        if method in {
            "GET",
            "POST",
            "PUT",
            "DELETE",
        }:
            return "products"

        return None

    # -----------------------------------------
    # ORDERS
    # -----------------------------------------

    if path == "/orders":

        if method in {
            "GET",
            "POST",
        }:
            return "orders"

        return None

    # -----------------------------------------
    # CANCEL ORDER
    # -----------------------------------------

    if re_match_order_cancel(path):

        if method == "POST":
            return "orders"

        return None

    # -----------------------------------------
    # GET SINGLE ORDER
    # -----------------------------------------

    if re_match_order_id(path):

        if method == "GET":
            return "orders"

        return None

    return None


def re_match_order_id(path):
    import re

    return (
        re.match(
            r"^/orders/(\d+)$",
            path,
        )
        is not None
    )


def re_match_order_cancel(path):
    import re

    return (
        re.match(
            r"^/orders/(\d+)/cancel$",
            path,
        )
        is not None
    )


def role_allows(role, resource):

    # ADMIN
    if role == "admin":
        return resource in {
            "products",
            "orders",
        }

    # PRODUCT MANAGER
    if role == "products":
        return resource == "products"

    # CUSTOMER / ORDER USER
    if role == "orders":
        return resource == "orders"

    return False


def route_target(path):

    if (
        path == "/products"
        or path.startswith("/products/")
    ):
        return PRODUCT_LAMBDA_NAME

    if (
        path == "/orders"
        or path.startswith("/orders/")
    ):
        return ORDER_LAMBDA_NAME

    return None


def lambda_handler(event, context):

    # -----------------------------------------
    # 1. Extract token
    # -----------------------------------------

    incoming_token = extract_bearer_token(
        event
    )

    if not incoming_token:

        log(
            "WARN",
            "Request missing Authorization header",
        )

        return unauthorized()

    # -----------------------------------------
    # 2. Validate token
    # -----------------------------------------

    try:

        role = get_role_for_token(
            incoming_token
        )

    except Exception:

        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(
                {
                    "error": "internal_error",
                    "message": "Auth check failed",
                }
            ),
        }

    # -----------------------------------------
    # 3. Invalid token
    # -----------------------------------------

    if role is None:

        log(
            "WARN",
            "Request had an invalid token",
        )

        return unauthorized()

    # -----------------------------------------
    # 4. Get request details
    # -----------------------------------------

    method, path = get_request_details(
        event
    )

    # -----------------------------------------
    # 5. Determine permission
    # -----------------------------------------

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

        return {
            "statusCode": 404,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(
                {
                    "error": "not_found",
                    "message": "No matching route",
                }
            ),
        }

    # -----------------------------------------
    # 6. RBAC authorization
    # -----------------------------------------

    if not role_allows(
        role,
        permission,
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

    # -----------------------------------------
    # 7. Determine Lambda
    # -----------------------------------------

    target_function = route_target(
        path
    )

    if not target_function:

        return {
            "statusCode": 404,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(
                {
                    "error": "not_found",
                    "message": "No matching route",
                }
            ),
        }

    # -----------------------------------------
    # 8. Pass authenticated role downstream
    # -----------------------------------------

    event.setdefault(
        "requestContext",
        {}
    ).setdefault(
        "authorizer",
        {}
    )

    event["requestContext"][
        "authorizer"
    ]["role"] = role

    log(
        "INFO",
        "RBAC authorized, invoking downstream Lambda",
        role=role,
        method=method,
        path=path,
        target=target_function,
    )

    # -----------------------------------------
    # 9. Invoke Product / Order Lambda
    # -----------------------------------------

    try:

        response = lambda_client.invoke(
            FunctionName=target_function,
            InvocationType="RequestResponse",
            Payload=json.dumps(
                event
            ).encode("utf-8"),
        )

        payload = json.loads(
            response[
                "Payload"
            ].read()
        )

        return payload

    except Exception as e:

        log(
            "ERROR",
            "Failed to invoke downstream Lambda",
            error=str(e),
            target=target_function,
        )

        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(
                {
                    "error": "internal_error",
                    "message": "Downstream service failed",
                }
            ),
        }
