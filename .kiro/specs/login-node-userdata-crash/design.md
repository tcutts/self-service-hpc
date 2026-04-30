# Login Node User Data Crash Bugfix Design

## Overview

PCS login nodes crash in an infinite loop because `generate_user_data_script()` in `lambda/cluster_operations/posix_provisioning.py` produces a bash script with `set -euo pipefail`. When any individual command fails (e.g., EFS package install, S3 mount), the entire script aborts. This causes cloud-final to report failure, PCS health checks terminate the node, and the replacement node hits the same failure — creating a continuous crash loop.

The fix removes `set -euo pipefail` and wraps each logical section in error-isolated blocks that log failures but continue execution. The script always exits 0 so cloud-final succeeds regardless of individual section outcomes.

## Glossary

- **Bug_Condition (C)**: Any command within the generated user data script fails (non-zero exit code) while `set -euo pipefail` is active, causing the entire script to abort
- **Property (P)**: Individual section failures are logged and execution continues; the script always exits 0
- **Preservation**: When all commands succeed, the final node state (mounted filesystems, created users, disabled accounts, logging configured) is identical to the current behavior
- **`generate_user_data_script()`**: The function in `lambda/cluster_operations/posix_provisioning.py` (line 352) that assembles the complete bash user data script
- **cloud-final**: The cloud-init stage that executes user data scripts; a non-zero exit causes PCS to mark the node unhealthy
- **PCS**: AWS Parallel Computing Service — manages login node lifecycle and health checks
- **Error-isolated block**: A bash construct that captures a section's exit code without propagating failure to the parent script

## Bug Details

### Bug Condition

The bug manifests when any command in the generated user data script returns a non-zero exit code. Because `set -euo pipefail` is set at the top of the script, any single failure aborts the entire script immediately. The `generate_user_data_script()` function unconditionally includes `set -euo pipefail` on line 2 of every generated script, and the individual section generators (`generate_efs_mount_commands`, `generate_mountpoint_s3_commands`, `generate_fsx_lustre_mount_commands`) include no error handling.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type GeneratedUserDataScript
  OUTPUT: boolean
  
  RETURN input.containsSetEUOPipefail == true
         AND anyCommandInScript(input.commands) returns non-zero exit code
         AND scriptAbortsOnFailure(input) == true
