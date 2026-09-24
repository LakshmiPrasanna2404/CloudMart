import os
import logging
import hashlib
import csv
import io
from datetime import datetime, timezone, timedelta

import boto3
import pymysql

from botocore.exceptions import BotoCoreError, ClientError
from flask import (
    Flask,
    render_template,
    render_template_string,
    Response,
    request,
    redirect,
    url_for,
    session,
)

# ============================================================
# APPLICATION
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ============================================================
# AWS CONFIGURATION
# ============================================================

AWS_REGION = os.getenv(
    "AWS_REGION",
    "us-east-1",
)

REPORTS_BUCKET = os.getenv(
    "REPORTS_BUCKET",
    "cloudmart-reports-393476285814",
)

REPORTS_PREFIX = os.getenv(
    "REPORTS_PREFIX",
    "reports/",
)

# ============================================================
# DATABASE CONFIGURATION
# ============================================================

DB_HOST = os.getenv(
    "DB_HOST",
    "",
)

DB_PORT = int(
    os.getenv(
        "DB_PORT",
        "3306",
    )
)

DB_NAME = os.getenv(
    "DB_NAME",
    "cloudmart",
)

DB_USER = os.getenv(
    "DB_USER",
    "",
)

DB_PASSWORD = os.getenv(
    "DB_PASSWORD",
    "",
)

DB_CONNECT_TIMEOUT = int(
    os.getenv(
        "DB_CONNECT_TIMEOUT",
        "5",
    )
)

# ============================================================
# ADMIN AUTHENTICATION
# ============================================================

ADMIN_TOKEN_PARAMETER = os.getenv(
    "ADMIN_TOKEN_PARAMETER",
    "/cloudmart/prod/auth/admin-token",
)

# Session timeout.
SESSION_TIMEOUT_MINUTES = int(
    os.getenv(
        "SESSION_TIMEOUT_MINUTES",
        "60",
    )
)

# Flask secret key.
#
# Recommended:
# Set FLASK_SECRET_KEY in the EC2 environment.
#
# The fallback is generated when the application starts.
# This means active sessions will expire after an application restart.
FLASK_SECRET_KEY = os.getenv(
    "FLASK_SECRET_KEY"
)

if not FLASK_SECRET_KEY:
    FLASK_SECRET_KEY = os.urandom(32).hex()

app.secret_key = FLASK_SECRET_KEY

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
    PERMANENT_SESSION_LIFETIME=timedelta(
        minutes=SESSION_TIMEOUT_MINUTES
    ),
)

# ============================================================
# CLOUDWATCH DASHBOARD
# ============================================================

CLOUDWATCH_DASHBOARD_URL = os.getenv(
    "CLOUDWATCH_DASHBOARD_URL",
    "https://us-east-1.console.aws.amazon.com/cloudwatch/home"
    "?region=us-east-1"
    "#dashboards/dashboard/cloudmart-prod-operations",
)

# ============================================================
# AWS CLIENTS
# ============================================================

s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
)

ssm = boto3.client(
    "ssm",
    region_name=AWS_REGION,
)


# ============================================================
# ADMIN TOKEN
# ============================================================

def get_admin_token():
    """
    Read the CloudMart admin token securely from
    AWS Systems Manager Parameter Store.
    """

    try:
        response = ssm.get_parameter(
            Name=ADMIN_TOKEN_PARAMETER,
            WithDecryption=True,
        )

        token = response["Parameter"]["Value"]

        if not token:
            raise RuntimeError(
                "Admin token parameter is empty."
            )

        return token

    except (BotoCoreError, ClientError) as error:
        logging.exception(
            "Unable to retrieve admin token from SSM: %s",
            error,
        )

        raise RuntimeError(
            "Unable to retrieve admin authentication token."
        )


def is_authenticated():
    """
    Check whether the current browser session
    has successfully authenticated.
    """

    return session.get("admin_authenticated") is True


# ============================================================
# LOGIN REQUIRED DECORATOR
# ============================================================

