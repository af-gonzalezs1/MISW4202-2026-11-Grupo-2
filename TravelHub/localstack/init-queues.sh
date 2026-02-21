#!/usr/bin/env bash
set -euo pipefail

echo "[INIT] Creando DLQ..."
awslocal sqs create-queue --queue-name pagos-dlq >/dev/null

DLQ_URL="$(awslocal sqs get-queue-url --queue-name pagos-dlq --query QueueUrl --output text)"
DLQ_ARN="$(awslocal sqs get-queue-attributes --queue-url "$DLQ_URL" --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)"

echo "[INIT] Creando cola principal..."
awslocal sqs create-queue --queue-name pagos-queue >/dev/null
QUEUE_URL="$(awslocal sqs get-queue-url --queue-name pagos-queue --query QueueUrl --output text)"

echo "[INIT] Configurando atributos (VisibilityTimeout + RedrivePolicy)..."
REDRIVE_POLICY="{\"deadLetterTargetArn\":\"$DLQ_ARN\",\"maxReceiveCount\":\"5\"}"

awslocal sqs set-queue-attributes \
  --queue-url "$QUEUE_URL" \
  --attributes VisibilityTimeout=30,RedrivePolicy="$REDRIVE_POLICY"

echo "[INIT] Listo ✅"
awslocal sqs list-queues