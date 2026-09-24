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
    """
    Read a decrypted value from AWS Systems Manager
    Parameter Store.
    """

    response = ssm.get_parameter(
        Name=name,
        WithDecryption=True
    )

    return response["Parameter"]["Value"]


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    """
    Create a connection to the CloudMart RDS MySQL database.
    """

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
# GENERATE LAST 24 HOURS REPORT
# ============================================================

def generate_report():
    """
    Generate a CSV containing only orders created
    during the previous 24 hours.

    The report is uploaded to:

        reports/24hours/

    Revenue is represented by orders.total_amount.
    """

    # --------------------------------------------------------
    # Calculate the 24-hour time window
    # --------------------------------------------------------

    end_time = datetime.now(timezone.utc)

    start_time = end_time - timedelta(hours=24)

    logger.info(
        "Generating last 24 hours report: %s -> %s",
        start_time.isoformat(),
        end_time.isoformat()
    )


    # --------------------------------------------------------
    # SQL query
    #
    # IMPORTANT:
    # The WHERE clause restricts the report to the
    # previous 24 hours.
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # Execute query
    # --------------------------------------------------------

    connection = get_connection()

    try:

        with connection.cursor() as cursor:

            cursor.execute(
                query,
                (
                    start_time,
                    end_time
                )
            )

            rows = cursor.fetchall()

    finally:

        connection.close()


    # --------------------------------------------------------
    # CSV columns
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # Create CSV
    # --------------------------------------------------------

    output = io.StringIO()

    writer = csv.DictWriter(
        output,
        fieldnames=fieldnames
    )

    writer.writeheader()


    # --------------------------------------------------------
    # Write order rows
    # --------------------------------------------------------

    for row in rows:

        normalized = dict(row)

        for key, value in normalized.items():

            if hasattr(value, "isoformat"):

                normalized[key] = value.isoformat()


        writer.writerow(
            {
                field: normalized.get(field)
                for field in fieldnames
            }
        )


    # --------------------------------------------------------
    # Create timestamp
    # --------------------------------------------------------

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H-%M-%SZ"
    )


    # --------------------------------------------------------
    # S3 location
    #
    # Example:
    #
    # reports/24hours/orders-24hours-2026-09-24T10-43-00Z.csv
    # --------------------------------------------------------

    key = (
        f"reports/24hours/"
        f"orders-24hours-{timestamp}.csv"
    )


    # --------------------------------------------------------
    # Upload CSV to S3
    # --------------------------------------------------------

    s3.put_object(
        Bucket=REPORTS_BUCKET,
        Key=key,
        Body=output.getvalue().encode("utf-8"),
        ContentType="text/csv",
        ServerSideEncryption="AES256",
    )


    # --------------------------------------------------------
    # Log result
    # --------------------------------------------------------

    logger.info(
        "Last 24 hours report uploaded successfully: "
        "s3://%s/%s",
        REPORTS_BUCKET,
        key
    )

    logger.info(
        "Report row count: %s",
        len(rows)
    )


    # --------------------------------------------------------
    # Return result
    # --------------------------------------------------------

    return {
        "bucket": REPORTS_BUCKET,
        "key": key,
        "row_count": len(rows),
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
    }


# ============================================================
# LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):
    """
    AWS Lambda entry point.
    """

    logger.info(
        "Starting CloudMart last-24-hours report generation."
    )

    logger.info(
        "Environment=%s",
        ENVIRONMENT
    )

    logger.info(
        "RequestId=%s",
        getattr(
            context,
            "aws_request_id",
            "unknown"
        )
    )


    try:

        result = generate_report()


        return {
            "statusCode": 200,

            "body": json.dumps(
                {
                    "message": (
                        "Last 24 hours report "
                        "generated successfully"
                    ),

                    **result
                }
            )
        }


    except Exception:

        logger.exception(
            "Last 24 hours report generation failed"
        )

        raise
