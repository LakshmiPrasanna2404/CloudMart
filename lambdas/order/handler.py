import json
import os
import re

import boto3
import pymysql

from botocore.config import Config


# ============================================================
# AWS CLIENTS
# ============================================================

aws_config = Config(
    connect_timeout=2,
    read_timeout=3,
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


# ============================================================
# ENVIRONMENT
# ============================================================

ENVIRONMENT = os.environ.get(
    "ENVIRONMENT",
    "prod"
)


EVENT_BUS_NAME = os.environ.get(
    "EVENT_BUS_NAME"
)


DB_HOST = os.environ.get(
    "DB_HOST"
)


DB_NAME = os.environ.get(
    "DB_NAME",
    "cloudmart"
)


LOW_STOCK_THRESHOLD = int(
    os.environ.get(
        "LOW_STOCK_THRESHOLD",
        "10"
    )
)


# ============================================================
# DB CREDENTIAL CACHE
# ============================================================

_cached_username = None
_cached_password = None


# ============================================================
# RESPONSE
# ============================================================

def response(
    status_code,
    body
):

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
# LOG
# ============================================================

def log(
    level,
    message,
    **extra
):

    print(
        json.dumps(
            {
                "level": level,
                "message": message,
                **extra
            },
            default=str
        )
    )


# ============================================================
# SSM
# ============================================================

def get_param(
    name,
    decrypt=False
):

    result = ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )

    return result[
        "Parameter"
    ]["Value"]


# ============================================================
# DATABASE
# ============================================================

def get_connection():

    global _cached_username
    global _cached_password


    if not _cached_username:

        _cached_username = get_param(
            f"/cloudmart/{ENVIRONMENT}/db/username",
            decrypt=True
        )


    if not _cached_password:

        _cached_password = get_param(
            f"/cloudmart/{ENVIRONMENT}/db/password",
            decrypt=True
        )


    return pymysql.connect(
        host=DB_HOST,
        user=_cached_username,
        password=_cached_password,
        database=DB_NAME,
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False
    )


# ============================================================
# EVENTBRIDGE
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

        detail.update(
            extra
        )


    try:

        events_client.put_events(
            Entries=[
                {
                    "Source": "cloudmart.orders",

                    "DetailType": detail_type,

                    "EventBusName": EVENT_BUS_NAME,

                    "Detail": json.dumps(
                        detail
                    )
                }
            ]
        )

    except Exception as exc:

        log(
            "ERROR",
            "EventBridge order event failed",
            event_type=detail_type,
            error=str(exc)
        )


def publish_inventory_event(
    product_id,
    stock_count
):

    detail = {
        "productId": product_id,
        "stockCount": stock_count
    }


    try:

        events_client.put_events(
            Entries=[
                {
                    "Source": "cloudmart.inventory",

                    "DetailType": "InventoryChanged",

                    "EventBusName": EVENT_BUS_NAME,

                    "Detail": json.dumps(
                        detail
                    )
                }
            ]
        )

    except Exception as exc:

        log(
            "ERROR",
            "Inventory event failed",
            product_id=product_id,
            error=str(exc)
        )


# ============================================================
# PLACE ORDER
# ============================================================

