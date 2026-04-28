/**
 * Configuration for the HPC Self-Service Portal.
 *
 * These placeholder values are overwritten at deploy time by CDK
 * (see lib/constructs/web-portal.ts). For local development, replace
 * them with actual deployment outputs from `cdk deploy`.
 */
const CONFIG = {
  // Amazon Cognito
  cognitoUserPoolId: 'eu-west-1_ZKt3BRqeD',
  cognitoClientId: '45o67luv69t9p1eis6af0qjfa7',
  cognitoRegion: 'eu-west-1',

  // API Gateway
  apiBaseUrl: 'https://x2lt96v82j.execute-api.eu-west-1.amazonaws.com/prod/',

  // Polling
  clusterPollIntervalMs: 5000, // poll every 5 seconds for CREATING clusters
  projectPollIntervalMs: 5000, // poll every 5 seconds for DEPLOYING/DESTROYING projects

  // Staleness detection
  clusterCreationTimeoutMs: 9000000, // 2.5 hours — if a cluster has been CREATING longer than this, warn the user
};
