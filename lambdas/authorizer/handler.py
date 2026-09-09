import json
import os
import time
import boto3

ssm = boto3.client("ssm")
lambda_client = boto3.client("lambda")

CACHE_TTL = 300

TOKEN_CACHE = {}


ADMIN_TOKEN_PARAMETER_NAME = os.environ[
    "ADMIN_TOKEN_PARAMETER_NAME"
]

PRODUCTS_TOKEN_PARAMETER_NAME = os.environ[
    "PRODUCTS_TOKEN_PARAMETER_NAME"
]

ORDERS_TOKEN_PARAMETER_NAME = os.environ[
    "ORDERS_TOKEN_PARAMETER_NAME"
]

PRODUCT_LAMBDA_NAME = os.environ[
    "PRODUCT_LAMBDA_NAME"
]

ORDER_LAMBDA_NAME = os.environ[
    "ORDER_LAMBDA_NAME"
]


def get_token(parameter_name):
    now = time.time()

    cached = TOKEN_CACHE.get(parameter_name)

    if cached:
        token, expires_at = cached

        if now < expires_at:
            return token

    response = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True
    )

    token = response["Parameter"]["Value"]

    TOKEN_CACHE[parameter_name] = (
        token,
        now + CACHE_TTL
    )

    return token


def get_request(event):
    request_context = event.get("requestContext", {})

    http = request_context.get("http", {})

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


def get_authorization_token(event):
    headers = event.get("headers") or {}

    authorization = (
        headers.get("authorization")
        or headers.get("Authorization")
        or ""
    )

    if not authorization:
        return None

    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()

    return authorization.strip()


def identify_role(token):
    if not token:
        return None

    if token == get_token(ADMIN_TOKEN_PARAMETER_NAME):
        return "admin"

    if token == get_token(PRODUCTS_TOKEN_PARAMETER_NAME):
        return "products"

    if token == get_token(ORDERS_TOKEN_PARAMETER_NAME):
        return "orders"

    return None


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


def role_allowed(role, method, path):

    # ----------------------------------------------------------
    # ADMIN
    # ----------------------------------------------------------
    if role == "admin":
        return (
            is_product_route(path)
            or is_order_route(path)
        )

    # ----------------------------------------------------------
    # PRODUCTS ROLE
    # ----------------------------------------------------------
    if role == "products":
        if not is_product_route(path):
            return False

        return method in {
            "GET",
            "POST",
            "PUT",
            "DELETE"
        }

    # ----------------------------------------------------------
    # ORDERS ROLE
    # ----------------------------------------------------------
    if role == "orders":

        if not is_order_route(path):
            return False

        return method in {
            "GET",
            "POST"
        }

    return False


def get_target_lambda(path):

    if is_product_route(path):
        return PRODUCT_LAMBDA_NAME

    if is_order_route(path):
        return ORDER_LAMBDA_NAME

    return None


def invoke_lambda(function_name, event):

    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode()
    )

    payload = response.get("Payload")

    if payload:
        return json.loads(
            payload.read().decode()
        )

    return {
        "statusCode": 500,
        "body": json.dumps({
            "error": "Empty Lambda response"
        })
    }


def response(status_code, body):

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body)
    }


def lambda_handler(event, context):

    try:

        method, path = get_request(event)

        print(
            f"Incoming request: "
            f"{method} {path}"
        )

        token = get_authorization_token(event)

        if not token:
            print("Authentication failed: no token")

            return response(
                401,
                {
                    "error": "Unauthorized"
                }
            )

        role = identify_role(token)

        if not role:
            print("Authentication failed: invalid token")

            return response(
                401,
                {
                    "error": "Unauthorized"
                }
            )

        print(
            f"Authenticated role: {role}"
        )

        if not role_allowed(
            role,
            method,
            path
        ):
            print(
                f"Authorization denied: "
                f"{role} -> {method} {path}"
            )

            return response(
                403,
                {
                    "error": "Forbidden",
                    "message":
                        "You do not have permission "
                        "to perform this operation"
                }
            )

        target_lambda = get_target_lambda(path)

        if not target_lambda:

            return response(
                404,
                {
                    "error": "Route not found"
                }
            )

        # Add RBAC information for downstream Lambda.
        event.setdefault(
            "requestContext",
            {}
        )

        event["requestContext"][
            "authorizer"
        ] = {
            "role": role
        }

        print(
            f"Invoking downstream Lambda: "
            f"{target_lambda}"
        )

        result = invoke_lambda(
            target_lambda,
            event
        )

        if isinstance(result, dict):
            return result

        return response(
            200,
            result
        )

    except Exception as exc:

        print(
            f"Authorizer error: {exc}"
        )

        return response(
            500,
            {
                "error": "Internal server error"
            }
        )
