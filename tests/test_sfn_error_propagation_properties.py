"""Property-based tests for SFN transition optimization — error propagation preservation.

# Feature: sfn-transition-optimization, Property 7: Error propagation preservation

For any consolidated handler and for any sub-step position within that handler,
if the sub-step raises an exception, the consolidated handler SHALL re-raise an
exception of the same type with a message containing the original error message,
and no subsequent sub-steps SHALL be executed.

**Validates: Requirements 1.2, 2.2, 3.3, 4.3, 5.3, 6.3, 12.1, 12.2, 12.3**
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — load lambda modules directly.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
_CLUSTER_OPS_DIR = os.path.join(_LAMBDA_DIR, "cluster_operations")
_PROJECT_MGMT_DIR = os.path.join(_LAMBDA_DIR, "project_management")
_SHARED_DIR = os.path.join(_LAMBDA_DIR, "shared")

# ---------------------------------------------------------------------------
# Step 1: Import cluster_operations modules (errors → cluster_creation,
#         cluster_destruction).
# ---------------------------------------------------------------------------
for _mod in [
    "errors", "lifecycle", "cluster_names",
    "cluster_creation", "cluster_destruction",
    "pcs_sizing", "pcs_versions", "posix_provisioning", "tagging",
]:
    sys.modules.pop(_mod, None)

sys.path.insert(0, _CLUSTER_OPS_DIR)
sys.path.insert(0, _SHARED_DIR)
import errors as _cluster_errors_mod  # noqa: E402

# Stash the cluster_operations errors module under a stable alias so
# re-importing "errors" for project_management doesn't clobber it.
cluster_errors = _cluster_errors_mod

import cluster_creation  # noqa: E402
import cluster_destruction  # noqa: E402

# ---------------------------------------------------------------------------
# Step 2: Import project_management modules (errors → lifecycle →
#         project_deploy, project_update, project_destroy).
# ---------------------------------------------------------------------------
for _mod in ["errors", "lifecycle",
             "project_deploy", "project_update", "project_destroy"]:
    sys.modules.pop(_mod, None)

sys.path.insert(0, _PROJECT_MGMT_DIR)
import errors as _project_errors_mod  # noqa: E402

project_errors = _project_errors_mod

import project_deploy  # noqa: E402
import project_update  # noqa: E402
import project_destroy  # noqa: E402


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

project_id_strategy = st.from_regex(r"proj-[a-z0-9]{3,8}", fullmatch=True)
cluster_name_strategy = st.from_regex(
    r"[a-zA-Z][a-zA-Z0-9_-]{2,12}", fullmatch=True,
)
error_message_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=50,
)


# ---------------------------------------------------------------------------
# Helper: build a call-tracking side effect for sub-steps
# ---------------------------------------------------------------------------

def _make_tracking_side_effects(step_names, fail_index, error_cls, error_msg):
    """Create side-effect functions that track calls and inject a failure.

    Returns a list of side-effect callables, one per step. The step at
    ``fail_index`` raises ``error_cls(error_msg)``. Steps after
    ``fail_index`` record themselves in ``calls_after_failure`` if
    invoked (which should NOT happen).

    Returns (side_effects, calls_after_failure) where
    calls_after_failure is a list that should remain empty.
    """
    calls_after_failure = []

    def make_success(idx, name):
        def success_fn(event):
            return {**event, f"_step_{name}_done": True}
        return success_fn

    def make_failure(idx, name):
        def failure_fn(event):
            raise error_cls(error_msg)
        return failure_fn

    def make_post_failure(idx, name):
        def post_failure_fn(event):
            calls_after_failure.append(name)
            return {**event, f"_step_{name}_done": True}
        return post_failure_fn

    side_effects = []
    for i, name in enumerate(step_names):
        if i < fail_index:
            side_effects.append(make_success(i, name))
        elif i == fail_index:
            side_effects.append(make_failure(i, name))
        else:
            side_effects.append(make_post_failure(i, name))

    return side_effects, calls_after_failure


# ---------------------------------------------------------------------------
# Strategies for error types
# ---------------------------------------------------------------------------

cluster_error_type_strategy = st.sampled_from([
    cluster_errors.ValidationError,
    cluster_errors.InternalError,
    cluster_errors.NotFoundError,
    cluster_errors.ConflictError,
    cluster_errors.BudgetExceededError,
])

project_error_type_strategy = st.sampled_from([
    project_errors.ValidationError,
    project_errors.InternalError,
    project_errors.NotFoundError,
    project_errors.ConflictError,
])


# ===================================================================
# [PBT: Property 7] Error propagation preservation
# ===================================================================


# ===================================================================
# Cluster Creation — consolidated_pre_parallel (4 sub-steps)
# ===================================================================

class TestClusterCreationPreParallelErrorPropagation:
    """[PBT: Property 7 — cluster_creation.consolidated_pre_parallel]

    For each sub-step position (0..3) in consolidated_pre_parallel,
    injecting a failure causes the consolidated handler to re-raise
    the same exception type with the original error message, and no
    subsequent sub-steps are executed.

    # Feature: sfn-transition-optimization, Property 7: Error propagation preservation

    **Validates: Requirements 1.2, 12.1**
    """

    _STEP_NAMES = [
        "validate_and_register_name",
        "check_budget_breach",
        "resolve_template",
        "create_iam_resources",
    ]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
        fail_index=st.integers(min_value=0, max_value=3),
        error_cls=cluster_error_type_strategy,
        error_msg=error_message_strategy,
    )
    def test_error_propagation(
        self, project_id, cluster_name, fail_index, error_cls, error_msg,
    ):
        """Injecting a failure at sub-step position ``fail_index``
        causes consolidated_pre_parallel to re-raise the same exception
        type with the original message, and no subsequent sub-steps run.

        **Validates: Requirements 1.2, 12.1**
        """
        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "templateId": "tmpl-test",
        }

        side_effects, calls_after = _make_tracking_side_effects(
            self._STEP_NAMES, fail_index, error_cls, error_msg,
        )

        patches = []
        for i, name in enumerate(self._STEP_NAMES):
            p = patch.object(
                cluster_creation, name, side_effect=side_effects[i],
            )
            patches.append(p)

        for p in patches:
            p.start()

        try:
            with pytest.raises(type(error_cls(error_msg))) as exc_info:
                cluster_creation.consolidated_pre_parallel(event)

            # 1. Same exception type
            assert type(exc_info.value) is error_cls

            # 2. Original error message preserved
            assert error_msg in str(exc_info.value)

            # 3. No subsequent sub-steps executed
            assert calls_after == [], (
                f"Sub-steps after failure index {fail_index} were called: "
                f"{calls_after}"
            )
        finally:
            for p in patches:
                p.stop()


# ===================================================================
# Cluster Creation — consolidated_post_parallel (4 sub-steps)
# ===================================================================

class TestClusterCreationPostParallelErrorPropagation:
    """[PBT: Property 7 — cluster_creation.consolidated_post_parallel]

    For each sub-step position (0..3) in consolidated_post_parallel,
    injecting a failure causes the consolidated handler to re-raise
    the same exception type with the original error message, and no
    subsequent sub-steps are executed.

    # Feature: sfn-transition-optimization, Property 7: Error propagation preservation

    **Validates: Requirements 2.2, 12.1**
    """

    _STEP_NAMES = [
        "resolve_login_node_details",
        "create_pcs_queue",
        "tag_resources",
        "record_cluster",
    ]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
        fail_index=st.integers(min_value=0, max_value=3),
        error_cls=cluster_error_type_strategy,
        error_msg=error_message_strategy,
    )
    def test_error_propagation(
        self, project_id, cluster_name, fail_index, error_cls, error_msg,
    ):
        """Injecting a failure at sub-step position ``fail_index``
        causes consolidated_post_parallel to re-raise the same exception
        type with the original message, and no subsequent sub-steps run.

        **Validates: Requirements 2.2, 12.1**
        """
        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "pcsClusterId": "pcs-test123",
            "loginNodeGroupId": "ng-login1",
            "computeNodeGroupId": "ng-compute1",
        }

        side_effects, calls_after = _make_tracking_side_effects(
            self._STEP_NAMES, fail_index, error_cls, error_msg,
        )

        patches = []
        for i, name in enumerate(self._STEP_NAMES):
            p = patch.object(
                cluster_creation, name, side_effect=side_effects[i],
            )
            patches.append(p)

        for p in patches:
            p.start()

        try:
            with pytest.raises(type(error_cls(error_msg))) as exc_info:
                cluster_creation.consolidated_post_parallel(event)

            assert type(exc_info.value) is error_cls
            assert error_msg in str(exc_info.value)
            assert calls_after == [], (
                f"Sub-steps after failure index {fail_index} were called: "
                f"{calls_after}"
            )
        finally:
            for p in patches:
                p.stop()


# ===================================================================
# Cluster Destruction — consolidated_delete_resources (2-3 sub-steps)
# ===================================================================

class TestClusterDestructionDeleteResourcesErrorPropagation:
    """[PBT: Property 7 — cluster_destruction.consolidated_delete_resources]

    For each sub-step position in consolidated_delete_resources,
    injecting a failure causes the consolidated handler to re-raise
    the same exception type with the original error message, and no
    subsequent sub-steps are executed.

    The handler has 2 steps for lustre mode and 3 for mountpoint mode.

    # Feature: sfn-transition-optimization, Property 7: Error propagation preservation

    **Validates: Requirements 3.3, 12.2**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
        storage_mode=st.sampled_from(["lustre", "mountpoint"]),
        data=st.data(),
        error_cls=cluster_error_type_strategy,
        error_msg=error_message_strategy,
    )
    def test_error_propagation(
        self, project_id, cluster_name, storage_mode, data,
        error_cls, error_msg,
    ):
        """Injecting a failure at any sub-step position causes
        consolidated_delete_resources to re-raise the same exception
        type with the original message, and no subsequent sub-steps run.

        **Validates: Requirements 3.3, 12.2**
        """
        step_names = [
            "delete_pcs_cluster_step",
            "delete_fsx_filesystem",
        ]
        if storage_mode == "mountpoint":
            step_names.append("remove_mountpoint_s3_policy")

        max_idx = len(step_names) - 1
        fail_index = data.draw(
            st.integers(min_value=0, max_value=max_idx),
            label="fail_index",
        )

        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
            "pcsClusterId": "pcs-test123",
            "fsxFilesystemId": "fs-abc12345",
            "storageMode": storage_mode,
        }

        side_effects, calls_after = _make_tracking_side_effects(
            step_names, fail_index, error_cls, error_msg,
        )

        patches = []
        for i, name in enumerate(step_names):
            p = patch.object(
                cluster_destruction, name, side_effect=side_effects[i],
            )
            patches.append(p)

        for p in patches:
            p.start()

        try:
            with pytest.raises(type(error_cls(error_msg))) as exc_info:
                cluster_destruction.consolidated_delete_resources(event)

            assert type(exc_info.value) is error_cls
            assert error_msg in str(exc_info.value)
            assert calls_after == [], (
                f"Sub-steps after failure index {fail_index} were called: "
                f"{calls_after}"
            )
        finally:
            for p in patches:
                p.stop()


