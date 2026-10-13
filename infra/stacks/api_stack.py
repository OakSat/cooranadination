"""FreightApi: one HTTP API for the rep dashboard (Cognito JWT), customer choice links (signed
tokens, no login) and the mock freight providers."""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from infra.stacks.common import PARAM_PREFIX, DemoConfig, function, state_machine_arn
from infra.stacks.storage_stack import StorageStack

TASK_RESPONSE = ["states:SendTaskSuccess", "states:SendTaskFailure", "states:SendTaskHeartbeat"]


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        storage: StorageStack,
        code: lambda_.Code,
        config: DemoConfig,
        **kw: object,
    ) -> None:
        super().__init__(scope, cid, **kw)  # type: ignore[arg-type]

        self.api = apigw.HttpApi(self, "Http", api_name="freight-http-api")
        self.api_url = self.api.api_endpoint
        dashboard_url = f"{self.api_url}/dashboard"

        pool = cognito.UserPool(
            self,
            "Reps",
            user_pool_name="freight-reps",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12, require_digits=True, require_symbols=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        domain = pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"freight-{self.account}"),
        )
        client = pool.add_client(
            "Dashboard",
            user_pool_client_name="freight-dashboard",
            generate_secret=False,  # a browser app: authorization code + PKCE
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[dashboard_url],
                logout_urls=[dashboard_url],
            ),
            id_token_validity=Duration.hours(8),
        )

        env = {
            "TABLE_NAME": storage.table.table_name,
            "CACHE_TABLE": storage.cache.table_name,
            "MOCK_TABLE": storage.mock.table_name,
            "RAW_BUCKET": storage.raw_bucket.bucket_name,
            "STATE_MACHINE_ARN": state_machine_arn(self),
            "API_BASE_URL": self.api_url,
            "SENDER_EMAIL": config.sender_email,
        }
        rep_api = function(
            self,
            "rep-api",
            "freight.handlers.rep_api.handler",
            code,
            {
                **env,
                "COGNITO_DOMAIN": domain.base_url(),
                "COGNITO_CLIENT_ID": client.user_pool_client_id,
            },
        )
        choose = function(self, "choose", "freight.handlers.choose.handler", code, env)
        mock = function(
            self,
            "mock-provider",
            "freight.handlers.mock_provider.handler",
            code,
            env,
        )

        params_arn = f"arn:aws:ssm:{self.region}:{self.account}:parameter{PARAM_PREFIX}/*"
        for fn in (rep_api, choose, mock):
            fn.add_to_role_policy(
                iam.PolicyStatement(actions=["ssm:GetParameter"], resources=[params_arn])
            )
        for fn in (rep_api, choose):
            storage.table.grant_read_write_data(fn)
            fn.add_to_role_policy(
                iam.PolicyStatement(actions=TASK_RESPONSE, resources=[state_machine_arn(self)])
            )
        storage.raw_bucket.grant_put(rep_api, "inbound/*")
        storage.mock.grant_read_write_data(mock)
        mock.add_to_role_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[f"arn:aws:events:{self.region}:{self.account}:event-bus/default"],
            )
        )

        jwt = authorizers.HttpUserPoolAuthorizer("RepJwt", pool, user_pool_clients=[client])
        rep_integration = integrations.HttpLambdaIntegration("RepApi", rep_api)
        for path in ("/dashboard", "/dashboard/app.js", "/dashboard/config.js"):
            self.api.add_routes(
                path=path, methods=[apigw.HttpMethod.GET], integration=rep_integration
            )
        self.api.add_routes(
            path="/rep/{proxy+}",
            methods=[apigw.HttpMethod.GET, apigw.HttpMethod.POST],
            integration=rep_integration,
            authorizer=jwt,
        )
        self.api.add_routes(
            path="/choose",
            methods=[apigw.HttpMethod.GET, apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("Choose", choose),
        )
        self.api.add_routes(
            path="/mock/{proxy+}",
            methods=[apigw.HttpMethod.GET, apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("Mock", mock),
        )

        self.functions = {"rep-api": rep_api, "choose": choose, "mock-provider": mock}
        CfnOutput(self, "ApiUrl", value=self.api_url)
        CfnOutput(self, "DashboardUrl", value=dashboard_url)
        CfnOutput(self, "MockProviderUrl", value=f"{self.api_url}/mock")
        CfnOutput(self, "CognitoUserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "CognitoHostedUiUrl", value=domain.base_url())
        CfnOutput(self, "CognitoClientId", value=client.user_pool_client_id)
