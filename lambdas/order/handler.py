import json
import os
import re

import boto3
import pymysql
from botocore.config import Config


# ============================================================
# AWS CLIENT CONFIGURATION
# ============================================================

aws_config = Config(
    connect_timeout=2,
    read_timeout=2,
    retries={
        "max_attempts": 1
    }
)

ssm = boto3.client(
    "ssm",
    config=aws_config
)

events_client = boto3.client(
    "events",
    config=aws_config
)

cloudwatch = boto3.client(
    "cloudwatch",
    config=aws_config
)


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME")
LOW_STOCK_THRESHOLD = int(
    os.environ.get("LOW_STOCK_THRESHOLD", "10")
)


# ============================================================
# LOGGING
# ============================================================

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
# SSM PARAMETER
# ============================================================

def get_param(name, decrypt=False):
    return ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )["Parameter"]["Value"]


# ============================================================
# DATABASE CREDENTIAL CACHE
# ============================================================

_cached_username = None
_cached_password = None


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    """
    Create a fresh MySQL connection for each Lambda invocation.

    Username and password are cached between warm invocations,
    but the database connection itself is always fresh.
    """

    global _cached_username, _cached_password

    host = os.environ.get("DB_HOST")

    if not host:
        raise RuntimeError("DB_HOST environment variable is not configured")

    if _cached_username is None:
        _cached_username = get_param(
            f"/cloudmart/{ENVIRONMENT}/db/username",
            decrypt=True
        )

    if _cached_password is None:
        _cached_password = get_param(
            f"/cloudmart/{ENVIRONMENT}/db/password",
            decrypt=True
        )

    dbname = os.environ.get(
        "DB_NAME",
        "cloudmart"
    )

    return pymysql.connect(
        host=host,
        user=_cached_username,
        password=_cached_password,
        db=dbname,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=5,
        read_timeout=8,
        write_timeout=8
    )


# ============================================================
# HTTP RESPONSE
# ============================================================

def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(
            body,
            default=str
        )
    }


# ============================================================
# ORDER EVENT
# ============================================================

def publish_order_event(
    detail_type,
    order_id,
    customer_id,
    status,
    extra=None
):
    detail = {
        "orderId": order_id,
        "customerId": customer_id,
        "status": status
    }

    if extra:
        detail.update(extra)

    try:

        result = events_client.put_events(
            Entries=[
                {
                    "Source": "cloudmart.orders",
                    "DetailType": detail_type,
                    "EventBusName": EVENT_BUS_NAME,
                    "Detail": json.dumps(detail)
                }
            ]
        )

        failed_count = result.get(
            "FailedEntryCount",
            0
        )

        if failed_count > 0:

            log(
                "ERROR",
                f"Failed to publish {detail_type} event",
                orderId=order_id,
                status=status,
                result=result
            )

        else:

            log(
                "INFO",
                f"Published {detail_type} event",
                orderId=order_id,
                status=status
            )

    except Exception as e:

        log(
            "ERROR",
            "Failed to publish order event",
            error=str(e),
            detailType=detail_type
        )


# ============================================================
# INVENTORY EVENT
# ============================================================

def publish_inventory_event(
    product_id,
    stock_count
):
    try:

        result = events_client.put_events(
            Entries=[
                {
                    "Source": "cloudmart.inventory",
                    "DetailType": "InventoryChanged",
                    "EventBusName": EVENT_BUS_NAME,
                    "Detail": json.dumps(
                        {
                            "productId": product_id,
                            "stockCount": stock_count
                        }
                    )
                }
            ]
        )

        failed_count = result.get(
            "FailedEntryCount",
            0
        )

        if failed_count > 0:

            log(
                "ERROR",
                "Failed to publish inventory event",
                productId=product_id,
                stockCount=stock_count,
                result=result
            )

        else:

            log(
                "INFO",
                "Published InventoryChanged event",
                productId=product_id,
                stockCount=stock_count
            )

    except Exception as e:

        log(
            "ERROR",
            "Failed to publish inventory event",
            error=str(e)
        )


# ============================================================
# CLOUDWATCH METRIC
# ============================================================