def login_required(view_function):
    """
    Protect a Flask route.

    Unauthorized users are redirected to /login.
    """

    from functools import wraps

    @wraps(view_function)
    def wrapped_view(*args, **kwargs):

        if not is_authenticated():
            return redirect(
                url_for(
                    "login",
                    next=request.path,
                )
            )

        return view_function(
            *args,
            **kwargs,
        )

    return wrapped_view


# ============================================================
# DATABASE HELPERS
# ============================================================

def database_configured():
    return all(
        [
            DB_HOST,
            DB_NAME,
            DB_USER,
            DB_PASSWORD,
        ]
    )


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


def fetch_table_rows(
    table_name,
    limit=20,
    order_by=None,
):
    """
    Read a small, read-only view of a table.

    Table names are fixed internally and are never taken
    directly from a request.
    """

    allowed_tables = {
        "products",
        "orders",
    }

    if table_name not in allowed_tables:
        raise ValueError(
            "Unsupported table"
        )

    query = (
        f"SELECT * FROM `{table_name}`"
    )

    if order_by:
        query += (
            f" ORDER BY `{order_by}` DESC"
        )

    query += " LIMIT %s"

    with get_connection() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                query,
                (limit,),
            )

            rows = cursor.fetchall()

    return rows


def fetch_products():

    try:

        return fetch_table_rows(
            "products",
            limit=100,
            order_by="updated_at",
        )

    except pymysql.err.OperationalError as error:

        if (
            error.args
            and error.args[0]
            in (1054, 1146)
        ):

            return fetch_table_rows(
                "products",
                limit=100,
            )

        raise


