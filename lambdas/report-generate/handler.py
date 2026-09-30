import csv
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
import pymysql


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ============================================================
# AWS CLIENTS
# ============================================================

s3 = boto3.client("s3")
ssm = boto3.client("ssm")


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

ENVIRONMENT = os.getenv("ENVIRONMENT", "prod")
REPORTS_BUCKET = os.environ["REPORTS_BUCKET"]
DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.getenv("DB_NAME", "cloudmart")
DB_USERNAME_PARAMETER = os.environ["DB_USERNAME_PARAMETER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]


# ============================================================
# SSM PARAMETER
# ============================================================

def get_parameter(name):
    response = ssm.get_parameter(Name=name, WithDecryption=True)
    return response["Parameter"]["Value"]


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    return pymysql.connect(
        host=DB_HOST,
        user=get_parameter(DB_USERNAME_PARAMETER),
        password=get_parameter(DB_PASSWORD_PARAMETER),
        database=DB_NAME,
        port=3306,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


# ============================================================
# CSV HELPERS
# ============================================================

FIELDNAMES = [
    "order_id",
    "customer_id",
    "status",
    "total_amount",
    "created_at",
    "product_id",
    "quantity",
    "unit_price",
]


def fetch_orders(start_time, end_time):
    """Fetch orders and their line items inside a UTC time window."""
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
        WHERE o.created_at >= %s
          AND o.created_at < %s
        ORDER BY
            o.created_at DESC,
            o.order_id DESC
    """

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, (start_time, end_time))
            return cursor.fetchall()
    finally:
        connection.close()


def rows_to_csv(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=FIELDNAMES)
    writer.writeheader()

    for row in rows:
        normalized = dict(row)
        for key, value in normalized.items():
            if hasattr(value, "isoformat"):
                normalized[key] = value.isoformat()

        writer.writerow({field: normalized.get(field) for field in FIELDNAMES})

    return output.getvalue()


def upload_report(key, csv_text):
    s3.put_object(
        Bucket=REPORTS_BUCKET,
        Key=key,
        Body=csv_text.encode("utf-8"),
        ContentType="text/csv",
        ServerSideEncryption="AES256",
    )


def report_summary(rows):
    """Count unique orders and revenue once per order."""
    totals = {}
    for row in rows:
        order_id = row.get("order_id")
        if order_id is None:
            continue
        if order_id not in totals:
            try:
                totals[order_id] = float(row.get("total_amount") or 0)
            except (TypeError, ValueError):
                totals[order_id] = 0.0

    return {
        "order_count": len(totals),
        "revenue": round(sum(totals.values()), 2),
        "row_count": len(rows),
    }


# ============================================================
# PREVIOUS DAY REPORT
# ============================================================

def generate_previous_day_report(now=None):
    """
    Generate the previous calendar day's report:

        previous day 00:00 UTC -> current day 00:00 UTC

    Example on 2026-09-30:
        2026-09-29 00:00 UTC -> 2026-09-30 00:00 UTC
    """
    if now is None:
        now = datetime.now(timezone.utc)

    end_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_time = end_time.replace(day=end_time.day - 1)

    # Use timedelta so this also works correctly on the first day of a month/year.
    start_time = end_time - timedelta(days=1)

    rows = fetch_orders(start_time, end_time)
    csv_text = rows_to_csv(rows)
    report_date = start_time.strftime("%Y-%m-%d")
    key = f"reports/24hours/orders-previous-day-{report_date}.csv"

    upload_report(key, csv_text)
    summary = report_summary(rows)

    logger.info(
        "Previous day report uploaded: s3://%s/%s orders=%s revenue=%.2f",
        REPORTS_BUCKET,
        key,
        summary["order_count"],
        summary["revenue"],
    )

    return {
        "period": "previous_day",
        "bucket": REPORTS_BUCKET,
        "key": key,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        **summary,
    }


# ============================================================
# LAST MONTH REPORT
# ============================================================

def first_day_of_month(value):
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def generate_last_month_report(now=None):
    """
    Generate the previous calendar month's report:

        first day of previous month 00:00 UTC
        -> first day of current month 00:00 UTC

    A CSV is created even when there are zero orders.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    end_time = first_day_of_month(now)
    previous_month_end = end_time - timedelta(seconds=1)
    start_time = first_day_of_month(previous_month_end)

    rows = fetch_orders(start_time, end_time)
    csv_text = rows_to_csv(rows)
    month_label = start_time.strftime("%Y-%m")
    key = f"reports/monthly/orders-last-month-{month_label}.csv"

    upload_report(key, csv_text)
    summary = report_summary(rows)

    logger.info(
        "Last month report uploaded: s3://%s/%s orders=%s revenue=%.2f",
        REPORTS_BUCKET,
        key,
        summary["order_count"],
        summary["revenue"],
    )

    return {
        "period": "last_month",
        "bucket": REPORTS_BUCKET,
        "key": key,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        **summary,
    }


# ============================================================
# LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):
    logger.info("Starting CloudMart report generation")
    logger.info("Environment=%s", ENVIRONMENT)
    logger.info("RequestId=%s", getattr(context, "aws_request_id", "unknown"))

    try:
        now = datetime.now(timezone.utc)
        previous_day = generate_previous_day_report(now)
        last_month = generate_last_month_report(now)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Previous day and last month reports generated successfully",
                "previous_day": previous_day,
                "last_month": last_month,
            }),
        }
    except Exception:
        logger.exception("CloudMart report generation failed")
        raise
