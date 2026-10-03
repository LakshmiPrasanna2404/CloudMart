# CloudMart Architecture

## 1. Overview

CloudMart is an AWS-based e-commerce application deployed in `us-east-1` using AWS CloudFormation and GitHub Actions. The application uses AWS Lambda for authentication, product operations, order processing and reporting, Amazon RDS for MySQL for persistent data, EventBridge for application events, SNS for notifications, SES for customer emails, S3 for reports/artifacts, CloudWatch for monitoring, SSM Parameter Store for protected configuration, and an EC2-hosted Flask operations dashboard.

> **Environment:** `prod`
>
> **AWS Region:** `us-east-1`
>
> **Infrastructure:** AWS CloudFormation
>
> **CI/CD:** GitHub Actions with AWS OIDC

## 2. High-level architecture

```text
                         GitHub Repository
                                |
                                v
                     GitHub Actions / OIDC
                                |
                                v
                         CloudFormation
                                |
             +------------------+------------------+
             |                  |                  |
             v                  v                  v
      cloudmart-network   cloudmart-data    cloudmart-iam
             |                  |                  |
             +------------------+------------------+
                                |
                                v
                         cloudmart-app
                    +-----------+-----------+
                    |           |           |
                    v           v           v
              Authorizer   Product Lambda  Order Lambda
                    |           |           |
                    |           +-----+-----+
                    |                 |
                    +-----------------v
                              RDS MySQL
                                  |
                    +-------------+-------------+
                    |                           |
                    v                           v
              EventBridge                    SES
                    |                     customer email
                    |
              +-----+------+
              |            |
              v            v
             SNS       application events
              |
              v
        notification email

Order/Inventory events
        |
        v
CloudWatch custom metrics
        |
        +--> Dashboard
        |
        +--> 9 alarms
                  |
                  v
              SNS topic

Report Lambda
     |
     v
 RDS MySQL
     |
     v
 CSV reports
     |
     v
 S3 Reports Bucket
     |
     v
 EC2 Flask Dashboard

SSM Parameter Store
     |
     +--> DB credentials
     +--> admin token
     +--> products token
```

## 3. AWS networking

The network stack creates the CloudMart VPC and the networking resources required by the application.

### Public subnet

The public subnet hosts the EC2 operations dashboard.

### Private subnets

The private subnets are used for RDS and VPC-connected Lambda workloads. RDS is not publicly accessible.

### Security groups

Security groups restrict traffic between:

- EC2 dashboard
- Lambda functions
- RDS MySQL
- VPC endpoints

RDS MySQL uses TCP port `3306` and accepts traffic from the required application security groups rather than from the public internet.

### VPC endpoints

CloudMart uses VPC endpoints for AWS services required by private workloads, including SSM/SSM Messages, EventBridge, SNS, Lambda, S3 and CloudWatch monitoring. This supports the design without requiring a NAT Gateway.

## 4. Application components

| Component | Purpose |
|---|---|
| Lambda Authorizer | Validates application tokens and controls protected access |
| Product Lambda | Product CRUD and inventory-related operations |
| Order Lambda | Order placement, retrieval, cancellation and inventory updates |
| Report Lambda | Generates previous-day and monthly CSV reports |
| Apply Schema Lambda | Initializes the MySQL schema |
| RDS MySQL | Persistent application database |
| EventBridge | Application event routing |
| SNS | Low-stock and monitoring notifications |
| SES | Customer order confirmation/cancellation email |
| S3 | Reports and deployment artifacts |
| EC2 Flask Dashboard | Operational UI |
| CloudWatch | Logs, metrics, dashboard and alarms |
| SSM Parameter Store | Protected runtime parameters |

## 5. Order flow

```text
Client
  |
  v
Lambda Authorizer
  |
  v
Order Lambda
  |
  +--> Validate customer
  |
  +--> Validate product/stock
  |
  +--> MySQL transaction
  |      |
  |      +--> orders
  |      +--> order_items
  |      +--> inventory
  |      +--> order_history
  |
  +--> OrdersPlaced metric
  |
  +--> EventBridge events
  |
  +--> SES customer confirmation
  |
  v
Response
```

For cancellation:

```text
Client
  |
  v
Order Lambda
  |
  +--> Lock order
  +--> Restore inventory
  +--> Update order status
  +--> Write order history
  +--> OrdersCancelled metric
  +--> Customer cancellation email through SES
  |
  v
Response
```

## 6. Product/inventory flow

```text
Product Lambda
     |
     +--> RDS products table
     |
     +--> ProductsCreated
     +--> ProductsUpdated
     +--> ProductsDeleted
     |
     +--> LowStockEvents
     |
     v
EventBridge / SNS where applicable
```

Product deletion uses the application's active/inactive behavior so historical order references remain valid.

## 7. Reporting flow

```text
Scheduled Report Lambda
          |
          v
       RDS MySQL
          |
          v
       CSV report
          |
          v
      S3 Reports
          |
          v
   EC2 Flask Dashboard
```

The report system currently supports:

- previous-day order/revenue reports
- monthly order/revenue reports

## 8. Monitoring flow

CloudWatch collects AWS service metrics and CloudMart custom metrics.

Custom application metrics include:

- `OrdersPlaced`
- `OrdersCancelled`
- `OrdersFailed`
- `LowStockEvents`
- `ProductsCreated`
- `ProductsUpdated`
- `ProductsDeleted`

The operations dashboard also includes Lambda latency (p95) and Lambda throttles as metrics.

The environment currently has **9 CloudWatch alarms**. Each alarm is connected to the monitoring SNS notification mechanism.

## 9. CloudFormation stack structure

| Order | Stack | Responsibility |
|---|---|---|
| 1 | `cloudmart-network` | VPC, subnets, routes, security groups, endpoints |
| 2 | `cloudmart-data` | RDS MySQL and S3 buckets |
| 3 | `cloudmart-iam` | Lambda/EC2 IAM roles and policies |
| 4 | `cloudmart-app` | Lambda functions, EventBridge, SNS, EC2 dashboard |
| 5 | `cloudmart-monitoring` | CloudWatch dashboard, metrics and 9 alarms |
| 6 | `cloudmart-report` | Reporting Lambda and scheduled report resources |

All environment-specific names use the `Environment` parameter.

## 10. Security model

- RDS is private.
- Database credentials are stored in SSM Parameter Store as `SecureString`.
- Authentication tokens are stored in SSM Parameter Store.
- GitHub Actions uses OIDC rather than long-lived AWS access keys.
- Lambda and EC2 use dedicated IAM roles.
- IAM policies are scoped to the required services/resources; AWS APIs that require `Resource: "*"` are constrained where service-supported conditions allow it.
- No application passwords or AWS credentials are stored in source code.
- Infrastructure resources are managed through CloudFormation.

## 11. Deployment architecture

```text
GitHub push
    |
    v
GitHub Actions
    |
    v
AWS OIDC
    |
    v
CloudFormation
    |
    +--> network
    +--> data
    +--> iam
    +--> app
    +--> monitoring
    +--> report
```

This structure allows the complete environment to be deleted and recreated from the repository and deployment workflow.
