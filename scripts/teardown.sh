#!/usr/bin/env bash
# Remove every demo resource (DEMO_SETUP_GUIDE.md, Part G). Asks before deleting.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1

echo "This deletes the Freight stacks, their buckets and tables, and the link-signing key."
read -r -p "Type 'destroy' to continue: " answer
[[ "$answer" == "destroy" ]] || { echo "aborted"; exit 1; }

(cd infra && uv run cdk destroy --all --force)
aws ssm delete-parameter --name /freight/demo/link-hmac-key 2>/dev/null \
  && echo "deleted /freight/demo/link-hmac-key" || echo "/freight/demo/link-hmac-key already gone"

echo
echo "Leftovers check:"
aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query "StackSummaries[?starts_with(StackName, 'Freight')].StackName" --output text
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/freight- \
  --query "logGroups[].logGroupName" --output text
echo "(both lines above should be empty)"
echo "Optional: delete the SES identities and the CDKToolkit stack. Keep the budget (free)."
