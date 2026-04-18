# GitLab Webhook Setup

## 1. Set the webhook secret

Add to your `.env`:

```
SPARK_WEBHOOK_SECRET=your-secret-here
```

## 2. Start the Spark server with HTTP transport

```bash
spark serve --transport sse --port 8343
```

## 3. Configure in GitLab

For each project (or at the group level):

- **URL**: `https://your-spark-host.example.com/webhook/gitlab`
  (or `http://localhost:8343/webhook/gitlab` for local dev)
- **Secret token**: same value as `SPARK_WEBHOOK_SECRET`
- **Trigger**: **Merge request events** only
- **SSL verification**: enable (for production)

## 4. Test

```bash
spark webhook-test svc-claims --url http://localhost:8343
```

## 5. Monitor

Check queue status:

```bash
curl http://localhost:8343/webhook/status
```
