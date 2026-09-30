import csv
import io
import json
import logging
import os
from decimal import Decimal, InvalidOperation
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
# REPORT CSV COLUMNS
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


# ============================================================
# SSM PARAMETER
# ============================================================

def get_parameter(name):
    """Read a decrypted value from AWS Systems Manager Parameter Store."""

    response = ssm.get_parameter(
        Name=name,
        WithDecryption=True,
    )

    return response["Parameter"]["Value"]


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    """Create a connection to the CloudMart RDS MySQL database."""

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
# FETCH ORDERS FOR A FIXED UTC WINDOW
# ============================================================

def fetch_orders(start_time, end_time):
    """
    Fetch orders in the half-open UTC interval:

        start_time <= created_at < end_time

    A half-open interval prevents overlap between consecutive reports.
    """

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


# ============================================================
# REPORT SUMMARY
# ============================================================

def calculate_summary(rows):
    """
    Calculate report order count and revenue.

    total_amount is repeated for each order_items row, so each
    order is counted only once. Cancelled and failed orders are
    excluded from revenue.
    """

    orders = {}

    for row in rows:
        order_id = str(row.get("order_id", "")).strip()

        if not order_id:
            continue

        if order_id not in orders:
            orders[order_id] = row

    revenue = Decimal("0")

    for row in orders.values():
        status = str(row.get("status", "")).strip().upper()

        if status in {
            "CANCELLED",
            "CANCELED",
            "FAILED",
            "FAILURE",
        }:
            continue

        try:
            revenue += Decimal(
                str(row.get("total_amount") or "0")
            )
        except (InvalidOperation, ValueError, TypeError):
            logger.warning(
                "Invalid total_amount for order %s: %r",
                row.get("order_id"),
                row.get("total_amount"),
            )

    return {
        "order_count": len(orders),
        "revenue": float(revenue),
    }


# ============================================================
# BUILD CSV
# ============================================================

def build_csv(rows):
    """Build a CSV report. An empty report still contains the header."""

    output = io.StringIO()

    writer = csv.DictWriter(
        output,
        fieldnames=FIELDNAMES,
    )

    writer.writeheader()

    for row in rows:
        normalized = dict(row)

        for key, value in normalized.items():
            if hasattr(value, "isoformat"):
                normalized[key] = value.isoformat()

        writer.writerow(
            {
                field: normalized.get(field)
                for field in FIELDNAMES
            }
        )

    return output.getvalue()


# ============================================================
# UPLOAD REPORT
# ============================================================

def upload_report(
    rows,
    prefix,
    filename,
    report_type,
    start_time,
    end_time,
):
    """Upload a report CSV to S3 with exact period metadata."""

    key = f"{prefix.rstrip('/')}/{filename}"
    csv_data = build_csv(rows)
    summary = calculate_summary(rows)

    s3.put_object(
        Bucket=REPORTS_BUCKET,
        Key=key,
        Body=csv_data.encode("utf-8"),
        ContentType="text/csv",
        ServerSideEncryption="AES256",
        Metadata={
            "report-type": report_type,
            "report-start-utc": start_time.isoformat(),
            "report-end-utc": end_time.isoformat(),
            "order-row-count": str(len(rows)),
            "order-count": str(summary["order_count"]),
            "revenue": f"{summary['revenue']:.2f}",
        },
    )

    logger.info(
        "%s report uploaded: s3://%s/%s",
        report_type,
        REPORTS_BUCKET,
        key,
    )

    logger.info(
        "%s report window: %s -> %s",
        report_type,
        start_time.isoformat(),
        end_time.isoformat(),
    )

    logger.info(
        "%s report order count: %s",
        report_type,
        summary["order_count"],
    )

    logger.info(
        "%s report revenue: %.2f",
        report_type,
        summary["revenue"],
    )

    return {
        "bucket": REPORTS_BUCKET,
        "key": key,
        "row_count": len(rows),
        "order_count": summary["order_count"],
        "revenue": summary["revenue"],
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
    }


# ============================================================
# PREVIOUS CALENDAR DAY REPORT
# ============================================================

def generate_previous_day_report(now):
    """
    Generate the previous calendar day's report.

    Example at 2026-09-30 00:00 UTC:

        2026-09-29 00:00:00 UTC
        <= created_at <
        2026-09-30 00:00:00 UTC

    The report is still created when there are zero orders.
    """

    today_start = now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    previous_day_start = today_start - timedelta(days=1)

    rows = fetch_orders(
        previous_day_start,
        today_start,
    )

    date_label = previous_day_start.strftime("%Y-%m-%d")

    return upload_report(
        rows=rows,
        prefix="reports/24hours",
        filename=f"orders-previous-day-{date_label}.csv",
        report_type="previous-day",
        start_time=previous_day_start,
        end_time=today_start,
    )


# ============================================================
# PREVIOUS CALENDAR MONTH REPORT
# ============================================================

def generate_previous_month_report(now):
    """
    Generate the complete previous calendar month's report.

    Example during September 2026:

        2026-08-01 00:00:00 UTC
        <= created_at <
        2026-09-01 00:00:00 UTC

    The report is still created when there are zero orders.
    """

    current_month_start = now.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    if current_month_start.month == 1:
        previous_month_start = current_month_start.replace(
            year=current_month_start.year - 1,
            month=12,
            day=1,
        )
    else:
        previous_month_start = current_month_start.replace(
            month=current_month_start.month - 1,
            day=1,
        )

    rows = fetch_orders(
        previous_month_start,
        current_month_start,
    )

    month_label = previous_month_start.strftime("%Y-%m")

    return upload_report(
        rows=rows,
        prefix="reports/monthly",
        filename=f"orders-last-month-{month_label}.csv",
        report_type="previous-month",
        start_time=previous_month_start,
        end_time=current_month_start,
    )


# ============================================================
# LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):
    """
    Generate both scheduled reports in one invocation:

    1. Previous calendar day: 00:00 UTC -> 00:00 UTC
    2. Previous calendar month: first day 00:00 UTC -> current month first day 00:00 UTC

    Both reports are written even when their order count is zero.
    """

    logger.info("Starting CloudMart report generation")
    logger.info("Environment=%s", ENVIRONMENT)
    logger.info(
        "RequestId=%s",
        getattr(context, "aws_request_id", "unknown"),
    )

    now = datetime.now(timezone.utc)

    try:
        previous_day = generate_previous_day_report(now)
        previous_month = generate_previous_month_report(now)

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": (
                        "Previous day and previous month reports "
                        "generated successfully"
                    ),
                    "previous_day": previous_day,
                    "previous_month": previous_month,
                }
            ),
        }

    except Exception:
        logger.exception("CloudMart report generation failed")
        raise
