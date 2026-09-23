import os
import logging
from datetime import datetime, timezone

import boto3
import pymysql
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, render_template

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
REPORTS_BUCKET = os.getenv("REPORTS_BUCKET", "cloudmart-reports-393476285814")
REPORTS_PREFIX = os.getenv("REPORTS_PREFIX", "reports/")

DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "cloudmart")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))
CLOUDWATCH_DASHBOARD_URL = os.getenv(
    "CLOUDWATCH_DASHBOARD_URL",
    "https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=cloudmart-prod-operations",
)

s3 = boto3.client("s3", region_name=AWS_REGION)


def database_configured():
    return all([DB_HOST, DB_NAME, DB_USER, DB_PASSWORD])


def get_connection():
    if not database_configured():
        raise RuntimeError(
            "Database environment variables are not configured. "
            "Set DB_HOST, DB_NAME, DB_USER and DB_PASSWORD."
        )

    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=DB_CONNECT_TIMEOUT,
        read_timeout=DB_CONNECT_TIMEOUT,
        write_timeout=DB_CONNECT_TIMEOUT,
        autocommit=True,
    )


def fetch_table_rows(table_name, limit=20, order_by=None):
    """Read a small, read-only view of a table.

    Table names are fixed internally and are never taken from a request.
    """
    allowed_tables = {"products", "orders"}
    if table_name not in allowed_tables:
        raise ValueError("Unsupported table")

    query = f"SELECT * FROM `{table_name}`"
    if order_by:
        query += f" ORDER BY `{order_by}` DESC"
    query += " LIMIT %s"

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (limit,))
            rows = cursor.fetchall()

    return rows


def fetch_products():
    try:
        # updated_at is used when available; fallback is handled below.
        return fetch_table_rows("products", limit=100, order_by="updated_at")
    except pymysql.err.OperationalError as error:
        if error.args and error.args[0] in (1054, 1146):
            return fetch_table_rows("products", limit=100)
        raise


def fetch_orders():
    # The current project schema may use different timestamp column names.
    candidate_columns = ("created_at", "order_date", "ordered_at", "updated_at")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'orders'
                """,
                (DB_NAME,),
            )
            columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}

    order_column = next((column for column in candidate_columns if column in columns), None)
    return fetch_table_rows("orders", limit=20, order_by=order_column)


def latest_report():
    try:
        response = s3.list_objects_v2(
            Bucket=REPORTS_BUCKET,
            Prefix=REPORTS_PREFIX,
        )
        objects = response.get("Contents", [])
        csv_objects = [
            item for item in objects if item["Key"].lower().endswith(".csv")
        ]

        if not csv_objects:
            return None

        newest = max(csv_objects, key=lambda item: item["LastModified"])
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": REPORTS_BUCKET, "Key": newest["Key"]},
            ExpiresIn=900,
        )

        return {
            "key": newest["Key"],
            "size": newest["Size"],
            "last_modified": newest["LastModified"].astimezone(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            ),
            "url": url,
        }
    except (BotoCoreError, ClientError) as error:
        logging.exception("Unable to retrieve latest report: %s", error)
        return None


@app.route("/")
def dashboard():
    products = []
    orders = []
    errors = []

    try:
        products = fetch_products()
    except Exception as error:
        logging.exception("Unable to load products")
        errors.append(f"Products: {error}")

    try:
        orders = fetch_orders()
    except Exception as error:
        logging.exception("Unable to load orders")
        errors.append(f"Orders: {error}")

    failed_orders = [
        order
        for order in orders
        if str(order.get("status", "")).strip().upper() in {
            "FAILED", "FAILURE", "CANCELLED", "CANCELED"
        }
    ]

    return render_template(
        "index.html",
        products=products,
        orders=orders,
        failed_orders=failed_orders,
        cloudwatch_dashboard_url=CLOUDWATCH_DASHBOARD_URL,
        report=latest_report(),
        errors=errors,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


@app.route("/health")
def health():
    return {"status": "ok", "service": "cloudmart-dashboard"}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "80")))
