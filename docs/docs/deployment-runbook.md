# CloudMart Deployment Runbook

**Project:** CloudMart  
**AWS Region:** `us-east-1`  
**Environment:** `prod`  
**Infrastructure:** AWS CloudFormation  
**CI/CD:** GitHub Actions  
**AWS authentication:** GitHub Actions OIDC

> This runbook is written for the current CloudMart architecture. The six CloudFormation stacks and their names are the source of truth. Do not manually create replacement AWS resources.

## 1. Purpose

This runbook explains how to:

1. prepare the repository,
2. deploy CloudMart through GitHub Actions,
3. verify every CloudFormation stack,
4. verify application resources,
5. verify monitoring and reports,
6. troubleshoot deployment failures,
7. perform a complete teardown,
8. redeploy the environment from IaC.

The objective is that the environment can be recreated without manually rebuilding AWS resources.

## 2. CloudFormation stack order

| Order | Stack | Main responsibility |
|---|---|---|
| 1 | `cloudmart-network` | VPC, subnets, routing, security groups, VPC endpoints |
| 2 | `cloudmart-data` | RDS MySQL and S3 buckets |
| 3 | `cloudmart-iam` | Lambda/EC2 IAM roles and policies |
| 4 | `cloudmart-app` | Lambda, EventBridge, SNS and EC2 dashboard |
| 5 | `cloudmart-monitoring` | CloudWatch dashboard, metrics and 9 alarms |
| 6 | `cloudmart-report` | Report Lambda and scheduled reports |

### Why the order matters

- Network resources are required by RDS, Lambda and EC2.
- Data resources provide the database and S3 resources consumed by later stacks.
- IAM roles are required by Lambda and EC2.
- The application stack creates the main Lambda/event resources.
- Monitoring depends on application resources/metrics.
- Reporting depends on the database and application environment.

## 3. Prerequisites

Before deployment, confirm:

- The intended branch contains the latest code.
- All six CloudFormation templates are committed.
- Lambda source files are committed.
- Dashboard source is committed.
- GitHub Actions workflow is committed.
- Required SSM parameters exist.
- GitHub Actions AWS OIDC configuration is available.
- The deployment role has the permissions required by the CloudFormation workflow.

### Required SSM parameters

```text
/cloudmart/prod/db/username
/cloudmart/prod/db/password
/cloudmart/prod/auth/admin-token
/cloudmart/prod/auth/products-token
```

Check existence without exposing values:

```bash
aws ssm get-parameters \
  --names \
    /cloudmart/prod/db/username \
    /cloudmart/prod/db/password \
    /cloudmart/prod/auth/admin-token \
    /cloudmart/prod/auth/products-token \
  --with-decryption \
  --region us-east-1 \
  --query "Parameters[*].[Name,Type]" \
  --output table
```

Do not print secret values in logs or documentation.

## 4. GitHub Actions deployment

Normal infrastructure deployment must be performed through the repository workflow.

The deployment lifecycle is:

```text
Reviewed code
    |
    v
GitHub repository
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
    |
    +--> data
    |
    +--> iam
    |
    +--> app
    |
    +--> monitoring
    |
    +--> report
    |
    v
Post-deployment verification
```

### Deployment rules

- Do not manually create duplicate AWS resources.
- Do not manually modify CloudFormation-managed IAM policies.
- Do not manually create replacement EC2/RDS/SNS/Lambda resources.
- Fix infrastructure changes in the repository and redeploy through the workflow.
- Never commit credentials or tokens.

## 5. Deploying the workflow

1. Commit reviewed code to the intended branch.
2. Push the commit.
3. Open the repository's **Actions** tab.
4. Select the CloudMart deployment workflow.
5. Run the workflow.
6. Monitor each stack deployment.
7. Wait for every job to finish successfully.
8. Record the workflow run and commit SHA for the final review.

## 6. CloudFormation verification

Run:

```bash
for stack in \
  cloudmart-network \
  cloudmart-data \
  cloudmart-iam \
  cloudmart-app \
  cloudmart-monitoring \
  cloudmart-report
do
  echo "===== $stack ====="
  aws cloudformation describe-stacks \
    --stack-name "$stack" \
    --region us-east-1 \
    --query "Stacks[0].[StackStatus,StackStatusReason]" \
    --output table
done
```

