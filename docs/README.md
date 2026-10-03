# CloudMart

CloudMart is an AWS-based e-commerce application built with Python and AWS managed services. The infrastructure is defined using AWS CloudFormation and deployed through GitHub Actions using AWS OIDC.

> **Final submission status:** Update the values below only after verifying the live redeployment.
>
> **Dashboard URL:** `<add verified dashboard URL>`
>
> **AWS Region:** `us-east-1`
>
> **Environment:** `prod`

## Project overview

CloudMart provides:

- Product CRUD.
- Inventory management.
- Order placement and retrieval.
- Order cancellation.
- Customer order confirmation/cancellation emails.
- Low-stock notifications.
- Previous-day reporting.
- Monthly reporting.
- EC2 operations dashboard.
- CloudWatch metrics, dashboard and alarms.
- Infrastructure-as-code deployment.
- Repeatable clean deployment through GitHub Actions.

The architecture uses RDS MySQL rather than DynamoDB and does not use API Gateway or SQS.

## CloudFormation stacks

| Stack | Template | Responsibility |
|---|---|---|
| `cloudmart-network` | `network-stack.yaml` | VPC, subnets, routing, security groups and VPC endpoints |
| `cloudmart-data` | `data-stack.yaml` | RDS MySQL and S3 buckets |
| `cloudmart-iam` | `iam-stack.yaml` | Lambda and EC2 IAM roles/policies |
| `cloudmart-app` | `app-stack.yaml` | Lambda functions, EventBridge, SNS and EC2 dashboard |
| `cloudmart-monitoring` | `monitoring-stack.yaml` | CloudWatch dashboard, metrics and 9 alarms |
| `cloudmart-report` | `report-stack.yaml` | Report Lambda and scheduled reporting |

All templates accept an `Environment` parameter so resource names can be environment-specific.

## Architecture

```text
GitHub
   |
   v
GitHub Actions / OIDC
   |
   v
CloudFormation
   |
   +--> Network
   +--> Data
   +--> IAM
   +--> App
   +--> Monitoring
   +--> Report
            |
            v
     AWS CloudMart Environment

Authorizer Lambda
        |
        +--> Product Lambda --> RDS MySQL
        |
        +--> Order Lambda ---> RDS MySQL
        |          |
        |          +--> EventBridge
        |          +--> SES
        |
        +--> Report Lambda --> S3 --> EC2 Dashboard

CloudWatch --> Dashboard + 9 Alarms --> SNS
SSM Parameter Store --> protected runtime configuration
```

See [`docs/architecture.md`](docs/architecture.md).

## Repository layout

```text
CloudMart/
├── README.md
├── .github/
│   └── workflows/
│       └── deploy.yaml
├── cloudformation/
│   ├── network-stack.yaml
│   ├── data-stack.yaml
│   ├── iam-stack.yaml
│   ├── app-stack.yaml
│   ├── monitoring-stack.yaml
│   └── report-stack.yaml
├── lambda/
│   ├── authorizer/
│   ├── product/
│   ├── order/
│   ├── report/
│   └── apply-schema/
├── dashboard/
└── docs/
    ├── architecture.md
    ├── data-model.md
    └── deployment-runbook.md
```

## Application components

| Component | Purpose |
|---|---|
| Lambda Authorizer | Authentication/authorization |
| Product Lambda | Product CRUD and inventory operations |
| Order Lambda | Orders and inventory transactions |
| Report Lambda | CSV reporting |
| Apply Schema Lambda | RDS schema initialization |
| RDS MySQL | Application database |
| EventBridge | Application event routing |
| SNS | Notifications |
| SES | Customer emails |
| S3 | Reports/artifacts |
| EC2 Flask Dashboard | Operations dashboard |
| CloudWatch | Monitoring |
| SSM Parameter Store | Protected parameters |

## Data model

The database contains:

```text
customers
login_access
products
offers
orders
order_items
order_history
```

Main relationships:

```text
customers
   |
   +---- orders
           |
           +---- order_items ---- products
           |
           +---- order_history

products
   |
   +---- offers
```

See [`docs/data-model.md`](docs/data-model.md).

## Order processing

A normal order follows:

```text
Customer request
      |
      v
Authorizer
      |
      v
Order Lambda
      |
      +--> Validate customer
      +--> Validate product/stock
      +--> Create order
      +--> Create order items
      +--> Deduct inventory
      +--> Update order history
      +--> Confirm order
      +--> Send customer email
      +--> Publish events/metrics
```

Cancellation restores inventory, updates the order state and history, publishes the appropriate metrics/events and sends a cancellation email to the customer.

## Product and inventory

Product operations include:

```text
Create
Read/List
Update
Delete/Deactivate
```

Custom CloudWatch metrics include:

```text
ProductsCreated
ProductsUpdated
ProductsDeleted
LowStockEvents
```

## Monitoring

CloudWatch custom application metrics include:

```text
OrdersPlaced
OrdersCancelled
OrdersFailed
LowStockEvents
ProductsCreated
ProductsUpdated
ProductsDeleted
```

The dashboard also includes:

- Lambda invocations
- Lambda errors
- Lambda duration
- Lambda p95 latency
- Lambda throttles
- RDS CPU
- RDS free storage
- RDS connections

### CloudWatch alarms

The current deployment contains **9 alarms**:

1. Authorizer duration
2. Authorizer Lambda errors
3. Product Lambda errors
4. Order Lambda errors
5. Report Lambda errors
6. RDS CPU
7. RDS free storage
8. Orders failed
9. Low-stock events

Each alarm has a monitoring SNS action.

Latency and throttles are intentionally dashboard metrics only; additional alarms are not required.

## Reports

The Report Lambda generates CSV files for:

```text
Previous-day orders/revenue
Monthly orders/revenue
```

S3 prefixes:

```text
reports/24hours/
reports/monthly/
```

The EC2 Flask dashboard provides access to generated reports.

## Authentication and secrets

CloudMart uses a Lambda authorizer.

Protected application credentials/configuration are stored in SSM Parameter Store.

Expected parameters include:

```text
/cloudmart/prod/db/username
/cloudmart/prod/db/password
/cloudmart/prod/auth/admin-token
/cloudmart/prod/auth/products-token
```

Sensitive values must not be committed to GitHub.

## Security

- RDS is private.
- Database access is controlled through security groups.
- Lambda and EC2 use dedicated IAM roles.
- GitHub Actions uses AWS OIDC.
- No long-lived AWS access keys are stored in the repository.
- Secrets are stored in SSM Parameter Store.
- IAM permissions are scoped to required services/resources, with AWS APIs that require `Resource: "*"` constrained where supported.
- AWS resources are managed through CloudFormation.

## Deployment

Normal deployment is performed through GitHub Actions.

Order:

```text
1. cloudmart-network
2. cloudmart-data
3. cloudmart-iam
4. cloudmart-app
5. cloudmart-monitoring
6. cloudmart-report
```

See [`docs/deployment-runbook.md`](docs/deployment-runbook.md).

## Clean redeployment

CloudMart is designed to support a complete teardown/redeployment test.

The reports S3 bucket must be emptied before deleting the data stack when generated reports are present. The full teardown also removes the RDS database according to the stack's deletion behavior.

The correct approach is:

```text
Delete report
   |
Delete monitoring
   |
Delete app
   |
Delete IAM
   |
Delete data
   |
Delete network
   |
Run GitHub Actions
   |
Recreate all six stacks
```

Do not manually recreate resources during the test.