# ===================================================================
# Cluster Destruction — consolidated_cleanup (4 sub-steps)
# ===================================================================

class TestClusterDestructionCleanupErrorPropagation:
    """[PBT: Property 7 — cluster_destruction.consolidated_cleanup]

    For each sub-step position (0..3) in consolidated_cleanup,
    injecting a failure causes the consolidated handler to re-raise
    the same exception type with the original error message, and no
    subsequent sub-steps are executed.

    # Feature: sfn-transition-optimization, Property 7: Error propagation preservation

    **Validates: Requirements 3.3, 12.2**
    """

    _STEP_NAMES = [
        "delete_iam_resources",
        "delete_launch_templates",
        "deregister_cluster_name_step",
        "record_cluster_destroyed",
    ]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        cluster_name=cluster_name_strategy,
        fail_index=st.integers(min_value=0, max_value=3),
        error_cls=cluster_error_type_strategy,
        error_msg=error_message_strategy,
    )
    def test_error_propagation(
        self, project_id, cluster_name, fail_index, error_cls, error_msg,
    ):
        """Injecting a failure at sub-step position ``fail_index``
        causes consolidated_cleanup to re-raise the same exception
        type with the original message, and no subsequent sub-steps run.

        **Validates: Requirements 3.3, 12.2**
        """
        event = {
            "projectId": project_id,
            "clusterName": cluster_name,
        }

        side_effects, calls_after = _make_tracking_side_effects(
            self._STEP_NAMES, fail_index, error_cls, error_msg,
        )

        patches = []
        for i, name in enumerate(self._STEP_NAMES):
            p = patch.object(
                cluster_destruction, name, side_effect=side_effects[i],
            )
            patches.append(p)

        for p in patches:
            p.start()

        try:
            with pytest.raises(type(error_cls(error_msg))) as exc_info:
                cluster_destruction.consolidated_cleanup(event)

            assert type(exc_info.value) is error_cls
            assert error_msg in str(exc_info.value)
            assert calls_after == [], (
                f"Sub-steps after failure index {fail_index} were called: "
                f"{calls_after}"
            )
        finally:
            for p in patches:
                p.stop()