Expected successful status:

```text
CREATE_COMPLETE
```

or:

```text
UPDATE_COMPLETE
```

Any `FAILED`, `ROLLBACK`, or unexpected `DELETE_*` state must be investigated before final submission.

## 7. Verify stack resources

Example:

```bash
aws cloudformation list-stack-resources \
  --stack-name cloudmart-app \
  --region us-east-1 \
  --output table
```

Repeat for all six stacks.

The objective is to confirm that the AWS resources used by CloudMart are CloudFormation-managed.

## 8. Verify application resources

### Lambda functions

Confirm the expected functions exist:

```bash
aws lambda list-functions \
  --region us-east-1 \
  --query "Functions[?starts_with(FunctionName,'cloudmart-prod-')].[FunctionName,Runtime,State]" \
  --output table
```

Expected application functions include the authorizer, product, order, report and schema/application functions defined by the current app/report templates.

### RDS

```bash
aws rds describe-db-instances \
  --region us-east-1 \
  --query "DBInstances[?DBInstanceIdentifier=='cloudmart-prod-db'].[DBInstanceIdentifier,DBInstanceStatus,PubliclyAccessible,Endpoint.Address,Endpoint.Port]" \
  --output table
```

Expected:

- DB status: `available`
- Publicly accessible: `False`
- Port: `3306`

### S3 reports

Retrieve the reports bucket from the `cloudmart-data` stack output and verify:

```bash
aws s3 ls s3://<REPORTS_BUCKET_NAME>/reports/ \
  --recursive \
  --region us-east-1
```

## 9. Verify monitoring

The current monitoring deployment contains **9 alarms**.

Verify:

```bash
aws cloudwatch describe-alarms \
  --region us-east-1 \
  --alarm-name-prefix cloudmart-prod- \
  --query "MetricAlarms[*].[AlarmName,StateValue,AlarmActions]" \
  --output table
```

Every alarm must have a monitoring SNS action.

### Dashboard metrics

Confirm the dashboard includes:

- Orders placed
- Orders cancelled
- Orders failed
- Low-stock events
- Products created
- Products updated
- Products deleted
- Lambda invocations
- Lambda errors
- Lambda duration
- Lambda p95 latency
- Lambda throttles
- RDS CPU
- RDS free storage
- RDS connections

Latency and throttle items are metrics only; no additional alarms are required.

## 10. CRUD verification

CRUD testing must be performed against the deployed application.

### Products

Verify:

```text
Create product
Read/list products
Read individual product
Update product
Delete/deactivate product
```

Record the actual test result and do not mark a test as passed without executing it.

### Orders

Verify:

```text
Place order
Read/list orders
Cancel order
Verify inventory deduction
Verify inventory restoration after cancellation
```

### Authentication

Verify:

```text
Missing token -> rejected
Invalid token -> rejected
Valid authorized token -> accepted
Unauthorized access -> rejected
```

## 11. Email verification

### Order confirmation

The Order Lambda sends the confirmation email through SES to the customer's email address after a successful order.

Verify:

- correct order ID,
- customer ID,
- product,
- total amount,
- `CONFIRMED` status.

### Cancellation

The cancellation email is also intended for the customer.

Verify:

- correct order ID,
- customer ID,
- product,
- `CANCELLED` status.

Do not expose credentials or sensitive configuration in the email.

## 12. Report verification

The Report Lambda generates:

```text
reports/24hours/
reports/monthly/
```

Verify the previous-day report:

```bash
aws s3 ls s3://<REPORTS_BUCKET_NAME>/reports/24hours/ \
  --recursive \
  --region us-east-1
```

Verify monthly reports:

```bash
aws s3 ls s3://<REPORTS_BUCKET_NAME>/reports/monthly/ \
  --recursive \
  --region us-east-1
```

The EC2 dashboard should expose the available report files.

## 13. EC2 dashboard verification

Confirm the dashboard instance exists:

```bash
aws ec2 describe-instances \
  --region us-east-1 \
  --filters "Name=tag:Name,Values=cloudmart-prod-dashboard" \
  --query "Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress,PrivateIpAddress]" \
  --output table
```

Confirm the instance is managed through the CloudFormation app stack and has the expected IAM instance profile.