def fetch_orders():

    candidate_columns = (
        "created_at",
        "order_date",
        "ordered_at",
        "updated_at",
    )

    with get_connection() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = 'orders'
                """,
                (DB_NAME,),
            )

            columns = {
                row["COLUMN_NAME"]
                for row in cursor.fetchall()
            }

    order_column = next(
        (
            column
            for column in candidate_columns
            if column in columns
        ),
        None,
    )

    return fetch_table_rows(
        "orders",
        limit=20,
        order_by=order_column,
    )


# ============================================================
# REPORTS
# ============================================================

def list_report_objects(prefix):
    """
    List CSV reports under a specific S3 prefix.

    Example prefixes:
        reports/24hours/
        reports/monthly/
    """

    objects = []

    try:
        paginator = s3.get_paginator("list_objects_v2")

        for page in paginator.paginate(
            Bucket=REPORTS_BUCKET,
            Prefix=prefix,
        ):
            for item in page.get("Contents", []):
                key = item.get("Key", "")

                if key.lower().endswith(".csv"):
                    objects.append(item)

    except (BotoCoreError, ClientError) as error:
        logging.exception(
            "Unable to list reports under %s: %s",
            prefix,
            error,
        )
        return []

    return objects


def build_report_info(period):
    """
    Return the newest S3 report for the requested period.

    period:
        24h
        monthly
    """

    if period == "24h":
        prefix = f"{REPORTS_PREFIX.rstrip('/')}/24hours/"
        title = "Last 24 Hours"
    elif period == "monthly":
        prefix = f"{REPORTS_PREFIX.rstrip('/')}/monthly/"
        title = "Monthly"
    else:
        return None

    objects = list_report_objects(prefix)

    if not objects:
        return None

    newest = max(
        objects,
        key=lambda item: item["LastModified"],
    )

    key = newest["Key"]

    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": REPORTS_BUCKET,
            "Key": key,
        },
        ExpiresIn=900,
    )

    return {
        "period": period,
        "title": title,
        "key": key,
        "size": newest["Size"],
        "last_modified": (
            newest["LastModified"]
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S UTC")
        ),
        "url": url,
    }


def latest_report():
    """
    The dashboard's Latest Report card uses the newest
    Last-24-Hours report when one exists.
    """

    return build_report_info("24h")


def get_report_object(period):
    """
    Return the newest report object for a period.
    """

    report = build_report_info(period)

    if not report:
        return None

    try:
        response = s3.get_object(
            Bucket=REPORTS_BUCKET,
            Key=report["key"],
        )

        return report, response

    except (BotoCoreError, ClientError) as error:
        logging.exception(
            "Unable to read report %s from S3: %s",
            period,
            error,
        )
        return None


@app.route("/reports/<period>/view")
@login_required
def view_report(period):
    """
    Display the selected CSV report inside the dashboard.
    """

    if period not in {"24h", "monthly"}:
        return "Report not found", 404

    result = get_report_object(period)

    if not result:
        return (
            render_template_string(
                """
                <!doctype html>
                <html>
                <head>
                    <title>CloudMart Report</title>
                    <style>
                        body {
                            font-family: Arial, sans-serif;
                            margin: 40px;
                            background: #f4f6f9;
                            color: #172033;
                        }
                        .box {
                            background: white;
                            padding: 24px;
                            border-radius: 10px;
                            box-shadow: 0 2px 8px rgba(0,0,0,.08);
                        }
                        a {
                            display: inline-block;
                            margin-top: 16px;
                            padding: 10px 14px;
                            background: #2563eb;
                            color: white;
                            text-decoration: none;
                            border-radius: 6px;
                        }
                    </style>
                </head>
                <body>
                    <div class="box">
                        <h1>{{ title }} Report</h1>
                        <p>No report has been generated for this period yet.</p>
                        <a href="{{ url_for('dashboard') }}">Back to Dashboard</a>
                    </div>
                </body>
                </html>
                """,
                title=(
                    "Last 24 Hours"
                    if period == "24h"
                    else "Monthly"
                ),
            ),
            404,
        )

    report, response = result

    raw_csv = response["Body"].read().decode(
        "utf-8",
        errors="replace",
    )

    reader = csv.DictReader(
        io.StringIO(raw_csv)
    )

    rows = list(reader)

    return render_template_string(
        """
        <!doctype html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>CloudMart - {{ report.title }}</title>

            <style>
                body {
                    margin: 0;
                    padding: 24px 6%;
                    font-family: Arial, sans-serif;
                    background: #f4f6f9;
                    color: #172033;
                }

                .box {
                    background: white;
                    border-radius: 10px;
                    padding: 24px;
                    box-shadow: 0 2px 8px rgba(0,0,0,.08);
                }

                .actions {
                    display: flex;
                    gap: 10px;
                    flex-wrap: wrap;
                    margin: 18px 0;
                }

                a.button {
                    display: inline-block;
                    padding: 10px 14px;
                    background: #2563eb;
                    color: white;
                    text-decoration: none;
                    border-radius: 6px;
                }

                a.secondary {
                    background: #475569;
                }

                .muted {
                    color: #667085;
                    font-size: 13px;
                }

                .table-wrap {
                    overflow-x: auto;
                }

                table {
                    width: 100%;
                    border-collapse: collapse;
                    min-width: 650px;
                }

                th,
                td {
                    border-bottom: 1px solid #e5e7eb;
                    padding: 10px;
                    text-align: left;
                    vertical-align: top;
                    font-size: 14px;
                }

                th {
                    background: #eef1f5;
                }

                .empty {
                    padding: 20px;
                    background: #f8fafc;
                    border-radius: 8px;
                }
            </style>
        </head>

        <body>

            <div class="box">

                <h1>CloudMart {{ report.title }} Report</h1>

                <p class="muted">
                    File: {{ report.key }}
                </p>

                <p class="muted">
                    Last Modified: {{ report.last_modified }}
                </p>

                <p>
                    <strong>Orders in report:</strong>
                    {{ rows|length }}
                </p>

                <div class="actions">
                    <a
                        class="button"
                        href="{{ url_for('download_report', period=report.period) }}"
                    >
                        Download CSV
                    </a>

                    <a
                        class="button secondary"
                        href="{{ url_for('dashboard') }}"
                    >
                        Back to Dashboard
                    </a>
                </div>

                {% if rows %}

                    <div class="table-wrap">

                        <table>

                            <thead>
                                <tr>
                                    {% for key in rows[0].keys() %}
                                        <th>{{ key }}</th>
                                    {% endfor %}
                                </tr>
                            </thead>

                            <tbody>

                                {% for row in rows %}

                                    <tr>

                                        {% for value in row.values() %}

                                            <td>
                                                {{ value if value is not none else "" }}
                                            </td>

                                        {% endfor %}

                                    </tr>

                                {% endfor %}

                            </tbody>

                        </table>

                    </div>

                {% else %}

                    <div class="empty">
                        No orders were created during this report period.
                        The report file is still valid and available for download.
                    </div>

                {% endif %}

            </div>

        </body>
        </html>
        """,
        report=report,
        rows=rows,
    )


@app.route("/reports/<period>/download")
@login_required
def download_report(period):
    """
    Download the newest CSV report for the requested period.
    """

    if period not in {"24h", "monthly"}:
        return "Report not found", 404

    result = get_report_object(period)

    if not result:
        return "Report not available yet", 404

    report, response = result

    csv_data = response["Body"].read()

    filename = (
        "cloudmart-last-24-hours-report.csv"
        if period == "24h"
        else "cloudmart-monthly-report.csv"
    )

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            )
        },
    )

# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"],
)
def login():

    # Already logged in.
    if is_authenticated():

        return redirect(
            url_for("dashboard")
        )

    error = None

    if request.method == "POST":

        # Accept the common names used by login.html.
        submitted_token = (
            request.form.get("token")
            or request.form.get("admin_token")
            or request.form.get("password")
            or ""
        ).strip()

        if not submitted_token:

            error = "Please enter the admin token."

        else:

            try:

                actual_token = get_admin_token()

                # Constant-time comparison.
                if hashlib.sha256(
                    submitted_token.encode(
                        "utf-8"
                    )
                ).digest() == hashlib.sha256(
                    actual_token.encode(
                        "utf-8"
                    )
                ).digest():

                    session.clear()

                    session.permanent = True

                    session["admin_authenticated"] = True

                    session["login_time"] = (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    )

                    logging.info(
                        "Admin dashboard login successful"
                    )

                    next_url = request.args.get(
                        "next"
                    )

                    if (
                        next_url
                        and next_url.startswith("/")
                    ):
                        return redirect(
                            next_url
                        )

                    return redirect(
                        url_for(
                            "dashboard"
                        )
                    )

                else:

                    logging.warning(
                        "Invalid dashboard admin token attempt"
                    )

                    error = (
                        "Invalid admin token."
                    )

            except Exception as exception:

                logging.exception(
                    "Dashboard authentication error"
                )

                error = (
                    "Unable to verify admin token. "
                    "Please try again."
                )

    return render_template(
        "login.html",
        error=error,
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# ============================================================
# MAIN DASHBOARD
# ============================================================

@app.route("/")
@login_required
def dashboard():

    products = []
    orders = []
    errors = []

    try:

        products = fetch_products()

    except Exception as error:

        logging.exception(
            "Unable to load products"
        )

        errors.append(
            f"Products: {error}"
        )

    try:

        orders = fetch_orders()

    except Exception as error:

        logging.exception(
            "Unable to load orders"
        )

        errors.append(
            f"Orders: {error}"
        )

    failed_orders = [
        order
        for order in orders
        if str(
            order.get(
                "status",
                "",
            )
        ).strip().upper()
        in {
            "FAILED",
            "FAILURE",
            "CANCELLED",
            "CANCELED",
        }
    ]

    return render_template(
        "index.html",
        products=products,
        orders=orders,
        failed_orders=failed_orders,
        cloudwatch_dashboard_url=(
            CLOUDWATCH_DASHBOARD_URL
        ),
        report=latest_report(),
        report_24h=build_report_info("24h"),
        report_monthly=build_report_info("monthly"),
        errors=errors,
        generated_at=(
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        ),
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health")
def health():

    return {
        "status": "ok",
        "service": "cloudmart-dashboard",
    }, 200


# ============================================================
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "80",
            )
        ),
    )
