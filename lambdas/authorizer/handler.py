import json
import os
import time
import boto3

ssm = boto3.client("ssm")
lambda_client = boto3.client("lambda")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")
TOKEN_PARAM = os.environ.get("AUTH_TOKEN_PARAM", f"/cloudmart/{ENVIRONMENT}/auth/token")
PRODUCT_LAMBDA_NAME = os.environ.get("PRODUCT_LAMBDA_NAME")
ORDER_LAMBDA_NAME = os.environ.get("ORDER_LAMBDA_NAME")  # not yet deployed — routes to Product only until it exists

CACHE_TTL_SECONDS = 300

# Module-level cache — persists across warm invocations of the same
# execution environment, cleared on cold start.
_token_cache = {"value": None, "fetched_at": 0}


def log(level, message, **extra):
    print(json.dumps({"level": level, "message": message, **extra}))


def get_valid_token():
    now = time.time()
    if _token_cache["value"] is not None and (now - _token_cache["fetched_at"]) < CACHE_TTL_SECONDS:
        return _token_cache["value"]

    response = ssm.get_parameter(Name=TOKEN_PARAM, WithDecryption=True)
    _token_cache["value"] = response["Parameter"]["Value"]
    _token_cache["fetched_at"] = now
    return _token_cache["value"]


def unauthorized():
    return {
        "statusCode": 401,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "unauthorized", "message": "Missing or invalid token"})
    }


def extract_bearer_token(event):
    headers = event.get("headers", {}) or {}
    # Lambda Function URL headers arrive lowercase
    auth_header = headers.get("authorization") or headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    return auth_header[len("Bearer "):].strip()


def route_target(event):
    """Decide which downstream Lambda handles this request based on path."""
    path = event.get("requestContext", {}).get("http", {}).get("path", "")
    if path.startswith("/products"):
        return PRODUCT_LAMBDA_NAME
    if path.startswith("/orders"):
        return ORDER_LAMBDA_NAME
    return None


def lambda_handler(event, context):
    incoming_token = extract_bearer_token(event)

    if not incoming_token:
        log("WARN", "Request missing Authorization header")
        return unauthorized()

    try:
        valid_token = get_valid_token()
    except Exception as e:
        log("ERROR", "Failed to fetch auth token from SSM", error=str(e))
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "internal_error", "message": "Auth check failed"})
        }

    if incoming_token != valid_token:
        # Deliberately generic — never reveal whether the token was
        # missing, malformed, or simply wrong.
        log("WARN", "Request had an invalid token")
        return unauthorized()

    target_function = route_target(event)
    if not target_function:
        log("WARN", "No route matched for path", path=event.get("requestContext", {}).get("http", {}).get("path"))
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "not_found", "message": "No matching route"})
        }

    log("INFO", "Token valid, invoking downstream Lambda", target=target_function)

    try:
        response = lambda_client.invoke(
            FunctionName=target_function,
            InvocationType="RequestResponse",
            Payload=json.dumps(event).encode("utf-8")
        )
        payload = json.loads(response["Payload"].read())
        return payload
    except Exception as e:
        log("ERROR", "Failed to invoke downstream Lambda", error=str(e), target=target_function)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "internal_error", "message": "Downstream service failed"})
        }