# ===================================================================
# Project Deploy — consolidated_pre_loop (2 sub-steps)
# ===================================================================

class TestProjectDeployPreLoopErrorPropagation:
    """[PBT: Property 7 — project_deploy.consolidated_pre_loop]

    For each sub-step position (0..1) in consolidated_pre_loop,
    injecting a failure causes the consolidated handler to re-raise
    the same exception type with the original error message, and no
    subsequent sub-steps are executed.

    # Feature: sfn-transition-optimization, Property 7: Error propagation preservation

    **Validates: Requirements 4.3, 12.3**
    """

    _STEP_NAMES = [
        "validate_project_state",
        "start_cdk_deploy",
    ]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        fail_index=st.integers(min_value=0, max_value=1),
        error_cls=project_error_type_strategy,
        error_msg=error_message_strategy,
    )
    def test_error_propagation(
        self, project_id, fail_index, error_cls, error_msg,
    ):
        """Injecting a failure at sub-step position ``fail_index``
        causes consolidated_pre_loop to re-raise the same exception
        type with the original message, and no subsequent sub-steps run.

        **Validates: Requirements 4.3, 12.3**
        """
        event = {"projectId": project_id}

        side_effects, calls_after = _make_tracking_side_effects(
            self._STEP_NAMES, fail_index, error_cls, error_msg,
        )

        patches = []
        for i, name in enumerate(self._STEP_NAMES):
            p = patch.object(
                project_deploy, name, side_effect=side_effects[i],
            )
            patches.append(p)

        for p in patches:
            p.start()

        try:
            with pytest.raises(type(error_cls(error_msg))) as exc_info:
                project_deploy.consolidated_pre_loop(event)

            assert type(exc_info.value) is error_cls
            assert error_msg in str(exc_info.value)
            assert calls_after == [], (
                f"Sub-steps after failure index {fail_index} were called: "
                f"{calls_after}"
            )
        finally:
            for p in patches:
                p.stop()


