#!/bin/sh
# Initialize only the private local-development artifact bucket and worker scopes.
set -eu

mc alias set local "$MINIO_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"

until mc ready local >/dev/null 2>&1; do
    sleep 1
done

mc mb --ignore-existing "local/$S3_BUCKET"
mc anonymous set none "local/$S3_BUCKET"

configure_worker() {
    worker_id=$1
    access_key=$2
    secret_key=$3
    policy_name="crawltrove-$worker_id"
    policy_file="/tmp/$policy_name.json"

    if ! mc admin user info local "$access_key" >/dev/null 2>&1; then
        mc admin user add local "$access_key" "$secret_key"
    fi

    cat > "$policy_file" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::$S3_BUCKET"],
      "Condition": {"StringLike": {"s3:prefix": ["workers/$worker_id/*"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::$S3_BUCKET/workers/$worker_id/*"]
    }
  ]
}
EOF
    if ! mc admin policy info local "$policy_name" >/dev/null 2>&1; then
        mc admin policy create local "$policy_name" "$policy_file"
    fi
    mc admin policy attach local "$policy_name" --user "$access_key"
}

configure_worker "$STANDARD_WORKER_ID" "$STANDARD_S3_ACCESS_KEY" "$STANDARD_S3_SECRET_KEY"
configure_worker "$BROWSER_WORKER_ID" "$BROWSER_S3_ACCESS_KEY" "$BROWSER_S3_SECRET_KEY"
configure_worker "$CAPTCHA_WORKER_ID" "$CAPTCHA_S3_ACCESS_KEY" "$CAPTCHA_S3_SECRET_KEY"

# Import replaces the lifecycle document, keeping this init operation idempotent
# and limiting expiration to temporary worker uploads.
cat > /tmp/lifecycle.json <<EOF
{
  "Rules": [
    {
      "ID": "expire-$STANDARD_WORKER_ID-tmp",
      "Status": "Enabled",
      "Filter": {"Prefix": "workers/$STANDARD_WORKER_ID/tmp/"},
      "Expiration": {"Days": 1}
    },
    {
      "ID": "expire-$BROWSER_WORKER_ID-tmp",
      "Status": "Enabled",
      "Filter": {"Prefix": "workers/$BROWSER_WORKER_ID/tmp/"},
      "Expiration": {"Days": 1}
    },
    {
      "ID": "expire-$CAPTCHA_WORKER_ID-tmp",
      "Status": "Enabled",
      "Filter": {"Prefix": "workers/$CAPTCHA_WORKER_ID/tmp/"},
      "Expiration": {"Days": 1}
    }
  ]
}
EOF
mc ilm import "local/$S3_BUCKET" < /tmp/lifecycle.json
