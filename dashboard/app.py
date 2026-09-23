import os
import logging
import hmac
from datetime import datetime, timezone

import boto3
import pymysql
from botocore.exceptions import BotoCoreError, ClientError
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ============================================================
# AWS CONFIGURATION
# ============================================================

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

REPORTS_BUCKET = os.getenv(
    "REPORTS_BUCKET",
    "cloudmart-reports-393476285814",
)

REPORTS_PREFIX = os.getenv(
    "REPORTS_PREFIX",
    "reports/",
)

ADMIN_TOKEN_PARAMETER = os.getenv(
    "ADMIN_TOKEN_PARAMETER",
    "/cloudmart/prod/auth/admin-token",
)

CLOUDWATCH_DASHBOARD_URL = os.getenv(
    "CLOUDWATCH_DASHBOARD_URL",
    "https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards/dashboard/cloudmart-prod-operations",
)

# Flask session secret.
# This must be supplied through the EC2 environment.
app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    "CHANGE_THIS_SECRET_IN_EC2_ENVIRONMENT",
)

# ============================================================
# DATABASE CONFIGURATION
# ============================================================

DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "3306"))

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
    os.getenv("DB_CONNECT_TIMEOUT", "5")
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
# ADMIN AUTHENTICATION
# ============================================================

def get_admin_token():
    """
    Read the dashboard admin token from AWS SSM Parameter Store.
    The parameter is a SecureString.
    """

    response = ssm.get_parameter(
        Name=ADMIN_TOKEN_PARAMETER,
        WithDecryption=True,
    )

    token = (
        response
        .get("Parameter", {})
        .get("Value")
    )

    if not token:
        raise RuntimeError(
            "Admin token was not found in SSM Parameter Store."
        )

    return token.strip()


def is_admin_authenticated():
    return session.get("admin_authenticated") is True


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        submitted_token = request.form.get(
            "token",
            "",
        ).strip()

        if not submitted_token:
            return render_template(
                "login.html",
                error="Please enter the admin token.",
            )

        try:
            expected_token = get_admin_token()

            if hmac.compare_digest(
                submitted_token,
                expected_token,
            ):
                session.clear()

                session["admin_authenticated"] = True

                session["login_time"] = datetime.now(
                    timezone.utc
                ).isoformat()

                return redirect(
                    url_for("dashboard")
                )

            logging.warning(
                "Invalid dashboard admin token attempt"
            )

            return render_template(
                "login.html",
                error="Invalid admin token.",
            )

        except Exception:
            logging.exception(
                "Dashboard authentication failed"
            )

            return render_template(
                "login.html",
                error="Unable to verify the admin token. "
                      "Please try again.",
            )

    if is_admin_authenticated():
        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "login.html"
    )


@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# ============================================================
# AUTHENTICATION PROTECTION
# ============================================================

@app.before_request
def protect_dashboard():

    public_paths = {
        "/login",
        "/health",
    }

    if request.path in public_paths:
        return None

    if not is_admin_authenticated():
        return redirect(
            url_for("login")
        )

    return None


# ============================================================
# DATABASE
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
            and error.args[0] in (1054, 1146)
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
# S3 REPORT
# ============================================================

def latest_report():

    try:

        response = s3.list_objects_v2(
            Bucket=REPORTS_BUCKET,
            Prefix=REPORTS_PREFIX,
        )

        objects = response.get(
            "Contents",
            [],
        )

        csv_objects = [
            item
            for item in objects
            if item["Key"]
            .lower()
            .endswith(".csv")
        ]

        if not csv_objects:
            return None

        newest = max(
            csv_objects,
            key=lambda item: item["LastModified"],
        )

        url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": REPORTS_BUCKET,
                "Key": newest["Key"],
            },
            ExpiresIn=900,
        )

        return {
            "key": newest["Key"],
            "size": newest["Size"],
            "last_modified": (
                newest["LastModified"]
                .astimezone(timezone.utc)
                .strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )
            ),
            "url": url,
        }

    except (
        BotoCoreError,
        ClientError,
    ) as error:

        logging.exception(
            "Unable to retrieve latest report: %s",
            error,
        )

        return None


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/")
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
        cloudwatch_dashboard_url=CLOUDWATCH_DASHBOARD_URL,
        report=latest_report(),
        errors=errors,
        generated_at=datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
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
# START
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
