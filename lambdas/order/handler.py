import json
import os
import re
import boto3
import pymysql

ssm = boto3.client("ssm")
events_client = boto3.client("events")
cloudwatch = boto3.client("cloudwatch")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME")
LOW_STOCK_THRESHOLD = int(os.environ.get("LOW_STOCK_THRESHOLD", "10"))


def log(level, message, **extra):
    print(json.dumps({"level": level, "message": message, **extra}))


def get_param(name, decrypt=False):
    return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]


def get_connection():
    """A fresh connection per invocation — never cached globally.
    Caching a connection across invocations is risky for transactional
    code like this: if an invocation is ever killed mid-transaction
    (timeout, crash), a cached connection would silently carry a stuck,
    uncommitted transaction — and its row lock — into the next call."""
    host = os.environ.get("DB_HOST")
    username = get_param(f"/cloudmart/{ENVIRONMENT}/db/username", decrypt=True)
    password = get_param(f"/cloudmart/{ENVIRONMENT}/db/password", decrypt=True)
    dbname = os.environ.get("DB_NAME", "cloudmart")

    return pymysql.connect(
        host=host, user=username, password=password, db=dbname,
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
        connect_timeout=5, read_timeout=8, write_timeout=8
    )


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str)
    }


def publish_order_event(detail_type, order_id, customer_id, status, extra=None):
    detail = {"orderId": order_id, "customerId": customer_id, "status": status}
    if extra:
        detail.update(extra)
    try:
        events_client.put_events(Entries=[{
            "Source": "cloudmart.orders",
            "DetailType": detail_type,
            "EventBusName": EVENT_BUS_NAME,
            "Detail": json.dumps(detail)
        }])
        log("INFO", f"Published {detail_type} event", orderId=order_id, status=status)
    except Exception as e:
        log("ERROR", "Failed to publish order event", error=str(e), detailType=detail_type)


def publish_inventory_event(product_id, stock_count):
    try:
        events_client.put_events(Entries=[{
            "Source": "cloudmart.inventory",
            "DetailType": "InventoryChanged",
            "EventBusName": EVENT_BUS_NAME,
            "Detail": json.dumps({"productId": product_id, "stockCount": stock_count})
        }])
    except Exception as e:
        log("ERROR", "Failed to publish inventory event", error=str(e))


def publish_metric(metric_name, dimensions=None):
    try:
        cloudwatch.put_metric_data(
            Namespace="cloudmart",
            MetricData=[{
                "MetricName": metric_name,
                "Value": 1,
                "Unit": "Count",
                "Dimensions": [{"Name": k, "Value": v} for k, v in (dimensions or {}).items()]
            }]
        )
    except Exception as e:
        log("ERROR", "Failed to publish metric", error=str(e), metric=metric_name)


def write_order_history(cursor, order_id, previous_status, new_status):
    cursor.execute(
        "INSERT INTO order_history (order_id, previous_status, new_status, changed_by) VALUES (%s, %s, %s, %s)",
        (order_id, previous_status, new_status, "order-lambda")
    )


