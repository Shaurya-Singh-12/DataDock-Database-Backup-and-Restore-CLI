"""Create the demo S3 bucket. Used only by the Docker demo.

Real S3 buckets are created in the AWS console or with the AWS CLI; the demo's
local S3 server (moto) starts empty, so this makes the bucket it needs.
It retries for a while because the server may still be starting.
"""

import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

bucket = os.environ["YOURTOOL_STORAGE_BUCKET"]
client = boto3.client(
    "s3",
    endpoint_url=os.environ["YOURTOOL_STORAGE_ENDPOINT_URL"],
    region_name=os.environ.get("YOURTOOL_STORAGE_REGION", "us-east-1"),
)

for attempt in range(30):
    try:
        client.create_bucket(Bucket=bucket)
        print(f"Bucket '{bucket}' is ready.")
        break
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            print(f"Bucket '{bucket}' is ready (it already existed).")
            break
        raise
    except Exception:  # the server is probably still starting
        time.sleep(1)
else:
    sys.exit("Could not reach the S3 service after 30 seconds.")