def publish_metric(
    metric_name,
    dimensions=None
):
    try:

        cloudwatch.put_metric_data(
            Namespace="cloudmart",
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {
                            "Name": key,
                            "Value": value
                        }
                        for key, value in (
                            dimensions or {}
                        ).items()
                    ]
                }
            ]
        )

    except Exception as e:

        log(
            "ERROR",
            "Failed to publish metric",
            error=str(e),
            metric=metric_name
        )


# ============================================================
# ORDER HISTORY
# ============================================================

def write_order_history(
    cursor,
    order_id,
    previous_status,
    new_status
):
    cursor.execute(
        """
        INSERT INTO order_history
        (
            order_id,
            previous_status,
            new_status,
            changed_by
        )
        VALUES (%s, %s, %s, %s)
        """,
        (
            order_id,
            previous_status,
            new_status,
            "order-lambda"
        )
    )


# ============================================================
# PLACE ORDER
# ============================================================

def place_order(body):

    customer_id = body.get("customer_id")
    items = body.get("items")

    # --------------------------------------------------------
    # Validate request
    # --------------------------------------------------------

    if (
        not customer_id
        or not items
        or not isinstance(items, list)
        or len(items) == 0
    ):
        return response(
            400,
            {
                "error": "validation_error",
                "message": (
                    "customer_id and a non-empty "
                    "items array are required"
                )
            }
        )

    for item in items:

        if (
            "product_id" not in item
            or "quantity" not in item
            or item["quantity"] <= 0
        ):

            return response(
                400,
                {
                    "error": "validation_error",
                    "message": (
                        "Each item needs product_id "
                        "and a positive quantity"
                    )
                }
            )

    conn = None

    try:

        # ----------------------------------------------------
        # Connect to database
        # ----------------------------------------------------

        conn = get_connection()

        with conn.cursor() as cur:

            order_items_data = []
            total_amount = 0

            # ------------------------------------------------
            # Check products and stock
            # ------------------------------------------------

            for item in items:

                cur.execute(
                    """
                    SELECT
                        product_id,
                        name,
                        price,
                        stock_count
                    FROM products
                    WHERE product_id = %s
                      AND is_active = TRUE
                    FOR UPDATE
                    """,
                    (
                        item["product_id"],
                    )
                )

                product = cur.fetchone()

                # --------------------------------------------
                # Product not found
                # --------------------------------------------

                if not product:

                    conn.rollback()

                    publish_metric(
                        "OrdersFailed",
                        {
                            "Environment": ENVIRONMENT,
                            "FailureReason": "PRODUCT_NOT_FOUND"
                        }
                    )

                    publish_order_event(
                        "OrderFailed",
                        None,
                        customer_id,
                        "FAILED",
                        {
                            "reason": "PRODUCT_NOT_FOUND"
                        }
                    )

                    return response(
                        400,
                        {
                            "error": "validation_error",
                            "message": (
                                f"Product "
                                f"{item['product_id']} "
                                f"not found"
                            )
                        }
                    )

                # --------------------------------------------
                # Insufficient stock
                # --------------------------------------------

                if product["stock_count"] < item["quantity"]:

                    conn.rollback()

                    publish_metric(
                        "OrdersFailed",
                        {
                            "Environment": ENVIRONMENT,
                            "FailureReason": "INSUFFICIENT_STOCK"
                        }
                    )

                    publish_order_event(
                        "OrderFailed",
                        None,
                        customer_id,
                        "FAILED",
                        {
                            "reason": "INSUFFICIENT_STOCK",
                            "productId": item["product_id"]
                        }
                    )

                    log(
                        "INFO",
                        "Order failed - insufficient stock",
                        productId=item["product_id"]
                    )

                    return response(
                        409,
                        {
                            "error": "insufficient_stock",
                            "message": (
                                f"Not enough stock for "
                                f"product {item['product_id']}"
                            )
                        }
                    )

                # --------------------------------------------
                # Calculate order total
                # --------------------------------------------

                unit_price = float(
                    product["price"]
                )

                order_items_data.append(
                    {
                        "product_id": product["product_id"],
                        "product_name_snapshot": product["name"],
                        "quantity": item["quantity"],
                        "unit_price": unit_price
                    }
                )

                total_amount += (
                    unit_price *
                    item["quantity"]
                )

            # ------------------------------------------------
            # Create order as PENDING
            # ------------------------------------------------

            cur.execute(
                """
                INSERT INTO orders
                (
                    customer_id,
                    status,
                    total_amount
                )
                VALUES (%s, %s, %s)
                """,
                (
                    customer_id,
                    "PENDING",
                    total_amount
                )
            )

            order_id = cur.lastrowid

            # ------------------------------------------------
            # Write PENDING history
            # ------------------------------------------------

            write_order_history(
                cur,
                order_id,
                None,
                "PENDING"
            )

            # ------------------------------------------------
            # Deduct stock and create order items
            # ------------------------------------------------

            for oi in order_items_data:

                cur.execute(
                    """
                    UPDATE products
                    SET stock_count =
                        stock_count - %s
                    WHERE product_id = %s
                    """,
                    (
                        oi["quantity"],
                        oi["product_id"]
                    )
                )

                # IMPORTANT:
                # Read remaining stock BEFORE COMMIT.
                cur.execute(
                    """
                    SELECT stock_count
                    FROM products
                    WHERE product_id = %s
                    """,
                    (
                        oi["product_id"],
                    )
                )

                stock_result = cur.fetchone()

                remaining = stock_result[
                    "stock_count"
                ]

                # Save it in memory.
                # No DB query will be needed after COMMIT.
                oi["remaining_stock"] = remaining

                # --------------------------------------------
                # Create order item
                # --------------------------------------------

                cur.execute(
                    """
                    INSERT INTO order_items
                    (
                        order_id,
                        product_id,
                        product_name_snapshot,
                        quantity,
                        unit_price
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        order_id,
                        oi["product_id"],
                        oi["product_name_snapshot"],
                        oi["quantity"],
                        oi["unit_price"]
                    )
                )

            # ------------------------------------------------
            # Confirm order
            # ------------------------------------------------

            cur.execute(
                """
                UPDATE orders
                SET status = %s
                WHERE order_id = %s
                """,
                (
                    "CONFIRMED",
                    order_id
                )
            )

            # ------------------------------------------------
            # Write CONFIRMED history
            # ------------------------------------------------

            write_order_history(
                cur,
                order_id,
                "PENDING",
                "CONFIRMED"
            )

            # ------------------------------------------------
            # COMMIT TRANSACTION
            # ------------------------------------------------

            conn.commit()

        # ====================================================
        # DATABASE TRANSACTION IS COMPLETE
        # ====================================================

        log(
            "INFO",
            "Order transaction committed",
            orderId=order_id,
            totalAmount=total_amount
        )

        # ----------------------------------------------------
        # Publish order events
        # ----------------------------------------------------

        publish_order_event(
            "OrderPlaced",
            order_id,
            customer_id,
            "PENDING"
        )

        publish_order_event(
            "OrderConfirmed",
            order_id,
            customer_id,
            "CONFIRMED",
            {
                "totalAmount": total_amount
            }
        )

        # ----------------------------------------------------
        # Low-stock notification
        #
        # IMPORTANT:
        # We use the stock value already captured BEFORE
        # commit. No DB query is performed here.
        # ----------------------------------------------------

        for oi in order_items_data:

            remaining = oi["remaining_stock"]

            if remaining < LOW_STOCK_THRESHOLD:

                publish_inventory_event(
                    oi["product_id"],
                    remaining
                )

        # ----------------------------------------------------
        # SUCCESS RESPONSE
        # ----------------------------------------------------

        log(
            "INFO",
            "Order confirmed - returning response",
            orderId=order_id,
            totalAmount=total_amount
        )

        return response(
            201,
            {
                "order_id": order_id,
                "status": "CONFIRMED",
                "total_amount": total_amount
            }
        )

    # ========================================================
    # DATABASE ERROR
    # ========================================================

    except pymysql.Error as e:

        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        log(
            "ERROR",
            "Database error during order placement",
            error=str(e)
        )

        publish_metric(
            "OrdersFailed",
            {
                "Environment": ENVIRONMENT,
                "FailureReason": "DB_UNAVAILABLE"
            }
        )

        publish_order_event(
            "OrderFailed",
            None,
            customer_id,
            "FAILED",
            {
                "reason": "DB_UNAVAILABLE"
            }
        )

        return response(
            500,
            {
                "error": "internal_error",
                "message": "Database error"
            }
        )

    # ========================================================
    # GENERAL ERROR
    # ========================================================

    except Exception as e:

        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        log(
            "ERROR",
            "Unhandled exception during order placement",
            error=str(e)
        )

        publish_metric(
            "OrdersFailed",
            {
                "Environment": ENVIRONMENT,
                "FailureReason": "INTERNAL_ERROR"
            }
        )

        publish_order_event(
            "OrderFailed",
            None,
            customer_id,
            "FAILED",
            {
                "reason": "INTERNAL_ERROR"
            }
        )

        return response(
            500,
            {
                "error": "internal_error",
                "message": "Unexpected error"
            }
        )

    # ========================================================
    # CLOSE CONNECTION
    # ========================================================

    finally:

        if conn:

            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# GET ORDER
# ============================================================

def get_order(order_id):

    conn = get_connection()

    try:

        with conn.cursor() as cur:

            # ------------------------------------------------
            # Get order
            # ------------------------------------------------

            cur.execute(
                """
                SELECT *
                FROM orders
                WHERE order_id = %s
                """,
                (
                    order_id,
                )
            )

            order = cur.fetchone()

            if not order:

                return response(
                    404,
                    {
                        "error": "not_found",
                        "message": "Order not found"
                    }
                )

            # ------------------------------------------------
            # Get order items
            # ------------------------------------------------

            cur.execute(
                """
                SELECT *
                FROM order_items
                WHERE order_id = %s
                """,
                (
                    order_id,
                )
            )

            order["items"] = cur.fetchall()

            # ------------------------------------------------
            # Get order history
            # ------------------------------------------------

            cur.execute(
                """
                SELECT *
                FROM order_history
                WHERE order_id = %s
                ORDER BY changed_at
                """,
                (
                    order_id,
                )
            )

            order["history"] = cur.fetchall()

        return response(
            200,
            order
        )

    finally:

        conn.close()


# ============================================================
# LIST ORDERS BY CUSTOMER
# ============================================================

def list_orders_by_customer(customer_id):

    conn = get_connection()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM orders
                WHERE customer_id = %s
                ORDER BY created_at DESC
                """,
                (
                    customer_id,
                )
            )

            orders = cur.fetchall()

        return response(
            200,
            {
                "orders": orders
            }
        )

    finally:

        conn.close()