# ===================================================================
# Project Deploy — consolidated_post_loop (2 sub-steps)
# ===================================================================

class TestProjectDeployPostLoopErrorPropagation:
    """[PBT: Property 7 — project_deploy.consolidated_post_loop]

    For each sub-step position (0..1) in consolidated_post_loop,
    injecting a failure causes the consolidated handler to re-raise
    the same exception type with the original error message, and no
    subsequent sub-steps are executed.

    # Feature: sfn-transition-optimization, Property 7: Error propagation preservation

    **Validates: Requirements 4.3, 12.3**
    """

    _STEP_NAMES = [
        "extract_stack_outputs",
        "record_infrastructure",
    ]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        fail_index=st.integers(min_value=0, max_value=1),
        error_cls=project_error_type_strategy,
        error_msg=error_message_strategy,
    )
    def test_error_propagation(
        self, project_id, fail_index, error_cls, error_msg,
    ):
        """Injecting a failure at sub-step position ``fail_index``
        causes consolidated_post_loop to re-raise the same exception
        type with the original message, and no subsequent sub-steps run.

        **Validates: Requirements 4.3, 12.3**
        """
        event = {"projectId": project_id, "deployComplete": True}

        side_effects, calls_after = _make_tracking_side_effects(
            self._STEP_NAMES, fail_index, error_cls, error_msg,
        )

        patches = []
        for i, name in enumerate(self._STEP_NAMES):
            p = patch.object(
                project_deploy, name, side_effect=side_effects[i],
            )
            patches.append(p)

        for p in patches:
            p.start()

        try:
            with pytest.raises(type(error_cls(error_msg))) as exc_info:
                project_deploy.consolidated_post_loop(event)

            assert type(exc_info.value) is error_cls
            assert error_msg in str(exc_info.value)
            assert calls_after == [], (
                f"Sub-steps after failure index {fail_index} were called: "
                f"{calls_after}"
            )
        finally:
            for p in patches:
                p.stop()