END FUNCTION
```

### Examples

- **EFS mount failure**: `yum install -y amazon-efs-utils` fails because the package repo is unavailable → script aborts → user accounts never created → node terminated → replacement also fails → infinite loop
- **S3 Mountpoint failure**: `mount-s3 bucket /data` fails because IAM role hasn't propagated yet → script aborts → node terminated → infinite loop
- **FSx Lustre failure**: `amazon-linux-extras install -y lustre` fails on AL2023 (command doesn't exist) → script aborts → node terminated → infinite loop
- **Stunnel dependency**: EFS TLS mount requires stunnel; if stunnel install fails, `mount -a -t efs` fails → script aborts → infinite loop
- **All commands succeed**: No bug manifests — script completes normally (this is the preservation case)

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- When all commands succeed, the final node state must be identical: same users created, same filesystems mounted, same accounts disabled, same logging configured
- The order of section execution must remain: SSM Agent → EFS mount → User creation → Generic account disabling → Access logging → CloudWatch agent → Storage mount (S3/FSx)
- The MIME multipart wrapping via `wrap_user_data_mime()` must continue to produce valid PCS-compatible user data
- SSM Agent commands already have `|| true` error handling and must continue to work as-is
- User creation commands already use `2>/dev/null || true` and must continue to work as-is
- The `generate_*_commands()` helper functions' return values (list of strings) must remain unchanged for callers other than `generate_user_data_script()`

**Scope:**
All inputs where every command succeeds should produce byte-for-byte identical node state. The only observable differences should be:
- Absence of `set -euo pipefail` in the script header
- Presence of error-isolation wrappers around each section
- Presence of a summary section at the end of the script
- The script always exits with code 0

## Hypothesized Root Cause

Based on the bug description and code analysis, the root cause is clear and singular:

1. **`set -euo pipefail` in script header**: Line 2 of every generated script (`lines` list in `generate_user_data_script()`) contains `"set -euo pipefail"`. This causes bash to exit immediately on any command failure (`-e`), undefined variable (`-u`), or pipe failure (`-o pipefail`).

2. **No error isolation in section generators**: `generate_efs_mount_commands()`, `generate_mountpoint_s3_commands()`, and `generate_fsx_lustre_mount_commands()` return raw commands with no error handling. Commands like `yum install -y amazon-efs-utils` and `mount -a -t efs` will fail in various environments.

3. **No graceful degradation design**: The script was written assuming all commands would succeed. There is no concept of "optional" vs "required" sections, no error logging per section, and no summary of outcomes.

4. **Exit code propagation to cloud-final**: When the script aborts due to `set -e`, it exits with the failing command's exit code. Cloud-final interprets any non-zero exit as failure, triggering PCS health check failure and node termination.

## Correctness Properties

Property 1: Bug Condition - Script Continues After Section Failure

_For any_ generated user data script where one or more section commands fail (isBugCondition returns true — i.e., a command returns non-zero), the fixed `generate_user_data_script()` SHALL produce a script that continues executing all remaining sections, logs each failure with the section name and error details, outputs a summary of succeeded/failed sections, and exits with code 0.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6**

Property 2: Preservation - Successful Execution Produces Same Node State

_For any_ generated user data script where all commands succeed (isBugCondition returns false — no command fails), the fixed `generate_user_data_script()` SHALL produce a script that executes all sections in the same order, runs the same commands, and results in the same final node state (mounted filesystems, created users, disabled accounts, configured logging) as the original unfixed function.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `lambda/cluster_operations/posix_provisioning.py`

**Function**: `generate_user_data_script()`

**Specific Changes**:

1. **Remove `set -euo pipefail`**: Delete the line `"set -euo pipefail"` from the `lines` list. Replace with a comment explaining why strict mode is intentionally not used.

2. **Add error-tracking infrastructure**: At the top of the generated script, declare an array variable (e.g., `FAILED_SECTIONS=()` and `SUCCEEDED_SECTIONS=()`) to track section outcomes.

3. **Create a section-wrapper pattern**: Define a bash function or use a consistent pattern to wrap each section's commands in a subshell or conditional block that:
   - Captures the section's exit code
   - Logs success or failure with the section name
   - Appends to the appropriate tracking array
   - Never propagates failure to the parent script

4. **Wrap each section in error isolation**: Apply the wrapper to:
   - SSM Agent commands
   - EFS mount commands
   - User creation commands (as a group)
   - Generic account disabling commands
   - Access logging (PAM exec) commands
   - CloudWatch agent commands
   - Storage mount commands (S3 or FSx)

5. **Add summary output**: At the end of the script, output a clear summary showing which sections succeeded and which failed, suitable for CloudWatch logs and EC2 console output.

6. **Force exit 0**: Ensure the script always ends with `exit 0` regardless of any section failures.

7. **Preserve helper function signatures**: The `generate_*_commands()` helper functions remain unchanged — the error isolation is applied in `generate_user_data_script()` when assembling the sections, not in the helpers themselves. This preserves backward compatibility for SSM Run Command usage.

### Implementation Pattern

The generated script will use this pattern for each section:

```bash
# --- Section: EFS Mount ---
(
  set -e
  yum install -y amazon-efs-utils || apt-get install -y amazon-efs-utils
  mkdir -p /home
  echo 'fs-abc123:/ /home efs _netdev,tls 0 0' >> /etc/fstab
  mount -a -t efs
) 2>&1
if [ $? -eq 0 ]; then
  SUCCEEDED_SECTIONS+=("EFS Mount")
  echo "[SUCCESS] EFS Mount"
