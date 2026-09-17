import csv
import io
import json
import logging
import os
from datetime import datetime, timezone

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
ssm = boto3.client("ssm")

ENVIRONMENT = os.getenv("ENVIRONMENT", "prod")
REPORTS_BUCKET = os.environ["REPORTS_BUCKET"]
DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.getenv("DB_NAME", "cloudmart")
DB_USERNAME_PARAMETER = os.environ["DB_USERNAME_PARAMETER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]


def get_parameter(name):
    response = ssm.get_parameter(Name=name, WithDecryption=True)
    return response["Parameter"]["Value"]


def get_connection():
    return pymysql.connect(
        host=DB_HOST,
        user=get_parameter(DB_USERNAME_PARAMETER),
        password=get_parameter(DB_PASSWORD_PARAMETER),
        database=DB_NAME,
        port=3306,
        connect_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def generate_report():
    query = """
        SELECT
            o.order_id,
            o.customer_id,
            o.status,
            o.total_amount,
            o.created_at,
            oi.product_id,
            oi.quantity,
            oi.unit_price
        FROM orders o
        LEFT JOIN order_items oi
            ON o.order_id = oi.order_id
        ORDER BY o.created_at DESC, o.order_id DESC
    """

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(query)
            rows = cursor.fetchall()
    finally:
        connection.close()

    output = io.StringIO()
    fieldnames = [
        "order_id",
        "customer_id",
        "status",
        "total_amount",
        "created_at",
        "product_id",
        "quantity",
        "unit_price",
    ]

    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for row in rows:
        normalized = dict(row)
        for key, value in normalized.items():
            if hasattr(value, "isoformat"):
                normalized[key] = value.isoformat()
        writer.writerow({field: normalized.get(field) for field in fieldnames})

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    key = f"reports/orders-report-{timestamp}.csv"

    s3.put_object(
        Bucket=REPORTS_BUCKET,
        Key=key,
        Body=output.getvalue().encode("utf-8"),
        ContentType="text/csv",
        ServerSideEncryption="AES256",
    )

    return {
        "bucket": REPORTS_BUCKET,
        "key": key,
        "row_count": len(rows),
    }


def lambda_handler(event, context):
    logger.info("Report generation started. Event: %s", json.dumps(event))

    try:
        result = generate_report()
        logger.info("Report generated successfully: %s", result)
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Report generated successfully",
                **result,
            }),
        }
    except Exception:
        logger.exception("Report generation failed")
        raise
