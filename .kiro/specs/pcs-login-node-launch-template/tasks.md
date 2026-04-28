# Tasks — PCS Login Node Launch Template Bugfix

- [x] 1. Add EC2 launch template IAM permissions to clusterCreationStepLambda
  - [x] 1.1 Add `ec2:CreateLaunchTemplate`, `ec2:DeleteLaunchTemplate`, and `ec2:DescribeLaunchTemplates` to the existing EC2 policy statement on `clusterCreationStepLambda` in `lib/constructs/cluster-operations.ts`
- [x] 2. Add CreateLaunchTemplates step to the cluster creation state machine
  - [x] 2.1 Define a new `tasks.LambdaInvoke` task named `CreateLaunchTemplates` that invokes `clusterCreationStepLambda` with `step: 'create_launch_templates'` and `payload: sfn.JsonPath.entirePayload`, using `resultPath: '$'` in `lib/constructs/cluster-operations.ts`
  - [x] 2.2 Add `createLaunchTemplates.addCatch(failureChain, catchConfig)` alongside the existing catch handler registrations
  - [x] 2.3 Wire the step into the chain: change `AreInstanceProfilesReady` true branch to route to `CreateLaunchTemplates`, then `CreateLaunchTemplates.next(storageModeChoice)` so the flow becomes `... → AreInstanceProfilesReady → CreateLaunchTemplates → StorageModeChoice → ...`
- [x] 3. Write CDK assertion tests for the fix
  - [x] 3.1 Add a test in `test/constructs/cluster-operations.test.ts` that asserts the creation state machine definition contains `CreateLaunchTemplates` and `create_launch_templates`
  - [x] 3.2 Add a test in `test/constructs/cluster-operations.test.ts` that asserts the IAM policy grants `ec2:CreateLaunchTemplate`, `ec2:DeleteLaunchTemplate`, and `ec2:DescribeLaunchTemplates`
- [x] 4. Verify all existing tests still pass (preservation)
  - [x] 4.1 Run `npx jest test/constructs/cluster-operations.test.ts` and confirm all existing tests plus the new tests pass
