# Release Process

1. Run CI and ensure both Dockerfiles build.
2. Complete the architecture and security review gates.
3. Set repository variables `ARCHITECTURE_REVIEW_TAG=vMAJOR.MINOR.PATCH` and `SECURITY_REVIEW_TAG=vMAJOR.MINOR.PATCH` for the exact tag you intend to release.
4. Configure AWS release promotion secrets for the same repository before you tag:
   - `AWS_OIDC_ROLE_ARN`
   - `AWS_ACCESS_KEY_ID`
   - `AWS_SECRET_ACCESS_KEY`
   - optional repo variable `AWS_ECR_REGION` if you do not use `us-east-1`
5. If you want release jobs on self-hosted runners, set `USE_SELF_HOSTED=true` and `SELF_HOSTED_RUNNER_LABEL=<your-runner-label>`.
6. Tag a release with `vMAJOR.MINOR.PATCH` that matches `pyproject.toml`.
7. Let `release.yml` publish and promote:
   - GHCR tags:
     - `vMAJOR.MINOR.PATCH`
     - `vMAJOR.MINOR`
     - `sha-<commit>`
   - AWS ECR tags for Lambda consumers (legacy repo name `cleanarr-job` is preserved):
     - `cleanarr-webhook:vMAJOR.MINOR.PATCH`
     - `cleanarr-webhook:latest`
     - `cleanarr-job:vMAJOR.MINOR.PATCH` *(legacy Lambda consumer repo/image name)*
     - `cleanarr-job:latest` *(legacy Lambda consumer repo/image name)*
8. Verify the workflow artifacts and summary:
   - each matrix job writes GHCR and ECR digests to the job summary
   - the workflow uploads a `release-metadata` artifact containing `release-metadata.json`
9. Only then update downstream Lambda consumers to the exact semver tag or digest. Do not rely on `latest` as the release source of truth.

## Downstream Lambda + SQS deployment contract

The `cleanarr-lambda` image is the shared runtime for Lambda webhook consumers that receive events through SQS.

For SQS-driven Lambda consumers:

- Consume `release-metadata/<entry>.json` from the workflow output and use:
  - `ecr_repository`: must be `cleanarr-job` for Lambda queue consumers
  - `ecr_release_tag_ref`: exact semver image reference for rollout
  - `ecr_latest_ref`: optional mutable `latest` reference for compatibility only
- Configure the Lambda function in SQS mode:
  - `CLEANARR_WEBHOOK_QUEUE_MODE=sqs`
  - `CLEANARR_WEBHOOK_QUEUE_POLLING=false` when using SQS event source mappings
  - `CLEANARR_WEBHOOK_QUEUE_ENQUEUING=true` or `false` based on whether the same binary is also used as the ingress producer
  - `CLEANARR_WEBHOOK_QUEUE_URL=<SQS queue URL>`
- Configure the SQS event source mapping with partial failure reporting:
  - `FunctionResponseTypes: [ReportBatchItemFailures]`
  - `batch_size` in the same range as `CLEANARR_WEBHOOK_QUEUE_MAX_MESSAGES`
- Keep webhook ingress consumers in `direct` mode if you are not using queue staging.

Current image names:

- `ghcr.io/<owner>/cleanarr-cronjob`
- `ghcr.io/<owner>/cleanarr-webhook-app`
- `ghcr.io/<owner>/cleanarr-lambda`

Current AWS ECR repositories for Lambda consumers:

- `<account>.dkr.ecr.<region>.amazonaws.com/cleanarr-webhook`
- `<account>.dkr.ecr.<region>.amazonaws.com/cleanarr-job` *(legacy Lambda consumer repository name)*

Do not consume `latest` in downstream clusters.

## Verification

The release workflow now makes the semver tag authoritative for both registries. For every release:

- GHCR remains the build source of truth
- Lambda-consumer images are promoted from the exact GHCR release tag into ECR during the same release workflow
- `release-metadata.json` records the release tag, GHCR refs, GHCR digests, ECR refs, and ECR digests

Example downstream example:

- `deploy/examples/lambda-sqs-consumer.yaml` for a minimal AWS SQS event-consumer wiring example

That metadata is intended to feed downstream repos that deploy Lambda consumers.

Example verification commands after a successful release:

```bash
aws ecr describe-images \
  --repository-name cleanarr-job \
  --image-ids imageTag=v0.2.12 \
  --query 'imageDetails[0].imageDigest' \
  --output text

aws ecr describe-images \
  --repository-name cleanarr-webhook \
  --image-ids imageTag=v0.2.12 \
  --query 'imageDetails[0].imageDigest' \
  --output text
```
