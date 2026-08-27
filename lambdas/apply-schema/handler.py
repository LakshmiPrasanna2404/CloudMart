import json
import os
import boto3
import pymysql

ssm = boto3.client("ssm")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")

SCHEMA_SQL = """
CREATE DATABASE IF NOT EXISTS cloudmart;
USE cloudmart;

CREATE TABLE IF NOT EXISTS customers (
    customer_id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS products (
    product_id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    description TEXT,
    price DECIMAL(10,2) NOT NULL,
    category VARCHAR(100),
    stock_count INT NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_products_category (category),
    INDEX idx_products_is_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS offers (
    offer_id INT AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL,
    discount_percentage DECIMAL(5,2) NOT NULL,
    starts_at TIMESTAMP NOT NULL,
    ends_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_offers_product FOREIGN KEY (product_id) REFERENCES products(product_id),
    INDEX idx_offers_product_id (product_id),
    INDEX idx_offers_active_window (starts_at, ends_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orders (
    order_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    total_amount DECIMAL(10,2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    INDEX idx_orders_customer_id (customer_id),
    INDEX idx_orders_status (status),
    INDEX idx_orders_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_history (
    history_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id BIGINT NOT NULL,
    previous_status VARCHAR(30),
    new_status VARCHAR(30) NOT NULL,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    changed_by VARCHAR(50) NOT NULL DEFAULT 'order-lambda',
    CONSTRAINT fk_order_history_order FOREIGN KEY (order_id) REFERENCES orders(order_id),
    INDEX idx_order_history_order_id (order_id),
    INDEX idx_order_history_changed_at (changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id BIGINT NOT NULL,
    product_id INT NOT NULL,
    product_name_snapshot VARCHAR(150) NOT NULL,
    quantity INT NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    CONSTRAINT fk_order_items_order FOREIGN KEY (order_id) REFERENCES orders(order_id),
    CONSTRAINT fk_order_items_product FOREIGN KEY (product_id) REFERENCES products(product_id),
    INDEX idx_order_items_order_id (order_id),
    INDEX idx_order_items_product_id (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

SAMPLE_DATA_SQL = """
INSERT INTO products (name, description, price, category, stock_count)
SELECT * FROM (SELECT 'Wireless Mouse', 'Ergonomic 2.4GHz wireless mouse', 19.99, 'Electronics', 42) AS tmp
WHERE NOT EXISTS (SELECT 1 FROM products WHERE name = 'Wireless Mouse') LIMIT 1;

INSERT INTO products (name, description, price, category, stock_count)
SELECT * FROM (SELECT 'Desk Lamp', 'LED desk lamp with adjustable brightness', 24.50, 'Home', 4) AS tmp
WHERE NOT EXISTS (SELECT 1 FROM products WHERE name = 'Desk Lamp') LIMIT 1;

INSERT INTO products (name, description, price, category, stock_count)
SELECT * FROM (SELECT 'Notebook Set', 'Pack of 3 ruled notebooks', 8.99, 'Office', 19) AS tmp
WHERE NOT EXISTS (SELECT 1 FROM products WHERE name = 'Notebook Set') LIMIT 1;

INSERT INTO customers (name, email)
SELECT * FROM (SELECT 'Test Customer', 'test.customer@example.com') AS tmp
WHERE NOT EXISTS (SELECT 1 FROM customers WHERE email = 'test.customer@example.com') LIMIT 1;
"""


def lambda_handler(event, context):
    username = ssm.get_parameter(Name=f"/cloudmart/{ENVIRONMENT}/db/username", WithDecryption=True)["Parameter"]["Value"]
    password = ssm.get_parameter(Name=f"/cloudmart/{ENVIRONMENT}/db/password", WithDecryption=True)["Parameter"]["Value"]
    host = os.environ.get("DB_HOST")

    conn = pymysql.connect(host=host, user=username, password=password, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            for statement in SCHEMA_SQL.strip().split(";"):
                if statement.strip():
                    cur.execute(statement)
            conn.commit()

            cur.execute("USE cloudmart")
            for statement in SAMPLE_DATA_SQL.strip().split(";"):
                if statement.strip():
                    cur.execute(statement)
            conn.commit()

        print(json.dumps({"level": "INFO", "message": "Schema and sample data applied successfully"}))
        return {"statusCode": 200, "body": json.dumps({"status": "schema applied"})}
    except Exception as e:
        print(json.dumps({"level": "ERROR", "message": "Schema apply failed", "error": str(e)}))
        return {"statusCode": 500, "body": json.dumps({"status": "failed", "error": str(e)})}
    finally:
        conn.close()