# ============================================================
# LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):

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

    query_params = (
        event.get("queryStringParameters")
        or {}
    )

    # --------------------------------------------------------
    # Parse JSON body
    # --------------------------------------------------------

    try:

        body = (
            json.loads(event["body"])
            if event.get("body")
            else {}
        )

    except json.JSONDecodeError:

        return response(
            400,
            {
                "error": "validation_error",
                "message": "Invalid JSON body"
            }
        )

    # --------------------------------------------------------
    # Match /orders/{id}
    # --------------------------------------------------------

    id_match = re.match(
        r"^/orders/(\d+)$",
        path
    )

    # --------------------------------------------------------
    # POST /orders
    # --------------------------------------------------------

    if (
        method == "POST"
        and path == "/orders"
    ):

        return place_order(body)

    # --------------------------------------------------------
    # GET /orders/{id}
    # --------------------------------------------------------

    elif (
        method == "GET"
        and id_match
    ):

        return get_order(
            int(id_match.group(1))
        )

    # --------------------------------------------------------
    # GET /orders?customerId=1
    # --------------------------------------------------------

    elif (
        method == "GET"
        and path == "/orders"
        and "customerId" in query_params
    ):

        try:

            customer_id = int(
                query_params["customerId"]
            )

        except (ValueError, TypeError):

            return response(
                400,
                {
                    "error": "validation_error",
                    "message": "customerId must be an integer"
                }
            )

        return list_orders_by_customer(
            customer_id
        )

    # --------------------------------------------------------
    # Route not found
    # --------------------------------------------------------

    else:

        return response(
            404,
            {
                "error": "not_found",
                "message": "No matching route"
            }
        )