# ===================================================================
# Project Update — consolidated_pre_loop (2 sub-steps)
# ===================================================================

class TestProjectUpdatePreLoopErrorPropagation:
    """[PBT: Property 7 — project_update.consolidated_pre_loop]

    For each sub-step position (0..1) in consolidated_pre_loop,
    injecting a failure causes the consolidated handler to re-raise
    the same exception type with the original error message, and no
    subsequent sub-steps are executed.

    # Feature: sfn-transition-optimization, Property 7: Error propagation preservation

    **Validates: Requirements 5.3, 12.3**
    """

    _STEP_NAMES = [
        "validate_update_state",
        "start_cdk_update",
    ]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        fail_index=st.integers(min_value=0, max_value=1),
        error_cls=project_error_type_strategy,
        error_msg=error_message_strategy,
    )
    def test_error_propagation(
        self, project_id, fail_index, error_cls, error_msg,
    ):
        """Injecting a failure at sub-step position ``fail_index``
        causes consolidated_pre_loop to re-raise the same exception
        type with the original message, and no subsequent sub-steps run.

        **Validates: Requirements 5.3, 12.3**
        """
        event = {"projectId": project_id}

        side_effects, calls_after = _make_tracking_side_effects(
            self._STEP_NAMES, fail_index, error_cls, error_msg,
        )

        patches = []
        for i, name in enumerate(self._STEP_NAMES):
            p = patch.object(
                project_update, name, side_effect=side_effects[i],
            )
            patches.append(p)

        for p in patches:
            p.start()

        try:
            with pytest.raises(type(error_cls(error_msg))) as exc_info:
                project_update.consolidated_pre_loop(event)

            assert type(exc_info.value) is error_cls
            assert error_msg in str(exc_info.value)
            assert calls_after == [], (
                f"Sub-steps after failure index {fail_index} were called: "
                f"{calls_after}"
            )
        finally:
            for p in patches:
                p.stop()


# ===================================================================
# Project Update — consolidated_post_loop (2 sub-steps)
# ===================================================================

class TestProjectUpdatePostLoopErrorPropagation:
    """[PBT: Property 7 — project_update.consolidated_post_loop]

    For each sub-step position (0..1) in consolidated_post_loop,
    injecting a failure causes the consolidated handler to re-raise
    the same exception type with the original error message, and no
    subsequent sub-steps are executed.

    # Feature: sfn-transition-optimization, Property 7: Error propagation preservation

    **Validates: Requirements 5.3, 12.3**
    """

    _STEP_NAMES = [
        "extract_stack_outputs",
        "record_updated_infrastructure",
    ]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        fail_index=st.integers(min_value=0, max_value=1),
        error_cls=project_error_type_strategy,
        error_msg=error_message_strategy,
    )
    def test_error_propagation(
        self, project_id, fail_index, error_cls, error_msg,
    ):
        """Injecting a failure at sub-step position ``fail_index``
        causes consolidated_post_loop to re-raise the same exception
        type with the original message, and no subsequent sub-steps run.

        **Validates: Requirements 5.3, 12.3**
        """
        event = {"projectId": project_id, "updateComplete": True}

        side_effects, calls_after = _make_tracking_side_effects(
            self._STEP_NAMES, fail_index, error_cls, error_msg,
        )

        patches = []
        for i, name in enumerate(self._STEP_NAMES):
            p = patch.object(
                project_update, name, side_effect=side_effects[i],
            )
            patches.append(p)

        for p in patches:
            p.start()

        try:
            with pytest.raises(type(error_cls(error_msg))) as exc_info:
                project_update.consolidated_post_loop(event)

            assert type(exc_info.value) is error_cls
            assert error_msg in str(exc_info.value)
            assert calls_after == [], (
                f"Sub-steps after failure index {fail_index} were called: "
                f"{calls_after}"
            )
        finally:
            for p in patches:
                p.stop()


