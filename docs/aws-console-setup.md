# AWS Console Setup Guide — End to End

> Complete step-by-step walkthrough for deploying the **CloudOps AI Agent** entirely through the AWS Management Console — no CLI required.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Enable Amazon Bedrock Access](#2-enable-amazon-bedrock-access)
3. [Create IAM Role for Lambda](#3-create-iam-role-for-lambda)
4. [Create the Lambda Function](#4-create-the-lambda-function)
5. [Build and Upload the Deployment Package](#5-build-and-upload-the-deployment-package)
6. [Configure Lambda Environment Variables](#6-configure-lambda-environment-variables)
7. [Create API Gateway](#7-create-api-gateway)
8. [Set Up CloudWatch Log Groups](#8-set-up-cloudwatch-log-groups)
9. [Create DynamoDB Table (Incident History)](#9-create-dynamodb-table-incident-history)
10. [Test End-to-End via Console](#10-test-end-to-end-via-console)
11. [Set Up CloudWatch Alarms for the Agent Itself](#11-set-up-cloudwatch-alarms-for-the-agent-itself)
12. [GitHub Actions OIDC Integration](#12-github-actions-oidc-integration)
13. [Post-Setup Validation Checklist](#13-post-setup-validation-checklist)
14. [Cost Estimate](#14-cost-estimate)
15. [Cleanup / Teardown](#15-cleanup--teardown)

---

## 1. Prerequisites

Before starting, ensure you have:

- [ ] An AWS account with administrator (or scoped) IAM access
- [ ] Python 3.11+ installed locally (for building the deployment package)
- [ ] The project cloned: `git clone https://github.com/YOUR_USERNAME/cloudops-ai-agent.git`
- [ ] AWS region decided — this guide uses **us-east-1** throughout. Replace with your chosen region.

> **Region note:** Amazon Bedrock (Amazon Nova models) is available in `us-east-1`, `us-west-2`, `eu-west-1`, and others. Check [Bedrock model availability](https://docs.aws.amazon.com/bedrock/latest/userguide/models-regions.html) for your region.

---

## 2. Enable Amazon Bedrock Access

Amazon Bedrock models require explicit model access — they are **not enabled by default**.

### 2.1 Open Bedrock Console

1. In the AWS Console, search for **"Bedrock"** in the top search bar.
2. Click **Amazon Bedrock**.
3. Ensure your region is set to **US East (N. Virginia)** `us-east-1` in the top-right corner.

### 2.2 Request Model Access

1. In the left sidebar, click **"Model access"** (under *Bedrock configurations*).
2. Click the **"Modify model access"** button (top right).
3. Find **Anthropic** in the provider list and expand it.
4. Check the box next to **Claude 3 Sonnet**.
5. Optionally also enable **Claude 3 Haiku** (cheaper for testing).
6. Click **"Next"** → review the End User License Agreement → click **"Submit"**.
7. Status changes from *Available to request* → **Access granted** (usually within 1–2 minutes).

> ⚠️ You must complete this step before deploying Lambda — the agent will fail to call Bedrock without it.

---

## 3. Create IAM Role for Lambda

### 3.1 Open IAM Console

1. Search for **"IAM"** → click **IAM**.
2. In the left sidebar, click **Roles** → **Create role**.

### 3.2 Configure Trusted Entity

1. **Trusted entity type:** AWS service
2. **Use case:** Lambda
3. Click **Next**.

### 3.3 Attach Permission Policies

Search for and attach the following **AWS managed policies**:

| Policy name | Purpose |
|-------------|---------|
| `AWSLambdaBasicExecutionRole` | Write Lambda logs to CloudWatch |
| `CloudWatchReadOnlyAccess` | Read CloudWatch metrics |
| `CloudWatchLogsReadOnlyAccess` | Read CloudWatch Logs |

Then click **Create policy** (inline, for Bedrock and Lambda concurrency):

1. Click **Create inline policy** → **JSON** tab.
2. Paste the following:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockInvoke",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": "arn:aws:bedrock:*::foundation-model/amazon.nova-pro-v1:0"
    },
    {
      "Sid": "LambdaConcurrency",
      "Effect": "Allow",
      "Action": ["lambda:PutFunctionConcurrency"],
      "Resource": "*"
    },
    {
      "Sid": "DynamoDBIncidentHistory",
      "Effect": "Allow",
      "Action": [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:*:*:table/cloudops-incidents"
    }
  ]
}
```

3. Click **Next** → Name it `CloudOpsAIAgentInlinePolicy` → **Create policy**.

### 3.4 Name and Create the Role

1. **Role name:** `cloudops-ai-agent-lambda-role`
2. **Description:** IAM role for CloudOps AI Agent Lambda function
3. Click **Create role**.
4. After creation, click into the role and **copy the Role ARN** (you'll need it for Lambda).

```
arn:aws:iam::YOUR_ACCOUNT_ID:role/cloudops-ai-agent-lambda-role
```

---

## 4. Create the Lambda Function

### 4.1 Open Lambda Console

1. Search for **"Lambda"** → click **AWS Lambda**.
2. Click **Create function** (top right).

### 4.2 Basic Configuration

| Field | Value |
|-------|-------|
| **Author from scratch** | ✓ selected |
| **Function name** | `cloudops-ai-agent` |
| **Runtime** | Python 3.11 |
| **Architecture** | x86_64 |

### 4.3 Execution Role

1. Under **Permissions** → **Change default execution role**.
2. Select **Use an existing role**.
3. In the dropdown, choose `cloudops-ai-agent-lambda-role`.

### 4.4 Advanced Settings

1. Expand **Advanced settings**.
2. Check **Enable function URL** → **Auth type:** NONE (for testing; use AWS_IAM for production).

Click **Create function**.

### 4.5 Configure Function Settings

After creation, go to **Configuration** tab:

**General configuration → Edit:**
| Setting | Value |
|---------|-------|
| Memory | 512 MB |
| Timeout | 5 min 0 sec (300 seconds) |
| Description | AI-powered CloudOps incident investigation agent |

Click **Save**.

---

## 5. Build and Upload the Deployment Package

Do this on your **local machine** (where the project is cloned):

### 5.1 Build the ZIP

```bash
cd cloudops-ai-agent

# Create a clean package directory
mkdir -p lambda_package

# Install production dependencies into the package directory
pip install -r requirements.txt --target lambda_package/ --platform manylinux2014_x86_64 --only-binary=:all: --quiet

# Copy source code
cp -r agents tools app.py lambda_package/

# Create the ZIP (exclude bytecode and cache)
cd lambda_package
zip -r ../cloudops-ai-agent.zip . -x "*.pyc" -x "*/__pycache__/*"
cd ..

# Verify size (should be < 50 MB)
du -sh cloudops-ai-agent.zip
```

### 5.2 Upload to Lambda Console

**Option A — Direct upload (< 50 MB):**

1. In the Lambda Console → your function → **Code** tab.
2. Click **Upload from** → **.zip file**.
3. Click **Upload** → select `cloudops-ai-agent.zip`.
4. Click **Save**.

**Option B — Via S3 (recommended for larger packages):**

1. Go to **S3** Console → create a bucket (e.g. `cloudops-ai-agent-deployments-YOUR_ACCOUNT_ID`).
   - Region: same as Lambda (us-east-1)
   - Block all public access: ✓ enabled
2. Upload `cloudops-ai-agent.zip` to the bucket.
3. Back in Lambda → **Code** → **Upload from** → **Amazon S3 location**.
4. Paste the S3 URI: `s3://cloudops-ai-agent-deployments-YOUR_ACCOUNT/cloudops-ai-agent.zip`
5. Click **Save**.

### 5.3 Set the Handler

1. **Code** tab → **Runtime settings** → **Edit**.
2. **Handler:** `app.lambda_handler`
3. Click **Save**.

---

## 6. Configure Lambda Environment Variables

1. Go to **Configuration** tab → **Environment variables** → **Edit**.
2. Click **Add environment variable** for each row:

| Key | Value |
|-----|-------|
| `AWS_REGION` | `us-east-1` |
| `BEDROCK_MODEL_ID` | `anthropic.claude-3-sonnet-20240229-v1:0` |
| `AUTO_EXECUTE` | `false` |
| `DRY_RUN` | `true` |
| `LOG_LEVEL` | `INFO` |
| `MAX_LOG_LOOKBACK` | `240` |

3. Click **Save**.

> **Security note:** For sensitive values (API keys, webhook URLs), use **AWS Systems Manager Parameter Store** or **Secrets Manager** instead of plain environment variables. Reference them via `ssm:` or `secretsmanager:` in your Lambda configuration.

---

## 7. Create API Gateway

### 7.1 Open API Gateway Console

1. Search for **"API Gateway"** → click **API Gateway**.
2. Click **Create API**.
3. Choose **REST API** → **Build**.

### 7.2 Configure the API

| Field | Value |
|-------|-------|
| **Protocol** | REST |
| **Create new API** | ✓ |
| **API name** | `cloudops-ai-agent` |
| **Description** | CloudOps AI Agent incident investigation API |
| **Endpoint Type** | Regional |

Click **Create API**.

### 7.3 Create Resource and Method

1. In **Resources** panel, click **Actions** → **Create Resource**.
   - **Resource Name:** `investigate`
   - **Resource Path:** `/investigate`
   - ✓ Enable API Gateway CORS
   - Click **Create Resource**

2. With `/investigate` selected, click **Actions** → **Create Method** → choose **POST** → click the checkmark ✓.

3. **Integration type:** Lambda Function
4. ✓ **Use Lambda Proxy integration**
5. **Lambda Region:** us-east-1
6. **Lambda Function:** `cloudops-ai-agent`
7. Click **Save** → **OK** (to add Lambda permission).

### 7.4 Deploy the API

1. Click **Actions** → **Deploy API**.
2. **Deployment stage:** [New Stage]
3. **Stage name:** `prod`
4. Click **Deploy**.

5. Copy the **Invoke URL** shown at the top:
   ```
   https://XXXXXXXX.execute-api.us-east-1.amazonaws.com/prod
   ```

### 7.5 Test from Console

1. Click the **POST** method under `/investigate`.
2. Click **TEST** (lightning bolt icon).
3. In **Request Body**, paste:
   ```json
   {
     "incident_description": "Lambda payments-processor has 40% error rate",
     "resource_hints": { "lambda": ["payments-processor"] }
   }
   ```
4. Click **Test**. You should see HTTP 200 with the pipeline result.

---

## 8. Set Up CloudWatch Log Groups

Lambda automatically creates a log group `/aws/lambda/cloudops-ai-agent`. Let's configure retention:

### 8.1 Configure Retention

1. Go to **CloudWatch** → **Logs** → **Log groups**.
2. Find `/aws/lambda/cloudops-ai-agent`.
3. Click the log group → **Actions** → **Edit retention setting**.
4. Set retention: **30 days**.
5. Click **Save**.

### 8.2 Create Log Metric Filters

**Filter 1: Count Lambda errors**

1. Click on the log group → **Metric filters** tab → **Create metric filter**.
2. **Filter pattern:** `[timestamp, requestId, level="ERROR", message]`
3. Click **Test pattern** → **Next**.
4. **Filter name:** `CloudOpsAgentErrors`
5. **Metric namespace:** `CloudOpsAIAgent`
6. **Metric name:** `ErrorCount`
7. **Metric value:** `1`
8. Click **Next** → **Create metric filter**.

---

## 9. Create DynamoDB Table (Incident History)

This stores historical incident data for trend analysis.

### 9.1 Open DynamoDB Console

1. Search for **"DynamoDB"** → click **DynamoDB**.
2. Click **Create table**.

### 9.2 Table Configuration

| Field | Value |
|-------|-------|
| **Table name** | `cloudops-incidents` |
| **Partition key** | `incident_id` (String) |
| **Sort key** | `start_time` (String) |

**Table settings:** Customize settings:
- **Table class:** DynamoDB Standard
- **Capacity mode:** On-demand (pay per request)
- **Encryption:** Owned by Amazon DynamoDB

Click **Create table**.

### 9.3 Add a Global Secondary Index (Optional)

For querying by severity:

1. Click on the table → **Indexes** tab → **Create index**.
2. **Partition key:** `severity` (String)
3. **Sort key:** `start_time` (String)
4. **Index name:** `severity-start_time-index`
5. Click **Create index**.

---

## 10. Test End-to-End via Console

### 10.1 Lambda Test Event

1. Go to **Lambda** → `cloudops-ai-agent` → **Test** tab.
2. Click **Create new event**.
3. **Event name:** `TestPaymentIncident`
4. **Template:** (blank)
5. Paste:
```json
{
  "body": "{\"incident_description\": \"CRITICAL: Lambda payments-processor has 40% error rate since 14:00 UTC. Users are experiencing failed transactions.\", \"resource_hints\": {\"lambda\": [\"payments-processor\"]}, \"override_severity\": \"CRITICAL\"}"
}
```
6. Click **Test**.

### 10.2 Reading the Result

Expand the **Execution result** panel. You should see:

```json
{
  "statusCode": 200,
  "body": "{\"status\": \"SUCCESS\", \"incident_id\": \"INC-...\", ...}"
}
```

Key sections to check:
- `pipeline_result.overall_health` — CRITICAL / WARNING / HEALTHY
- `pipeline_result.estimated_ttr_minutes` — estimated time to resolve
- `pipeline_result.final_recommendation` — Bedrock-generated executive summary
- `performance.total_elapsed_seconds` — pipeline execution time

### 10.3 View Logs in CloudWatch

1. After running the test, click **Monitor** tab → **View CloudWatch logs**.
2. Click the most recent log stream.
3. Look for log entries from each agent stage:
   ```
   [INFO] IncidentAgent ready
   [INFO] Stage 1 done in 1.2s — incident INC-...
   [INFO] MetricsAgent ready
   [INFO] Stage 2 done in 0.8s — overall_health=CRITICAL
   ...
   ```

### 10.4 Test via cURL

```bash
# Replace with your actual API Gateway URL
API_URL="https://XXXXXXXX.execute-api.us-east-1.amazonaws.com/prod"

curl -X POST "${API_URL}/investigate" \
  -H "Content-Type: application/json" \
  -d '{
    "incident_description": "Lambda payments-processor error rate 40%",
    "resource_hints": { "lambda": ["payments-processor"] }
  }' | python3 -m json.tool
```

---

## 11. Set Up CloudWatch Alarms for the Agent Itself

### 11.1 Open CloudWatch Alarms

1. Go to **CloudWatch** → **Alarms** → **All alarms** → **Create alarm**.

### Alarm 1: Lambda Error Rate

1. Click **Select metric** → **Lambda** → **By Function Name**.
2. Select `cloudops-ai-agent` → **Errors** → **Select metric**.
3. **Period:** 5 minutes | **Statistic:** Sum
4. **Threshold:** Greater than **2**
5. **Alarm name:** `CloudOpsAgent-HighErrors`
6. **Action:** Create an SNS topic → `cloudops-alerts` → add your email → **Create topic**.
7. Click **Create alarm**.

### Alarm 2: Lambda Duration (near timeout)

1. Create alarm → **Lambda** → **Duration** → `cloudops-ai-agent`.
2. **Statistic:** Average | **Period:** 5 min
3. **Threshold:** Greater than **240000** (240 seconds — 80% of 300s timeout)
4. **Alarm name:** `CloudOpsAgent-HighDuration`
5. Reuse the SNS topic created above.

### Alarm 3: Lambda Throttles

1. Create alarm → **Lambda** → **Throttles** → `cloudops-ai-agent`.
2. **Statistic:** Sum | **Period:** 5 min
3. **Threshold:** Greater than **0**
4. **Alarm name:** `CloudOpsAgent-Throttled`

---

## 12. GitHub Actions OIDC Integration

This allows GitHub Actions to deploy to Lambda **without storing long-lived AWS credentials**.

### 12.1 Create OIDC Identity Provider

1. Go to **IAM** → **Identity providers** → **Add provider**.
2. **Provider type:** OpenID Connect
3. **Provider URL:** `https://token.actions.githubusercontent.com`
4. Click **Get thumbprint**.
5. **Audience:** `sts.amazonaws.com`
6. Click **Add provider**.

### 12.2 Create Deployment IAM Role

1. **IAM** → **Roles** → **Create role**.
2. **Trusted entity type:** Web identity
3. **Identity provider:** `token.actions.githubusercontent.com`
4. **Audience:** `sts.amazonaws.com`
5. **GitHub organization:** your GitHub username
6. **GitHub repository:** `cloudops-ai-agent`
7. Click **Next**.

**Attach policies:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "lambda:PublishVersion",
        "lambda:CreateAlias",
        "lambda:UpdateAlias",
        "lambda:GetFunction",
        "lambda:InvokeFunction"
      ],
      "Resource": "arn:aws:lambda:*:*:function:cloudops-ai-agent*"
    }
  ]
}
```

Create as inline policy → name it `CloudOpsGitHubDeployPolicy`.

8. **Role name:** `cloudops-ai-agent-github-actions`
9. Click **Create role**.
10. Copy the **Role ARN** — paste it into GitHub Secrets as `AWS_ROLE_ARN`.

### 12.3 Add Secrets to GitHub

1. Go to your GitHub repo → **Settings** → **Secrets and variables** → **Actions**.
2. Click **New repository secret** for each:

| Name | Value |
|------|-------|
| `AWS_ROLE_ARN` | `arn:aws:iam::YOUR_ACCOUNT:role/cloudops-ai-agent-github-actions` |
| `AWS_REGION` | `us-east-1` |
| `LAMBDA_FUNCTION_NAME` | `cloudops-ai-agent` |
| `BEDROCK_MODEL_ID` | `amazon.nova-pro-v1:0` |

3. Now every push to `main` will automatically deploy via GitHub Actions.

---

## 13. Post-Setup Validation Checklist

Run through these checks after completing all steps:

### Infrastructure
- [ ] Bedrock model access granted for amazon.nova-pro-v1:0
- [ ] IAM role `cloudops-ai-agent-lambda-role` exists with correct policies
- [ ] Lambda function `cloudops-ai-agent` exists, runtime Python 3.11, handler `app.lambda_handler`
- [ ] Lambda timeout = 300s, memory = 512 MB
- [ ] All 6 environment variables set in Lambda
- [ ] API Gateway `/investigate` POST method deployed to `prod` stage
- [ ] DynamoDB table `cloudops-incidents` created

### Testing
- [ ] Lambda test event returns HTTP 200
- [ ] Response contains `"status": "SUCCESS"`
- [ ] CloudWatch Logs show all 4 agent stages completing
- [ ] cURL to API Gateway URL returns valid JSON

### Observability
- [ ] Log group `/aws/lambda/cloudops-ai-agent` retention = 30 days
- [ ] CloudWatch alarm `CloudOpsAgent-HighErrors` active
- [ ] CloudWatch alarm `CloudOpsAgent-HighDuration` active
- [ ] SNS email subscription confirmed

### CI/CD (if using GitHub Actions)
- [ ] OIDC provider created in IAM
- [ ] GitHub Actions role `cloudops-ai-agent-github-actions` created
- [ ] All GitHub Secrets set
- [ ] CI workflow passes on a test push
- [ ] Deploy workflow deploys successfully to staging

---

## 14. Cost Estimate

Based on moderate production usage (100 incidents/day):

| Service | Usage | Est. Monthly Cost |
|---------|-------|------------------|
| AWS Lambda | 100 invocations × 4s × 512 MB/day | ~$0.50 |
| Amazon Bedrock (Amazon Nov Pro) | 4 calls × 1000 tokens input + 500 output per incident | ~$15–25 |
| CloudWatch Logs | ~50 MB/day ingestion | ~$1.50 |
| CloudWatch Metrics | 10 GetMetricData calls per incident | ~$1.00 |
| API Gateway | 100 requests/day | ~$0.10 |
| DynamoDB | On-demand, ~1 KB per incident | ~$0.25 |
| **Total** | | **~$20–30/month** |


---

## 15. Cleanup / Teardown

To remove all resources when no longer needed:

### Via Console (in this order — dependencies first)

1. **API Gateway** → select `cloudops-ai-agent` → **Actions** → **Delete API** → confirm.
2. **Lambda** → select `cloudops-ai-agent` → **Actions** → **Delete** → confirm.
3. **DynamoDB** → select `cloudops-incidents` → **Delete** → type "confirm" → **Delete**.
4. **CloudWatch** → **Log groups** → select `/aws/lambda/cloudops-ai-agent` → **Actions** → **Delete**.
5. **CloudWatch** → **Alarms** → select all `CloudOpsAgent-*` alarms → **Delete**.
6. **SNS** → select `cloudops-alerts` topic → **Delete**.
7. **IAM** → **Roles** → delete `cloudops-ai-agent-lambda-role` and `cloudops-ai-agent-github-actions`.
8. **IAM** → **Identity providers** → delete `token.actions.githubusercontent.com`.
9. **S3** → empty and delete `cloudops-ai-agent-deployments-*` bucket (if created).

> ⚠️ Deleting IAM roles is irreversible and cannot be undone.