def place_order(
    body,
    customer_id
):

    if not isinstance(
        body,
        dict
    ):

        return response(
            400,
            {
                "error": "validation_error",
                "message": (
                    "Request body must be JSON"
                )
            }
        )


    items = body.get(
        "items"
    )


    if (
        not isinstance(items, list)
        or not items
    ):

        return response(
            400,
            {
                "error": "validation_error",
                "message": (
                    "items must be a "
                    "non-empty array"
                )
            }
        )


    # --------------------------------------------------------
    # Normalize items
    # --------------------------------------------------------

    normalized_items = []


    for item in items:

        if not isinstance(
            item,
            dict
        ):

            return response(
                400,
                {
                    "error": "validation_error",
                    "message": (
                        "Each item must be an object"
                    )
                }
            )


        try:

            product_id = int(
                item.get(
                    "product_id"
                )
            )

            quantity = int(
                item.get(
                    "quantity"
                )
            )

        except (
            TypeError,
            ValueError
        ):

            return response(
                400,
                {
                    "error": "validation_error",
                    "message": (
                        "product_id and quantity "
                        "must be integers"
                    )
                }
            )


        if product_id <= 0:

            return response(
                400,
                {
                    "error": "validation_error",
                    "message": (
                        "product_id must be positive"
                    )
                }
            )


        if quantity <= 0:

            return response(
                400,
                {
                    "error": "validation_error",
                    "message": (
                        "quantity must be greater than zero"
                    )
                }
            )


        normalized_items.append(
            {
                "product_id": product_id,
                "quantity": quantity
            }
        )


    conn = None


    try:

        conn = get_connection()

        total_amount = 0

        locked_products = []


        with conn.cursor() as cur:

            # =================================================
            # LOCK PRODUCTS
            # =================================================

            for item in normalized_items:

                cur.execute(
                    """
                    SELECT
                        product_id,
                        name,
                        price,
                        stock_count,
                        is_active
                    FROM products
                    WHERE product_id = %s
                    FOR UPDATE
                    """,
                    (
                        item["product_id"],
                    )
                )


                product = cur.fetchone()


                if not product:

                    conn.rollback()

                    return response(
                        404,
                        {
                            "error": "product_not_found",
                            "message": (
                                f"Product "
                                f"{item['product_id']} "
                                f"not found"
                            )
                        }
                    )


                if not product[
                    "is_active"
                ]:

                    conn.rollback()

                    return response(
                        409,
                        {
                            "error": "product_inactive",
                            "message": (
                                f"Product "
                                f"{item['product_id']} "
                                f"is inactive"
                            )
                        }
                    )


                quantity = item[
                    "quantity"
                ]


                if product[
                    "stock_count"
                ] < quantity:

                    conn.rollback()

                    return response(
                        409,
                        {
                            "error": "insufficient_stock",
                            "message": (
                                f"Insufficient stock "
                                f"for product "
                                f"{item['product_id']}"
                            ),
                            "available_stock":
                                product[
                                    "stock_count"
                                ]
                        }
                    )


                line_total = (
                    product["price"]
                    * quantity
                )


                total_amount += line_total


                locked_products.append(
                    {
                        "product": product,
                        "quantity": quantity
                    }
                )


            # =================================================
            # CREATE ORDER
            # =================================================

            cur.execute(
                """
                INSERT INTO orders
                    (
                        customer_id,
                        status,
                        total_amount
                    )
                VALUES
                    (
                        %s,
                        'PENDING',
                        %s
                    )
                """,
                (
                    customer_id,
                    total_amount
                )
            )


            order_id = cur.lastrowid


            # =================================================
            # ORDER HISTORY
            # =================================================

            cur.execute(
                """
                INSERT INTO order_history
                    (
                        order_id,
                        previous_status,
                        new_status,
                        changed_by
                    )
                VALUES
                    (
                        %s,
                        NULL,
                        'PENDING',
                        'order-lambda'
                    )
                """,
                (
                    order_id,
                )
            )


            # =================================================
            # INVENTORY + ORDER ITEMS
            # =================================================

            for locked in locked_products:

                product = locked[
                    "product"
                ]

                quantity = locked[
                    "quantity"
                ]


                cur.execute(
                    """
                    UPDATE products
                    SET stock_count =
                        stock_count - %s
                    WHERE product_id = %s
                    """,
                    (
                        quantity,
                        product[
                            "product_id"
                        ]
                    )
                )


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
                    VALUES
                        (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s
                        )
                    """,
                    (
                        order_id,

                        product[
                            "product_id"
                        ],

                        product[
                            "name"
                        ],

                        quantity,

                        product[
                            "price"
                        ]
                    )
                )


            # =================================================
            # CONFIRM ORDER
            # =================================================

            cur.execute(
                """
                UPDATE orders
                SET status = 'CONFIRMED'
                WHERE order_id = %s
                """,
                (
                    order_id,
                )
            )


            cur.execute(
                """
                INSERT INTO order_history
                    (
                        order_id,
                        previous_status,
                        new_status,
                        changed_by
                    )
                VALUES
                    (
                        %s,
                        'PENDING',
                        'CONFIRMED',
                        'order-lambda'
                    )
                """,
                (
                    order_id,
                )
            )


            # =================================================
            # COMMIT
            # =================================================

            conn.commit()


        # ====================================================
        # EVENTS AFTER COMMIT
        # ====================================================

        publish_order_event(
            "OrderPlaced",
            order_id,
            customer_id,
            "PENDING",
            {
                "totalAmount":
                    float(total_amount)
            }
        )


        publish_order_event(
            "OrderConfirmed",
            order_id,
            customer_id,
            "CONFIRMED",
            {
                "totalAmount":
                    float(total_amount)
            }
        )


        for locked in locked_products:

            product = locked[
                "product"
            ]

            new_stock = (
                product["stock_count"]
                - locked["quantity"]
            )


            publish_inventory_event(
                product[
                    "product_id"
                ],
                new_stock
            )


        return response(
            201,
            {
                "order_id": order_id,

                "customer_id": customer_id,

                "status": "CONFIRMED",

                "total_amount":
                    float(total_amount),

                "items":
                    normalized_items
            }
        )


    except pymysql.Error as exc:

        if conn:

            conn.rollback()


        log(
            "ERROR",
            "Database error during order placement",
            customer_id=customer_id,
            error=str(exc)
        )


        publish_order_event(
            "OrderFailed",
            None,
            customer_id,
            "FAILED",
            {
                "reason": "DB_ERROR"
            }
        )


        return response(
            500,
            {
                "error": "internal_error",
                "message": "Database error"
            }
        )


    except Exception as exc:

        if conn:

            conn.rollback()


        log(
            "ERROR",
            "Unexpected order placement error",
            customer_id=customer_id,
            error=str(exc)
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


    finally:

        if conn:

            conn.close()


# ============================================================
# GET ORDER
# ============================================================

def get_order(
    order_id,
    customer_id,
    role
):

    conn = get_connection()


    try:

        with conn.cursor() as cur:

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
            # Customer ownership check
            # ------------------------------------------------

            if role == "customer":

                if int(
                    order["customer_id"]
                ) != int(
                    customer_id
                ):

                    return response(
                        403,
                        {
                            "error": "forbidden",
                            "message": (
                                "You can only "
                                "view your own orders"
                            )
                        }
                    )


            # ------------------------------------------------
            # Items
            # ------------------------------------------------

            cur.execute(
                """
                SELECT *
                FROM order_items
                WHERE order_id = %s
                ORDER BY order_item_id
                """,
                (
                    order_id,
                )
            )


            order["items"] = cur.fetchall()


            # ------------------------------------------------
            # History
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
# CUSTOMER ORDER LIST
# ============================================================

def list_orders_by_customer(
    customer_id
):

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
                "customer_id":
                    customer_id,

                "orders":
                    orders
            }
        )


    finally:

        conn.close()


# ============================================================
# ADMIN ORDER LIST
# ============================================================

def list_all_orders():

    conn = get_connection()


    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM orders
                ORDER BY created_at DESC
                """
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
# CANCEL ORDER
# ============================================================