The dashboard should provide access to:

- operational information,
- recent orders,
- inventory information,
- generated reports.

## 14. Troubleshooting

### CloudFormation failure

```bash
aws cloudformation describe-stack-events \
  --stack-name <STACK_NAME> \
  --region us-east-1 \
  --query "StackEvents[?contains(ResourceStatus, 'FAILED')].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason]" \
  --output table
```

Fix the source/template/workflow and redeploy through GitHub Actions.

### Lambda cannot connect to RDS

Check:

- Lambda VPC configuration.
- Lambda security group.
- RDS security group.
- TCP port `3306`.
- RDS endpoint.
- SSM database credentials.
- Database schema.

### Report is missing

Check:

```bash
aws logs tail /aws/lambda/cloudmart-prod-report-generate \
  --region us-east-1 \
  --since 1h
```

Then verify the S3 report prefix.

### Dashboard unavailable

Check:

- EC2 instance state.
- Security group ports.
- Flask/Nginx service.
- SSM connectivity.
- Dashboard files/artifacts.
- CloudFormation app stack status.

## 15. Full teardown and clean redeployment

This procedure is a destructive test of the IaC design.

**Warning:** the data stack contains the RDS database. A full teardown can remove the database and its data according to the template's deletion policies.

### 15.1 Empty generated reports

Before deleting the data stack, empty the reports bucket if it contains objects:

```bash
aws s3 rm s3://<REPORTS_BUCKET_NAME> \
  --recursive \
  --region us-east-1
```

Verify:

```bash
aws s3 ls s3://<REPORTS_BUCKET_NAME> \
  --recursive \
  --region us-east-1
```

Expected: no objects.

Do not delete the SSM bootstrap parameters as part of this test.

### 15.2 Delete in reverse dependency order

```bash
aws cloudformation delete-stack \
  --stack-name cloudmart-report \
  --region us-east-1

aws cloudformation wait stack-delete-complete \
  --stack-name cloudmart-report \
  --region us-east-1

aws cloudformation delete-stack \
  --stack-name cloudmart-monitoring \
  --region us-east-1

aws cloudformation wait stack-delete-complete \
  --stack-name cloudmart-monitoring \
  --region us-east-1

aws cloudformation delete-stack \
  --stack-name cloudmart-app \
  --region us-east-1

aws cloudformation wait stack-delete-complete \
  --stack-name cloudmart-app \
  --region us-east-1

aws cloudformation delete-stack \
  --stack-name cloudmart-iam \
  --region us-east-1

aws cloudformation wait stack-delete-complete \
  --stack-name cloudmart-iam \
  --region us-east-1

aws cloudformation delete-stack \
  --stack-name cloudmart-data \
  --region us-east-1

aws cloudformation wait stack-delete-complete \
  --stack-name cloudmart-data \
  --region us-east-1

aws cloudformation delete-stack \
  --stack-name cloudmart-network \
  --region us-east-1

aws cloudformation wait stack-delete-complete \
  --stack-name cloudmart-network \
  --region us-east-1
```

Delete one stack, wait for completion, then continue.

### 15.3 Confirm deletion

```bash
aws cloudformation list-stacks \
  --region us-east-1 \
  --stack-status-filter \
    CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
  --query "StackSummaries[?starts_with(StackName,'cloudmart-')].[StackName,StackStatus]" \
  --output table
```

### 15.4 Redeploy

Run the GitHub Actions workflow.

The expected result is that all six stacks can be created again from the repository.

## 16. Final deployment evidence

For final review, record:

- GitHub Actions workflow run.
- Commit SHA.
- Six CloudFormation stack statuses.
- RDS status.
- Lambda deployment status.
- SNS subscriptions.
- EventBridge rules.
- S3 report generation.
- EC2 dashboard availability.
- CloudWatch dashboard.
- 9 alarm states and SNS actions.
- CRUD test evidence.
- Email test evidence.

Do not record passwords, tokens, access keys or other secrets.

## 17. Final IaC rule

CloudMart infrastructure must remain reproducible from:

```text
GitHub repository
        +
GitHub Actions
        +
AWS OIDC
        +
CloudFormation
```

Do not manually create replacement resources to bypass a deployment failure.