def place_order(body):
    customer_id = body.get("customer_id")
    items = body.get("items")

    if not customer_id or not items or not isinstance(items, list) or len(items) == 0:
        return response(400, {"error": "validation_error", "message": "customer_id and a non-empty items array are required"})

    for item in items:
        if "product_id" not in item or "quantity" not in item or item["quantity"] <= 0:
            return response(400, {"error": "validation_error", "message": "Each item needs product_id and a positive quantity"})

    conn = get_connection()
    try:
        with conn.cursor() as cur:            # Lock and check stock for every item before writing anything
            order_items_data = []
            total_amount = 0
            for item in items:
                cur.execute(
                    "SELECT product_id, name, price, stock_count FROM products "
                    "WHERE product_id = %s AND is_active = TRUE FOR UPDATE",
                    (item["product_id"],)
                )
                product = cur.fetchone()
                if not product:
                    conn.rollback()
                    publish_metric("OrdersFailed", {"Environment": ENVIRONMENT, "FailureReason": "PRODUCT_NOT_FOUND"})
                    return response(400, {"error": "validation_error", "message": f"Product {item['product_id']} not found"})

                if product["stock_count"] < item["quantity"]:
                    conn.rollback()
                    publish_metric("OrdersFailed", {"Environment": ENVIRONMENT, "FailureReason": "INSUFFICIENT_STOCK"})
                    log("INFO", "Order failed - insufficient stock", productId=item["product_id"])
                    return response(409, {
                        "error": "insufficient_stock",
                        "message": f"Not enough stock for product {item['product_id']}"
                    })

                unit_price = float(product["price"])
                order_items_data.append({
                    "product_id": product["product_id"],
                    "product_name_snapshot": product["name"],
                    "quantity": item["quantity"],
                    "unit_price": unit_price
                })
                total_amount += unit_price * item["quantity"]

            # All items available — create the order
            cur.execute(
                "INSERT INTO orders (customer_id, status, total_amount) VALUES (%s, %s, %s)",
                (customer_id, "PENDING", total_amount)
            )
            order_id = cur.lastrowid
            write_order_history(cur, order_id, None, "PENDING")

            # Deduct stock and write order_items
            for oi in order_items_data:
                cur.execute(
                    "UPDATE products SET stock_count = stock_count - %s WHERE product_id = %s",
                    (oi["quantity"], oi["product_id"])
                )
                cur.execute(
                    "INSERT INTO order_items (order_id, product_id, product_name_snapshot, quantity, unit_price) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (order_id, oi["product_id"], oi["product_name_snapshot"], oi["quantity"], oi["unit_price"])
                )

            # Confirm the order
            cur.execute("UPDATE orders SET status = %s WHERE order_id = %s", ("CONFIRMED", order_id))
            write_order_history(cur, order_id, "PENDING", "CONFIRMED")

            conn.commit()

        # Post-commit: events, metrics, low-stock check
        publish_order_event("OrderPlaced", order_id, customer_id, "PENDING")
        publish_order_event("OrderConfirmed", order_id, customer_id, "CONFIRMED", {"totalAmount": total_amount})
        publish_metric("OrdersPlaced", {"Environment": ENVIRONMENT})

        for oi in order_items_data:
            with conn.cursor() as cur:
                cur.execute("SELECT stock_count FROM products WHERE product_id = %s", (oi["product_id"],))
                remaining = cur.fetchone()["stock_count"]
            if remaining < LOW_STOCK_THRESHOLD:
                publish_inventory_event(oi["product_id"], remaining)

        log("INFO", "Order confirmed", orderId=order_id, totalAmount=total_amount)
        return response(201, {"order_id": order_id, "status": "CONFIRMED", "total_amount": total_amount})

    except pymysql.Error as e:
        conn.rollback()
        log("ERROR", "Database error during order placement", error=str(e))
        publish_metric("OrdersFailed", {"Environment": ENVIRONMENT, "FailureReason": "DB_UNAVAILABLE"})
        return response(500, {"error": "internal_error", "message": "Database error"})
    except Exception as e:
        conn.rollback()
        log("ERROR", "Unhandled exception during order placement", error=str(e))
        publish_metric("OrdersFailed", {"Environment": ENVIRONMENT, "FailureReason": "INTERNAL_ERROR"})
        return response(500, {"error": "internal_error", "message": "Unexpected error"})
    finally:
        conn.close()


def get_order(order_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()
            if not order:
                return response(404, {"error": "not_found", "message": "Order not found"})

            cur.execute("SELECT * FROM order_items WHERE order_id = %s", (order_id,))
            order["items"] = cur.fetchall()

            cur.execute("SELECT * FROM order_history WHERE order_id = %s ORDER BY changed_at", (order_id,))
            order["history"] = cur.fetchall()

        return response(200, order)
    finally:
        conn.close()


def list_orders_by_customer(customer_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE customer_id = %s ORDER BY created_at DESC", (customer_id,))
            orders = cur.fetchall()
        return response(200, {"orders": orders})
    finally:
        conn.close()


def lambda_handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "")
    path = http.get("path", "")
    query_params = event.get("queryStringParameters") or {}

    try:
        body = json.loads(event["body"]) if event.get("body") else {}
    except json.JSONDecodeError:
        return response(400, {"error": "validation_error", "message": "Invalid JSON body"})

    id_match = re.match(r"^/orders/(\d+)$", path)

    if method == "POST" and path == "/orders":
        return place_order(body)
    elif method == "GET" and id_match:
        return get_order(int(id_match.group(1)))
    elif method == "GET" and path == "/orders" and "customerId" in query_params:
        return list_orders_by_customer(int(query_params["customerId"]))
    else:
        return response(404, {"error": "not_found", "message": "No matching route"})
