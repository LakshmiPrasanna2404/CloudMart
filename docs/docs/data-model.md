# CloudMart Data Model

## 1. Overview

CloudMart uses Amazon RDS for MySQL as its persistent relational database.

Database name:

```text
cloudmart
```

The database stores customers, authentication records, products, offers, orders, order items and order history.

## 2. Entity relationship

```text
customers
   |
   +------< login_access
   |
   +------< orders
                 |
                 +------< order_items >------ products
                 |
                 +------< order_history

products
   |
   +------< offers
```

## 3. Tables

### 3.1 customers

Stores customer information.

| Column | Description |
|---|---|
| `customer_id` | Primary key |
| `name` | Customer name |
| `email` | Customer email |
| `created_at` | Creation timestamp |

Relationship:

```text
customers.customer_id
        |
        +---- login_access.customer_id
        |
        +---- orders.customer_id
```

### 3.2 login_access

Stores customer authentication information.

| Column | Description |
|---|---|
| `login_id` | Primary key |
| `customer_id` | Customer foreign key |
| `password_lookup` | Lookup fingerprint |
| `password_hash` | Password hash |
| `is_active` | Authentication status |
| `created_at` | Creation timestamp |
| `last_login_at` | Last login timestamp |
| `revoked_at` | Credential revocation timestamp |

Plain-text passwords are not stored.

### 3.3 products

Stores the product catalog and inventory.

| Column | Description |
|---|---|
| `product_id` | Primary key |
| `name` | Product name |
| `description` | Product description |
| `price` | Product price |
| `category` | Product category |
| `stock_count` | Available inventory |
| `is_active` | Active/inactive state |
| `updated_at` | Last update timestamp |

`is_active = 1` represents an active product. The application's delete behavior can deactivate a product instead of physically removing it.

### 3.4 offers

Stores product offers/discounts.

| Column | Description |
|---|---|
| `offer_id` | Primary key |
| `product_id` | Product foreign key |
| `discount_percentage` | Discount value |
| `starts_at` | Offer start |
| `ends_at` | Offer end |
| `created_at` | Creation timestamp |

Relationship:

```text
products.product_id
        |
        +---- offers.product_id
```

### 3.5 orders

Stores customer orders.

| Column | Description |
|---|---|
| `order_id` | Primary key |
| `customer_id` | Customer foreign key |
| `status` | Current order status |
| `total_amount` | Order total |
| `created_at` | Creation timestamp |
| `updated_at` | Last update timestamp |

Typical order lifecycle:

```text
PENDING -> CONFIRMED
             |
             +---- CANCELLED
```

The exact allowed transitions are enforced by the Order Lambda.

### 3.6 order_items

Stores individual products within an order.

| Column | Description |
|---|---|
| `order_item_id` | Primary key |
| `order_id` | Order foreign key |
| `product_id` | Product foreign key |
| `product_name_snapshot` | Product name at purchase time |
| `quantity` | Purchased quantity |
| `unit_price` | Price at purchase time |

The snapshot columns preserve historical order information if the product is later updated or deactivated.

### 3.7 order_history

Stores order status transitions.

| Column | Description |
|---|---|
| `history_id` | Primary key |
| `order_id` | Order foreign key |
| `previous_status` | Previous status |
| `new_status` | New status |
| `changed_at` | Change timestamp |
| `changed_by` | Service/user responsible for change |

## 4. Relationships

### Customer to orders

```text
One customer
     |
     +---- many orders
```

### Order to order items

```text
One order
     |
     +---- many order_items
```

### Product to order items

```text
One product
     |
     +---- many order_items
```

### Order to history

```text
One order
     |
     +---- many order_history records
```

### Product to offers

```text
One product
     |
     +---- many offers
```

## 5. Order placement transaction

The Order Lambda performs the order operation as a database transaction.

```text
Validate customer
       |
Validate product
       |
Validate stock
       |
Calculate total
       |
BEGIN TRANSACTION
       |
Create orders row
       |
Create order_items rows
       |
Decrease stock_count
       |
Write order_history
       |
Set order status
       |
COMMIT
       |
Send customer email
       |
Publish events / metrics
```

This keeps order creation and inventory changes consistent.

## 6. Order cancellation transaction

```text
Find order
    |
Lock order
    |
Read order items
    |
Restore stock
    |
Update order status = CANCELLED
    |
Write order_history
    |
COMMIT
    |
Send customer cancellation email
    |
Publish cancellation event / metric
```

The cancellation email does not expose database implementation details.

## 7. Inventory behavior

Inventory is represented by:

```text
products.stock_count
```

When an order is placed:

```text
new_stock = current_stock - quantity
```

When a cancellable order is cancelled:

```text
new_stock = current_stock + quantity
```

Low-stock activity produces the `LowStockEvents` CloudWatch custom metric.

## 8. Reporting data

Reports read from order data and calculate reporting values such as:

- order count
- revenue
- order/product details

The Report Lambda writes CSV files to the CloudFormation-managed S3 reports bucket.

Report paths include:

```text
reports/24hours/
reports/monthly/
```

## 9. Data security

- Database credentials are stored outside source code.
- SSM Parameter Store uses `SecureString` for protected parameters.
- RDS is private.
- Lambda database access is restricted through security groups.
- Customer password material is hashed rather than stored as plaintext.
- Historical order data is preserved through order items and status history.