# ===================================================================
# Project Destroy — consolidated_pre_loop (2 sub-steps)
# ===================================================================

class TestProjectDestroyPreLoopErrorPropagation:
    """[PBT: Property 7 — project_destroy.consolidated_pre_loop]

    For each sub-step position (0..1) in consolidated_pre_loop,
    injecting a failure causes the consolidated handler to re-raise
    the same exception type with the original error message, and no
    subsequent sub-steps are executed.

    # Feature: sfn-transition-optimization, Property 7: Error propagation preservation

    **Validates: Requirements 6.3, 12.3**
    """

    _STEP_NAMES = [
        "validate_and_check_clusters",
        "start_cdk_destroy",
    ]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        fail_index=st.integers(min_value=0, max_value=1),
        error_cls=project_error_type_strategy,
        error_msg=error_message_strategy,
    )
    def test_error_propagation(
        self, project_id, fail_index, error_cls, error_msg,
    ):
        """Injecting a failure at sub-step position ``fail_index``
        causes consolidated_pre_loop to re-raise the same exception
        type with the original message, and no subsequent sub-steps run.

        **Validates: Requirements 6.3, 12.3**
        """
        event = {"projectId": project_id}

        side_effects, calls_after = _make_tracking_side_effects(
            self._STEP_NAMES, fail_index, error_cls, error_msg,
        )

        patches = []
        for i, name in enumerate(self._STEP_NAMES):
            p = patch.object(
                project_destroy, name, side_effect=side_effects[i],
            )
            patches.append(p)

        for p in patches:
            p.start()

        try:
            with pytest.raises(type(error_cls(error_msg))) as exc_info:
                project_destroy.consolidated_pre_loop(event)

            assert type(exc_info.value) is error_cls
            assert error_msg in str(exc_info.value)
            assert calls_after == [], (
                f"Sub-steps after failure index {fail_index} were called: "
                f"{calls_after}"
            )
        finally:
            for p in patches:
                p.stop()


# ===================================================================
# Project Destroy — consolidated_post_loop (2 sub-steps)
# ===================================================================

class TestProjectDestroyPostLoopErrorPropagation:
    """[PBT: Property 7 — project_destroy.consolidated_post_loop]

    For each sub-step position (0..1) in consolidated_post_loop,
    injecting a failure causes the consolidated handler to re-raise
    the same exception type with the original error message, and no
    subsequent sub-steps are executed.

    # Feature: sfn-transition-optimization, Property 7: Error propagation preservation

    **Validates: Requirements 6.3, 12.3**
    """

    _STEP_NAMES = [
        "clear_infrastructure",
        "archive_project",
    ]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        project_id=project_id_strategy,
        fail_index=st.integers(min_value=0, max_value=1),
        error_cls=project_error_type_strategy,
        error_msg=error_message_strategy,
    )
    def test_error_propagation(
        self, project_id, fail_index, error_cls, error_msg,
    ):
        """Injecting a failure at sub-step position ``fail_index``
        causes consolidated_post_loop to re-raise the same exception
        type with the original message, and no subsequent sub-steps run.

        **Validates: Requirements 6.3, 12.3**
        """
        event = {"projectId": project_id, "destroyComplete": True}

        side_effects, calls_after = _make_tracking_side_effects(
            self._STEP_NAMES, fail_index, error_cls, error_msg,
        )

        patches = []
        for i, name in enumerate(self._STEP_NAMES):
            p = patch.object(
                project_destroy, name, side_effect=side_effects[i],
            )
            patches.append(p)

        for p in patches:
            p.start()

        try:
            with pytest.raises(type(error_cls(error_msg))) as exc_info:
                project_destroy.consolidated_post_loop(event)

            assert type(exc_info.value) is error_cls
            assert error_msg in str(exc_info.value)
            assert calls_after == [], (
                f"Sub-steps after failure index {fail_index} were called: "
                f"{calls_after}"
            )
        finally:
            for p in patches:
                p.stop()
