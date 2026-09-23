import csv
import io
import json
import logging
import os
from datetime import datetime, timezone, timedelta

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
    """Read a decrypted value from AWS Systems Manager Parameter Store."""
    response = ssm.get_parameter(Name=name, WithDecryption=True)
    return response["Parameter"]["Value"]


def get_connection():
    """Create a connection to the MySQL RDS database."""
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


def get_report_period(report_type, now=None):
    """
    Return the UTC period represented by the requested report.

    last_24_hours = rolling previous 24 hours.
    monthly = current calendar month-to-date.
    """
    now = now or datetime.now(timezone.utc).replace(microsecond=0)

    if report_type == "last_24_hours":
        return (
            now - timedelta(hours=24),
            now,
            "Last 24 Hours",
        )

    if report_type == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0)
        return (
            start,
            now,
            f"Monthly - {start.strftime('%Y-%m')}",
        )

    raise ValueError(
        "Unsupported report_type. Use 'last_24_hours' or 'monthly'."
    )


def generate_report(report_type="last_24_hours"):
    """Generate a report for the requested period and upload it to S3."""
    period_start, period_end, report_label = get_report_period(report_type)

    # Revenue is calculated directly from orders so a JOIN to order_items
    # cannot multiply the same order's total_amount.
    summary_query = """
        SELECT
            COUNT(*) AS total_orders,
            COALESCE(SUM(total_amount), 0) AS total_revenue
        FROM orders
        WHERE created_at >= %s
          AND created_at < %s
    """

    details_query = """
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
        ORDER BY o.created_at DESC, o.order_id DESC
    """

    connection = get_connection()

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                summary_query,
                (period_start.replace(tzinfo=None), period_end.replace(tzinfo=None)),
            )
            summary = cursor.fetchone() or {}

            cursor.execute(
                details_query,
                (period_start.replace(tzinfo=None), period_end.replace(tzinfo=None)),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    total_orders = int(summary.get("total_orders") or 0)
    total_revenue = summary.get("total_revenue") or 0

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

    output = io.StringIO()
    writer = csv.writer(output)

    # Summary is deliberately included in the CSV itself.
    writer.writerow(["CloudMart Report"])
    writer.writerow(["Report Type", report_label])
    writer.writerow(["Period Start (UTC)", period_start.isoformat()])
    writer.writerow(["Period End (UTC)", period_end.isoformat()])
    writer.writerow(["Total Orders", total_orders])
    writer.writerow(["Total Revenue", str(total_revenue)])
    writer.writerow([])
    writer.writerow(["Order Details"])
    writer.writerow(fieldnames)

    for row in rows:
        normalized = dict(row)

        for key, value in normalized.items():
            if hasattr(value, "isoformat"):
                normalized[key] = value.isoformat()

        writer.writerow(
            [normalized.get(field) for field in fieldnames]
        )

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H-%M-%SZ"
    )

    if report_type == "monthly":
        prefix = "reports/monthly"
        filename = f"orders-monthly-{period_start.strftime('%Y-%m')}-{timestamp}.csv"
    else:
        prefix = "reports/24hours"
        filename = f"orders-24hours-{timestamp}.csv"

    key = f"{prefix}/{filename}"

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
        "report_type": report_type,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "total_orders": total_orders,
        "total_revenue": str(total_revenue),
        "row_count": len(rows),
    }


def lambda_handler(event, context):
    """AWS Lambda entry point."""
    logger.info(
        "Starting report generation. Environment=%s, RequestId=%s, Event=%s",
        ENVIRONMENT,
        getattr(context, "aws_request_id", "unknown"),
        event,
    )

    try:
        report_type = "last_24_hours"

        if isinstance(event, dict):
            report_type = event.get(
                "report_type",
                event.get("reportType", "last_24_hours"),
            )

        result = generate_report(report_type)

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Report generated successfully",
                    **result,
                }
            ),
        }

    except Exception:
        logger.exception("Report generation failed")
        raise