def cancel_order(
    order_id,
    customer_id,
    role
):

    conn = None


    try:

        conn = get_connection()


        with conn.cursor() as cur:

            # =================================================
            # LOCK ORDER
            # =================================================

            cur.execute(
                """
                SELECT
                    order_id,
                    customer_id,
                    status
                FROM orders
                WHERE order_id = %s
                FOR UPDATE
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


            # =================================================
            # OWNERSHIP
            # =================================================

            if role == "customer":

                if int(
                    order["customer_id"]
                ) != int(
                    customer_id
                ):

                    return response(
                        403,
                        {
                            "error": "forbidden",
                            "message": (
                                "You can cancel "
                                "only your own order"
                            )
                        }
                    )


            current_status = order[
                "status"
            ]


            # =================================================
            # INVALID STATES
            # =================================================

            if current_status == "CANCELLED":

                return response(
                    409,
                    {
                        "error": "invalid_state",
                        "message": (
                            "Order is already cancelled"
                        )
                    }
                )


            if current_status in {
                "SHIPPED",
                "DELIVERED"
            }:

                return response(
                    409,
                    {
                        "error": "invalid_state",
                        "message": (
                            "Order cannot be cancelled "
                            f"because it is "
                            f"{current_status}"
                        )
                    }
                )


            if current_status not in {
                "PENDING",
                "CONFIRMED"
            }:

                return response(
                    409,
                    {
                        "error": "invalid_state",
                        "message": (
                            "Order cannot be cancelled "
                            f"from status "
                            f"{current_status}"
                        )
                    }
                )


            # =================================================
            # GET ORDER ITEMS
            # =================================================

            cur.execute(
                """
                SELECT
                    product_id,
                    quantity
                FROM order_items
                WHERE order_id = %s
                """,
                (
                    order_id,
                )
            )


            items = cur.fetchall()


            restored_items = []


            # =================================================
            # RESTORE INVENTORY
            # =================================================

            for item in items:

                cur.execute(
                    """
                    UPDATE products
                    SET stock_count =
                        stock_count + %s
                    WHERE product_id = %s
                    """,
                    (
                        item["quantity"],
                        item["product_id"]
                    )
                )


                cur.execute(
                    """
                    SELECT stock_count
                    FROM products
                    WHERE product_id = %s
                    """,
                    (
                        item["product_id"],
                    )
                )


                product = cur.fetchone()


                restored_items.append(
                    {
                        "product_id":
                            item["product_id"],

                        "quantity_restored":
                            item["quantity"],

                        "new_stock":
                            product["stock_count"]
                    }
                )


            # =================================================
            # UPDATE ORDER
            # =================================================

            cur.execute(
                """
                UPDATE orders
                SET status = 'CANCELLED'
                WHERE order_id = %s
                """,
                (
                    order_id,
                )
            )


            # =================================================
            # HISTORY
            # =================================================

            cur.execute(
                """
                INSERT INTO order_history
                    (
                        order_id,
                        previous_status,
                        new_status,
                        changed_by
                    )
                VALUES
                    (
                        %s,
                        %s,
                        'CANCELLED',
                        %s
                    )
                """,
                (
                    order_id,
                    current_status,
                    f"{role}-lambda"
                )
            )


            # =================================================
            # COMMIT
            # =================================================

            conn.commit()


        # ====================================================
        # EVENTS
        # ====================================================

        publish_order_event(
            "OrderCancelled",
            order_id,
            order["customer_id"],
            "CANCELLED",
            {
                "previousStatus":
                    current_status
            }
        )


        for item in restored_items:

            publish_inventory_event(
                item["product_id"],
                item["new_stock"]
            )


        return response(
            200,
            {
                "order_id":
                    order_id,

                "status":
                    "CANCELLED",

                "message":
                    "Order cancelled successfully",

                "inventory_restored":
                    restored_items
            }
        )


    except pymysql.Error as exc:

        if conn:

            conn.rollback()


        log(
            "ERROR",
            "Database error during cancellation",
            order_id=order_id,
            error=str(exc)
        )


        return response(
            500,
            {
                "error": "internal_error",
                "message": "Database error"
            }
        )


    except Exception as exc:

        if conn:

            conn.rollback()


        log(
            "ERROR",
            "Unexpected cancellation error",
            order_id=order_id,
            error=str(exc)
        )


        return response(
            500,
            {
                "error": "internal_error",
                "message": "Unexpected error"
            }
        )


    finally:

        if conn:

            conn.close()


# ============================================================
# MAIN HANDLER
# ============================================================

def lambda_handler(
    event,
    context
):

    http = (
        event.get(
            "requestContext",
            {}
        ).get(
            "http",
            {}
        )
    )


    method = (
        http.get(
            "method"
        )
        or ""
    ).upper()


    path = (
        http.get(
            "path"
        )
        or ""
    )


    # ========================================================
    # AUTHENTICATED IDENTITY
    # ========================================================

    authorizer = (
        event.get(
            "requestContext",
            {}
        ).get(
            "authorizer",
            {}
        )
    )


    role = authorizer.get(
        "role"
    )


    customer_id = authorizer.get(
        "customer_id"
    )


    # ========================================================
    # DEFENSE IN DEPTH
    # ========================================================

    if role not in {
        "admin",
        "customer"
    }:

        return response(
            403,
            {
                "error": "forbidden",
                "message": (
                    "Order access denied"
                )
            }
        )


    # ========================================================
    # JSON BODY
    # ========================================================

    try:

        body = (
            json.loads(
                event["body"]
            )
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


    # ========================================================
    # POST /orders
    # ========================================================

    if (
        method == "POST"
        and path == "/orders"
    ):

        if (
            role == "customer"
            and customer_id is None
        ):

            return response(
                403,
                {
                    "error": "forbidden",
                    "message": (
                        "Authenticated customer "
                        "identity required"
                    )
                }
            )


        return place_order(
            body,
            customer_id
        )


    # ========================================================
    # GET /orders
    # ========================================================

    if (
        method == "GET"
        and path == "/orders"
    ):

        if role == "customer":

            return list_orders_by_customer(
                customer_id
            )


        return list_all_orders()


    # ========================================================
    # GET /orders/{id}
    # ========================================================

    id_match = re.match(
        r"^/orders/(\d+)$",
        path
    )


    if (
        method == "GET"
        and id_match
    ):

        order_id = int(
            id_match.group(1)
        )


        return get_order(
            order_id,
            customer_id,
            role
        )


    # ========================================================
    # PATCH /orders/{id}
    # ========================================================

    if (
        method == "PATCH"
        and id_match
    ):

        order_id = int(
            id_match.group(1)
        )


        requested_status = str(
            body.get(
                "status",
                ""
            )
        ).upper()


        if requested_status != "CANCELLED":

            return response(
                400,
                {
                    "error": "validation_error",
                    "message": (
                        "Only CANCELLED status "
                        "is supported"
                    )
                }
            )


        return cancel_order(
            order_id,
            customer_id,
            role
        )


    return response(
        404,
        {
            "error": "not_found",
            "message": "Route not found"
        }
    )
