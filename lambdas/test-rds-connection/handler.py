import json
import os
import pymysql
import boto3

ssm = boto3.client("ssm")


def get_param(name, decrypt=False):
    response = ssm.get_parameter(Name=name, WithDecryption=decrypt)
    return response["Parameter"]["Value"]


def lambda_handler(event, context):
    environment = os.environ.get("ENVIRONMENT", "prod")

    try:
        db_host = get_param(f"/cloudmart/{environment}/db/host")
    except ssm.exceptions.ParameterNotFound:
        # db/host was never created in earlier steps — fall back to the
        # RDS endpoint passed in directly as a Lambda environment variable.
        db_host = os.environ.get("DB_HOST")

    db_username = get_param(f"/cloudmart/{environment}/db/username", decrypt=True)
    db_password = get_param(f"/cloudmart/{environment}/db/password", decrypt=True)

    print(json.dumps({
        "level": "INFO",
        "message": "Attempting RDS connection",
        "host": db_host
    }))

    try:
        connection = pymysql.connect(
            host=db_host,
            user=db_username,
            password=db_password,
            connect_timeout=5
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            result = cursor.fetchone()
        connection.close()

        print(json.dumps({
            "level": "INFO",
            "message": "RDS connection succeeded",
            "result": result
        }))

        return {
            "statusCode": 200,
            "body": json.dumps({"status": "connected", "result": result})
        }

    except Exception as e:
        print(json.dumps({
            "level": "ERROR",
            "message": "RDS connection failed",
            "error": str(e)
        }))
        return {
            "statusCode": 500,
            "body": json.dumps({"status": "failed", "error": str(e)})
        }
