/**
 * Configuration for the HPC Self-Service Portal.
 *
 * Replace these placeholder values with actual deployment outputs
 * after running `cdk deploy`.
 */
const CONFIG = {
  // Amazon Cognito
  cognitoUserPoolId: 'REPLACE_WITH_USER_POOL_ID',
  cognitoClientId: 'REPLACE_WITH_CLIENT_ID',
  cognitoRegion: 'us-east-1',

  // API Gateway
  apiBaseUrl: 'REPLACE_WITH_API_GATEWAY_URL', // e.g. https://abc123.execute-api.us-east-1.amazonaws.com/prod

  // Polling
  clusterPollIntervalMs: 5000, // poll every 5 seconds for CREATING clusters
  projectPollIntervalMs: 5000, // poll every 5 seconds for DEPLOYING/DESTROYING projects
};