else
  FAILED_SECTIONS+=("EFS Mount")
  echo "[FAILED] EFS Mount" >&2
fi
```

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm that `set -euo pipefail` causes script abort on section failure.

**Test Plan**: Generate user data scripts with the unfixed `generate_user_data_script()` function and verify that the output contains `set -euo pipefail`. Then simulate execution scenarios where individual commands fail and confirm the script would abort.

**Test Cases**:
1. **Script contains set -euo pipefail**: Verify the generated script includes the problematic line (will pass on unfixed code, confirming the bug condition exists)
2. **No error isolation present**: Verify that EFS mount commands have no error handling wrappers (will pass on unfixed code)
3. **No exit 0 at end**: Verify the script does not force exit 0 (will pass on unfixed code)
4. **No failure summary**: Verify the script has no section tracking or summary output (will pass on unfixed code)

**Expected Counterexamples**:
- Generated scripts contain `set -euo pipefail` on line 2
- No error isolation exists around mount commands
- Script exit code depends on last command's exit code

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds (any command would fail), the fixed function produces a script that continues execution and exits 0.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  script := generate_user_data_script_fixed(input)
  ASSERT "set -euo pipefail" NOT IN script
  ASSERT script ends with "exit 0"
  ASSERT each section is wrapped in error isolation
  ASSERT script contains section tracking variables
  ASSERT script contains summary output
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold (all commands succeed), the fixed function produces a script that executes the same commands in the same order.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  script_original := generate_user_data_script_original(input)
  script_fixed := generate_user_data_script_fixed(input)
  ASSERT extractCommands(script_original) == extractCommands(script_fixed)
  ASSERT sectionOrder(script_original) == sectionOrder(script_fixed)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many combinations of project configurations (with/without EFS, S3, FSx, varying user counts)
- It catches edge cases like empty user lists, missing storage config, or unusual project IDs
- It provides strong guarantees that the core commands are unchanged across all valid inputs

**Test Plan**: Generate scripts with both the original and fixed functions across many input combinations. Extract the actual operational commands (ignoring wrapper/tracking lines) and verify they match.

**Test Cases**:
1. **Command preservation**: Verify that the actual commands (yum install, mount, useradd, etc.) are identical between original and fixed output
2. **Section order preservation**: Verify sections execute in the same order (SSM → EFS → Users → Generic accounts → Logging → CloudWatch → Storage)
3. **MIME wrapping preservation**: Verify `wrap_user_data_mime()` continues to produce valid MIME output with the fixed script
4. **Helper function preservation**: Verify `generate_efs_mount_commands()`, `generate_mountpoint_s3_commands()`, `generate_fsx_lustre_mount_commands()` return identical command lists

### Unit Tests

- Test that generated script does NOT contain `set -euo pipefail`
- Test that generated script contains `exit 0` as the final effective line
- Test that each section (EFS, S3, FSx, users, logging) is wrapped in error isolation
- Test that section tracking arrays are declared at script start
- Test that summary output is present at script end
- Test with no EFS, no storage — minimal script still has error isolation
- Test with all options enabled — all sections wrapped correctly
- Test edge cases: empty user list, missing storage params, special characters in project ID

### Property-Based Tests

- Generate random valid configurations (varying EFS IDs, bucket names, FSx DNS names, user counts 0-50, storage modes) and verify the fixed script always contains error isolation and exit 0
- Generate random configurations and verify the operational commands extracted from the fixed script match those from the original script (preservation)
- Generate random project IDs with special characters and verify the script is syntactically valid bash

### Integration Tests

- Generate a complete user data script with all options and verify it parses as valid bash (shellcheck or bash -n)
- Verify the MIME-wrapped output is valid MIME multipart format
- Test that the script structure matches expected section ordering end-to-end
- Simulate a section failure in a bash subprocess and verify the script continues and exits 0
