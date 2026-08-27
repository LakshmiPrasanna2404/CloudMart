import json
import os
import re
import boto3
import pymysql

ssm = boto3.client("ssm")
events_client = boto3.client("events")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME")
LOW_STOCK_THRESHOLD = int(os.environ.get("LOW_STOCK_THRESHOLD", "10"))

_db_conn = None


def log(level, message, **extra):
    print(json.dumps({"level": level, "message": message, **extra}))


def get_param(name, decrypt=False):
    return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]


def get_connection():
    global _db_conn
    if _db_conn and _db_conn.open:
        return _db_conn

    host = os.environ.get("DB_HOST")
    username = get_param(f"/cloudmart/{ENVIRONMENT}/db/username", decrypt=True)
    password = get_param(f"/cloudmart/{ENVIRONMENT}/db/password", decrypt=True)
    dbname = os.environ.get("DB_NAME", "cloudmart")

    _db_conn = pymysql.connect(
        host=host, user=username, password=password, db=dbname,
        cursorclass=pymysql.cursors.DictCursor, autocommit=True, connect_timeout=5
    )
    return _db_conn


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str)
    }


def publish_inventory_event(product_id, stock_count):
    try:
        events_client.put_events(Entries=[{
            "Source": "cloudmart.inventory",
            "DetailType": "InventoryChanged",
            "EventBusName": EVENT_BUS_NAME,
            "Detail": json.dumps({"productId": product_id, "stockCount": stock_count})
        }])
        log("INFO", "Published InventoryChanged event", productId=product_id, stockCount=stock_count)
    except Exception as e:
        log("ERROR", "Failed to publish inventory event", error=str(e))


def create_product(body):
    for field in ["name", "price", "stock_count"]:
        if field not in body:
            return response(400, {"error": "validation_error", "message": f"Missing field: {field}"})

    if body["price"] < 0 or body["stock_count"] < 0:
        return response(400, {"error": "validation_error", "message": "price and stock_count must be non-negative"})

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO products (name, description, price, category, stock_count) VALUES (%s, %s, %s, %s, %s)",
            (body["name"], body.get("description"), body["price"], body.get("category"), body["stock_count"])
        )
        product_id = cur.lastrowid

    publish_inventory_event(product_id, body["stock_count"])
    log("INFO", "Product created", productId=product_id)
    return response(201, {"product_id": product_id, **body})


def list_products(query_params):
    conn = get_connection()
    category = (query_params or {}).get("category")
    with conn.cursor() as cur:
        if category:
            cur.execute("SELECT * FROM products WHERE is_active = TRUE AND category = %s", (category,))
        else:
            cur.execute("SELECT * FROM products WHERE is_active = TRUE")
        products = cur.fetchall()
    return response(200, {"products": products})


def get_product(product_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM products WHERE product_id = %s AND is_active = TRUE", (product_id,))
        product = cur.fetchone()
    if not product:
        return response(404, {"error": "not_found", "message": "Product not found"})
    return response(200, product)


def update_product(product_id, body):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM products WHERE product_id = %s AND is_active = TRUE", (product_id,))
        existing = cur.fetchone()
        if not existing:
            return response(404, {"error": "not_found", "message": "Product not found"})

        allowed_fields = ["name", "description", "price", "category", "stock_count"]
        updates = {k: v for k, v in body.items() if k in allowed_fields}
        if not updates:
            return response(400, {"error": "validation_error", "message": "No valid fields to update"})

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE products SET {set_clause} WHERE product_id = %s", (*updates.values(), product_id))

        if "stock_count" in updates:
            publish_inventory_event(product_id, updates["stock_count"])

        cur.execute("SELECT * FROM products WHERE product_id = %s", (product_id,))
        updated = cur.fetchone()

    log("INFO", "Product updated", productId=product_id, fields=list(updates.keys()))
    return response(200, updated)


def delete_product(product_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT product_id FROM products WHERE product_id = %s AND is_active = TRUE", (product_id,))
        if not cur.fetchone():
            return response(404, {"error": "not_found", "message": "Product not found"})
        cur.execute("UPDATE products SET is_active = FALSE WHERE product_id = %s", (product_id,))

    log("INFO", "Product soft-deleted", productId=product_id)
    return {"statusCode": 204, "headers": {}, "body": ""}


def lambda_handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "")
    path = http.get("path", "")
    query_params = event.get("queryStringParameters") or {}

    try:
        body = json.loads(event["body"]) if event.get("body") else {}
    except json.JSONDecodeError:
        return response(400, {"error": "validation_error", "message": "Invalid JSON body"})

    id_match = re.match(r"^/products/(\d+)$", path)

    try:
        if method == "POST" and path == "/products":
            return create_product(body)
        elif method == "GET" and path == "/products":
            return list_products(query_params)
        elif method == "GET" and id_match:
            return get_product(int(id_match.group(1)))
        elif method == "PUT" and id_match:
            return update_product(int(id_match.group(1)), body)
        elif method == "DELETE" and id_match:
            return delete_product(int(id_match.group(1)))
        else:
            return response(404, {"error": "not_found", "message": "No matching route"})
    except pymysql.Error as e:
        log("ERROR", "Database error", error=str(e))
        return response(500, {"error": "internal_error", "message": "Database error"})
    except Exception as e:
        log("ERROR", "Unhandled exception", error=str(e))
        return response(500, {"error": "internal_error", "message": "Unexpected error"})
