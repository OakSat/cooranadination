"""Post-deploy setup: the link-signing key (SecureString, which CloudFormation cannot create)
and the demo customer profile. Safe to run repeatedly.

    uv run scripts/seed.py
"""

import argparse
import secrets
import sys

import _common as c


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    if not c.aws_available():
        c.bad("no AWS credentials: run `aws sso login --profile freight` and export AWS_PROFILE")
        return 2
    import boto3

    out = c.require_outputs("CustomerEmail")
    ssm = boto3.client("ssm", region_name=c.REGION)
    name = f"{c.PARAM_PREFIX}/link-hmac-key"
    try:
        ssm.get_parameter(Name=name)
        c.ok(f"{name} already exists (left unchanged)")
    except ssm.exceptions.ParameterNotFound:
        ssm.put_parameter(
            Name=name, Value=secrets.token_hex(32), Type="SecureString", Tier="Standard"
        )
        c.ok(f"created {name} (SecureString, AWS-managed key)")

    c.use_deployed_stack()
    from freight.persistence.repo import Repo

    Repo("freight").put_customer(
        "biergarten-berlin",
        "Biergarten Berlin GmbH",
        [out["CustomerEmail"]],
        country="DE",
        preferred_language="de",
    )
    c.ok(f"customer profile Biergarten Berlin GmbH <{out['CustomerEmail']}>")
    pool = c.outputs().get("CognitoUserPoolId", "<pool id>")
    rep = c.outputs().get("RepEmail", "<rep email>")
    c.info(
        "create the rep login (once): aws cognito-idp admin-create-user "
        f"--user-pool-id {pool} --username {rep} "
        f"--user-attributes Name=email,Value={rep} Name=email_verified,Value=true"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
