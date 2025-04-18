import datetime
import os
import uuid
from enum import Enum
from typing import Annotated, Any

import structlog
import yaml
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import ORJSONResponse

from skyvern import analytics
from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.aws import aws_client
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.artifact.models import Artifact
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.permissions.permission_checker_factory import PermissionCheckerFactory
from skyvern.forge.sdk.core.security import generate_skyvern_signature
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestionBase, AISuggestionRequest
from skyvern.forge.sdk.schemas.organizations import (
    GetOrganizationAPIKeysResponse,
    GetOrganizationsResponse,
    Organization,
    OrganizationUpdate,
)
from skyvern.forge.sdk.schemas.task_generations import GenerateTaskRequest, TaskGeneration
from skyvern.forge.sdk.schemas.task_runs import TaskRunType
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Request
from skyvern.forge.sdk.schemas.tasks import (
    CreateTaskResponse,
    OrderBy,
    SortDirection,
    Task,
    TaskRequest,
    TaskResponse,
    TaskStatus,
)
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunTimeline
from skyvern.forge.sdk.services import org_auth_service, task_run_service
from skyvern.forge.sdk.workflow.exceptions import (
    FailedToCreateWorkflow,
    FailedToUpdateWorkflow,
    InvalidTemplateWorkflowPermanentId,
    WorkflowParameterMissingRequiredValue,
)
from skyvern.forge.sdk.workflow.models.block import BlockType
from skyvern.forge.sdk.workflow.models.workflow import (
    RunWorkflowResponse,
    Workflow,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunResponse,
    WorkflowRunStatus,
    WorkflowStatus,
)
from skyvern.forge.sdk.workflow.models.yaml import WorkflowCreateYAMLRequest
from skyvern.schemas.runs import RunEngine, TaskRunRequest, TaskRunResponse
from skyvern.services import task_v1_service, task_v2_service
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.schemas import BrowserSessionResponse

official_api_router = APIRouter()
base_router = APIRouter()
v2_router = APIRouter()

LOG = structlog.get_logger()


class EntityType(str, Enum):
    STEP = "step"
    TASK = "task"
    WORKFLOW_RUN = "workflow_run"
    WORKFLOW_RUN_BLOCK = "workflow_run_block"
    THOUGHT = "thought"


entity_type_to_param = {
    EntityType.STEP: "step_id",
    EntityType.TASK: "task_id",
    EntityType.WORKFLOW_RUN: "workflow_run_id",
    EntityType.WORKFLOW_RUN_BLOCK: "workflow_run_block_id",
    EntityType.THOUGHT: "thought_id",
}


class AISuggestionType(str, Enum):
    DATA_SCHEMA = "data_schema"


@base_router.post(
    "/webhook",
    tags=["server"],
    openapi_extra={
        "x-fern-sdk-group-name": "server",
        "x-fern-sdk-method-name": "webhook",
    },
)
@base_router.post("/webhook/", include_in_schema=False)
async def webhook(
    request: Request,
    x_skyvern_signature: Annotated[str | None, Header()] = None,
    x_skyvern_timestamp: Annotated[str | None, Header()] = None,
) -> Response:
    analytics.capture("skyvern-oss-agent-webhook-received")
    payload = await request.body()

    if not x_skyvern_signature or not x_skyvern_timestamp:
        LOG.error(
            "Webhook signature or timestamp missing",
            x_skyvern_signature=x_skyvern_signature,
            x_skyvern_timestamp=x_skyvern_timestamp,
            payload=payload,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing webhook signature or timestamp",
        )

    generated_signature = generate_skyvern_signature(
        payload.decode("utf-8"),
        settings.SKYVERN_API_KEY,
    )

    LOG.info(
        "Webhook received",
        x_skyvern_signature=x_skyvern_signature,
        x_skyvern_timestamp=x_skyvern_timestamp,
        payload=payload,
        generated_signature=generated_signature,
        valid_signature=x_skyvern_signature == generated_signature,
    )
    return Response(content="webhook validation", status_code=200)


@base_router.get(
    "/heartbeat",
    tags=["server"],
    openapi_extra={
        "x-fern-sdk-group-name": "server",
        "x-fern-sdk-method-name": "heartbeat",
    },
)
@base_router.get("/heartbeat/", include_in_schema=False)
async def heartbeat() -> Response:
    """
    Check if the server is running.
    """
    return Response(content="Server is running.", status_code=200)


@base_router.post(
    "/tasks",
    tags=["agent"],
    response_model=CreateTaskResponse,
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "run_task_v1",
    },
)
@base_router.post(
    "/tasks/",
    response_model=CreateTaskResponse,
    include_in_schema=False,
)
async def run_task_v1(
    request: Request,
    background_tasks: BackgroundTasks,
    task: TaskRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
    x_max_steps_override: Annotated[int | None, Header()] = None,
) -> CreateTaskResponse:
    analytics.capture("skyvern-oss-agent-task-create", data={"url": task.url})
    await PermissionCheckerFactory.get_instance().check(current_org, browser_session_id=task.browser_session_id)

    created_task = await task_v1_service.run_task(
        task=task,
        organization=current_org,
        x_max_steps_override=x_max_steps_override,
        x_api_key=x_api_key,
        request=request,
        background_tasks=background_tasks,
    )
    return CreateTaskResponse(task_id=created_task.task_id)


@base_router.get(
    "/tasks/{task_id}",
    tags=["agent"],
    response_model=TaskResponse,
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_task_v1",
    },
)
@base_router.get("/tasks/{task_id}/", response_model=TaskResponse, include_in_schema=False)
async def get_task_v1(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TaskResponse:
    analytics.capture("skyvern-oss-agent-task-get")
    task_obj = await app.DATABASE.get_task(task_id, organization_id=current_org.organization_id)
    if not task_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found {task_id}",
        )

    # get latest step
    latest_step = await app.DATABASE.get_latest_step(task_id, organization_id=current_org.organization_id)
    if not latest_step:
        return await app.agent.build_task_response(task=task_obj)

    failure_reason: str | None = None
    if task_obj.status == TaskStatus.failed and (latest_step.output or task_obj.failure_reason):
        failure_reason = ""
        if task_obj.failure_reason:
            failure_reason += task_obj.failure_reason or ""
        if latest_step.output is not None and latest_step.output.actions_and_results is not None:
            action_results_string: list[str] = []
            for action, results in latest_step.output.actions_and_results:
                if len(results) == 0:
                    continue
                if results[-1].success:
                    continue
                action_results_string.append(f"{action.action_type} action failed.")

            if len(action_results_string) > 0:
                failure_reason += "(Exceptions: " + str(action_results_string) + ")"
    return await app.agent.build_task_response(
        task=task_obj, last_step=latest_step, failure_reason=failure_reason, need_browser_log=True
    )


@base_router.post(
    "/tasks/{task_id}/cancel",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "cancel_task",
    },
)
@base_router.post("/tasks/{task_id}/cancel/", include_in_schema=False)
async def cancel_task(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    analytics.capture("skyvern-oss-agent-task-get")
    task_obj = await app.DATABASE.get_task(task_id, organization_id=current_org.organization_id)
    if not task_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found {task_id}",
        )
    task = await app.agent.update_task(task_obj, status=TaskStatus.canceled)
    # get latest step
    latest_step = await app.DATABASE.get_latest_step(task_id, organization_id=current_org.organization_id)
    # retry the webhook
    await app.agent.execute_task_webhook(task=task, last_step=latest_step, api_key=x_api_key)


@base_router.post(
    "/workflows/runs/{workflow_run_id}/cancel",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "cancel_workflow_run",
    },
)
@base_router.post("/workflows/runs/{workflow_run_id}/cancel/", include_in_schema=False)
async def cancel_workflow_run(
    workflow_run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    workflow_run = await app.DATABASE.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=current_org.organization_id,
    )
    if not workflow_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow run not found {workflow_run_id}",
        )
    # get all the child workflow runs and cancel them
    child_workflow_runs = await app.DATABASE.get_workflow_runs_by_parent_workflow_run_id(
        organization_id=current_org.organization_id,
        parent_workflow_run_id=workflow_run_id,
    )
    for child_workflow_run in child_workflow_runs:
        if child_workflow_run.status not in [
            WorkflowRunStatus.running,
            WorkflowRunStatus.created,
            WorkflowRunStatus.queued,
        ]:
            continue
        await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled(child_workflow_run.workflow_run_id)
    await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled(workflow_run_id)
    await app.WORKFLOW_SERVICE.execute_workflow_webhook(workflow_run, api_key=x_api_key)


@base_router.post(
    "/tasks/{task_id}/retry_webhook",
    tags=["agent"],
    response_model=TaskResponse,
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "retry_webhook",
    },
)
@base_router.post(
    "/tasks/{task_id}/retry_webhook/",
    response_model=TaskResponse,
    include_in_schema=False,
)
async def retry_webhook(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> TaskResponse:
    analytics.capture("skyvern-oss-agent-task-retry-webhook")
    task_obj = await app.DATABASE.get_task(task_id, organization_id=current_org.organization_id)
    if not task_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found {task_id}",
        )

    # get latest step
    latest_step = await app.DATABASE.get_latest_step(task_id, organization_id=current_org.organization_id)
    if not latest_step:
        return await app.agent.build_task_response(task=task_obj)

    # retry the webhook
    await app.agent.execute_task_webhook(task=task_obj, last_step=latest_step, api_key=x_api_key)

    return await app.agent.build_task_response(task=task_obj, last_step=latest_step)


@base_router.get(
    "/tasks",
    tags=["agent"],
    response_model=list[Task],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_tasks",
    },
)
@base_router.get(
    "/tasks/",
    response_model=list[Task],
    include_in_schema=False,
)
async def get_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    task_status: Annotated[list[TaskStatus] | None, Query()] = None,
    workflow_run_id: Annotated[str | None, Query()] = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    only_standalone_tasks: bool = Query(False),
    application: Annotated[str | None, Query()] = None,
    sort: OrderBy = Query(OrderBy.created_at),
    order: SortDirection = Query(SortDirection.desc),
) -> Response:
    """
    Get all tasks.
    :param page: Starting page, defaults to 1
    :param page_size: Page size, defaults to 10
    :param task_status: Task status filter
    :param workflow_run_id: Workflow run id filter
    :param only_standalone_tasks: Only standalone tasks, tasks which are part of a workflow run will be filtered out
    :param order: Direction to sort by, ascending or descending
    :param sort: Column to sort by, created_at or modified_at
    :return: List of tasks with pagination without steps populated. Steps can be populated by calling the
        get_agent_task endpoint.
    """
    analytics.capture("skyvern-oss-agent-tasks-get")
    if only_standalone_tasks and workflow_run_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only_standalone_tasks and workflow_run_id cannot be used together",
        )
    tasks = await app.DATABASE.get_tasks(
        page,
        page_size,
        task_status=task_status,
        workflow_run_id=workflow_run_id,
        organization_id=current_org.organization_id,
        only_standalone_tasks=only_standalone_tasks,
        order=order,
        order_by_column=sort,
        application=application,
    )
    return ORJSONResponse([(await app.agent.build_task_response(task=task)).model_dump() for task in tasks])


@base_router.get(
    "/runs",
    tags=["agent"],
    response_model=list[WorkflowRun | Task],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_runs",
    },
)
@base_router.get(
    "/runs/",
    response_model=list[WorkflowRun | Task],
    include_in_schema=False,
)
async def get_runs(
    current_org: Organization = Depends(org_auth_service.get_current_org),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    status: Annotated[list[WorkflowRunStatus] | None, Query()] = None,
) -> Response:
    analytics.capture("skyvern-oss-agent-runs-get")

    # temporary limit to 100 runs
    if page > 10:
        return []

    runs = await app.DATABASE.get_all_runs(current_org.organization_id, page=page, page_size=page_size, status=status)
    return ORJSONResponse([run.model_dump() for run in runs])


@base_router.get(
    "/runs/{run_id}",
    tags=["agent"],
    response_model=TaskRunResponse,
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_run",
    },
)
@base_router.get(
    "/runs/{run_id}/",
    response_model=TaskRunResponse,
    include_in_schema=False,
)
async def get_run(
    run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TaskRunResponse:
    task_run_response = await task_run_service.get_task_run_response(
        run_id, organization_id=current_org.organization_id
    )
    if not task_run_response:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task run not found {run_id}",
        )
    return task_run_response


@base_router.get(
    "/tasks/{task_id}/steps",
    tags=["agent"],
    response_model=list[Step],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_steps",
    },
)
@base_router.get(
    "/tasks/{task_id}/steps/",
    response_model=list[Step],
    include_in_schema=False,
)
async def get_steps(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all steps for a task.
    :param task_id:
    :return: List of steps for a task with pagination.
    """
    analytics.capture("skyvern-oss-agent-task-steps-get")
    steps = await app.DATABASE.get_task_steps(task_id, organization_id=current_org.organization_id)
    return ORJSONResponse([step.model_dump(exclude_none=True) for step in steps])


@base_router.get(
    "/{entity_type}/{entity_id}/artifacts",
    tags=["agent"],
    response_model=list[Artifact],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_artifacts",
    },
)
@base_router.get(
    "/{entity_type}/{entity_id}/artifacts/",
    response_model=list[Artifact],
    include_in_schema=False,
)
async def get_artifacts(
    entity_type: EntityType,
    entity_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all artifacts for an entity (step, task, workflow_run).

    Args:
        entity_type: Type of entity to fetch artifacts for
        entity_id: ID of the entity
        current_org: Current organization from auth

    Returns:
        List of artifacts for the entity

    Raises:
        HTTPException: If entity is not supported
    """

    if entity_type not in entity_type_to_param:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid entity_type: {entity_type}",
        )

    analytics.capture("skyvern-oss-agent-entity-artifacts-get")

    params = {
        "organization_id": current_org.organization_id,
        entity_type_to_param[entity_type]: entity_id,
    }

    artifacts = await app.DATABASE.get_artifacts_by_entity_id(**params)  # type: ignore

    if settings.ENV != "local" or settings.GENERATE_PRESIGNED_URLS:
        signed_urls = await app.ARTIFACT_MANAGER.get_share_links(artifacts)
        if signed_urls:
            for i, artifact in enumerate(artifacts):
                artifact.signed_url = signed_urls[i]
        else:
            LOG.warning(
                "Failed to get signed urls for artifacts",
                entity_type=entity_type,
                entity_id=entity_id,
            )

    return ORJSONResponse([artifact.model_dump() for artifact in artifacts])


@base_router.get(
    "/tasks/{task_id}/steps/{step_id}/artifacts",
    tags=["agent"],
    response_model=list[Artifact],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_step_artifacts",
    },
)
@base_router.get(
    "/tasks/{task_id}/steps/{step_id}/artifacts/",
    response_model=list[Artifact],
    include_in_schema=False,
)
async def get_step_artifacts(
    task_id: str,
    step_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all artifacts for a list of steps.
    :param task_id:
    :param step_id:
    :return: List of artifacts for a list of steps.
    """
    analytics.capture("skyvern-oss-agent-task-step-artifacts-get")
    artifacts = await app.DATABASE.get_artifacts_for_task_step(
        task_id,
        step_id,
        organization_id=current_org.organization_id,
    )
    if settings.ENV != "local" or settings.GENERATE_PRESIGNED_URLS:
        signed_urls = await app.ARTIFACT_MANAGER.get_share_links(artifacts)
        if signed_urls:
            for i, artifact in enumerate(artifacts):
                artifact.signed_url = signed_urls[i]
        else:
            LOG.warning(
                "Failed to get signed urls for artifacts",
                task_id=task_id,
                step_id=step_id,
            )
    return ORJSONResponse([artifact.model_dump() for artifact in artifacts])


@base_router.get(
    "/tasks/{task_id}/actions",
    response_model=list[Action],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_actions",
    },
)
@base_router.get(
    "/tasks/{task_id}/actions/",
    response_model=list[Action],
    include_in_schema=False,
)
async def get_actions(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[Action]:
    analytics.capture("skyvern-oss-agent-task-actions-get")
    actions = await app.DATABASE.get_task_actions(task_id, organization_id=current_org.organization_id)
    return actions


@base_router.post(
    "/workflows/{workflow_id}/run",
    response_model=RunWorkflowResponse,
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "run_workflow",
    },
)
@base_router.post(
    "/workflows/{workflow_id}/run/",
    response_model=RunWorkflowResponse,
    include_in_schema=False,
)
async def run_workflow(
    request: Request,
    background_tasks: BackgroundTasks,
    workflow_id: str,  # this is the workflow_permanent_id internally
    workflow_request: WorkflowRequestBody,
    version: int | None = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    template: bool = Query(False),
    x_api_key: Annotated[str | None, Header()] = None,
    x_max_steps_override: Annotated[int | None, Header()] = None,
) -> RunWorkflowResponse:
    analytics.capture("skyvern-oss-agent-workflow-execute")
    context = skyvern_context.ensure_context()
    request_id = context.request_id
    await PermissionCheckerFactory.get_instance().check(
        current_org,
        browser_session_id=workflow_request.browser_session_id,
    )

    if template:
        if workflow_id not in await app.STORAGE.retrieve_global_workflows():
            raise InvalidTemplateWorkflowPermanentId(workflow_permanent_id=workflow_id)

    workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
        request_id=request_id,
        workflow_request=workflow_request,
        workflow_permanent_id=workflow_id,
        organization_id=current_org.organization_id,
        version=version,
        max_steps_override=x_max_steps_override,
        is_template_workflow=template,
    )
    workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_id,
        organization_id=None if template else current_org.organization_id,
        version=version,
    )
    await app.DATABASE.create_task_run(
        task_run_type=TaskRunType.workflow_run,
        organization_id=current_org.organization_id,
        run_id=workflow_run.workflow_run_id,
        title=workflow.title,
    )
    if x_max_steps_override:
        LOG.info("Overriding max steps per run", max_steps_override=x_max_steps_override)
    await AsyncExecutorFactory.get_executor().execute_workflow(
        request=request,
        background_tasks=background_tasks,
        organization_id=current_org.organization_id,
        workflow_id=workflow_run.workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
        max_steps_override=x_max_steps_override,
        browser_session_id=workflow_request.browser_session_id,
        api_key=x_api_key,
    )
    return RunWorkflowResponse(
        workflow_id=workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
    )


@base_router.get(
    "/workflows/runs",
    response_model=list[WorkflowRun],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_workflow_runs",
    },
)
@base_router.get(
    "/workflows/runs/",
    response_model=list[WorkflowRun],
    include_in_schema=False,
)
async def get_workflow_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    status: Annotated[list[WorkflowRunStatus] | None, Query()] = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[WorkflowRun]:
    analytics.capture("skyvern-oss-agent-workflow-runs-get")
    return await app.WORKFLOW_SERVICE.get_workflow_runs(
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
        status=status,
    )


@base_router.get(
    "/workflows/{workflow_id}/runs",
    response_model=list[WorkflowRun],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_workflow_runs_by_id",
    },
)
@base_router.get(
    "/workflows/{workflow_id}/runs/",
    response_model=list[WorkflowRun],
    include_in_schema=False,
)
async def get_workflow_runs_by_id(
    workflow_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    status: Annotated[list[WorkflowRunStatus] | None, Query()] = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[WorkflowRun]:
    analytics.capture("skyvern-oss-agent-workflow-runs-get")
    return await app.WORKFLOW_SERVICE.get_workflow_runs_for_workflow_permanent_id(
        workflow_permanent_id=workflow_id,
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
        status=status,
    )


@base_router.get(
    "/workflows/{workflow_id}/runs/{workflow_run_id}",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_workflow_run_with_workflow_id",
    },
)
@base_router.get(
    "/workflows/{workflow_id}/runs/{workflow_run_id}/",
    include_in_schema=False,
)
async def get_workflow_run_with_workflow_id(
    workflow_id: str,
    workflow_run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> dict[str, Any]:
    analytics.capture("skyvern-oss-agent-workflow-run-get")
    workflow_run_status_response = await app.WORKFLOW_SERVICE.build_workflow_run_status_response(
        workflow_permanent_id=workflow_id,
        workflow_run_id=workflow_run_id,
        organization_id=current_org.organization_id,
        include_cost=True,
    )
    return_dict = workflow_run_status_response.model_dump()
    task_v2 = await app.DATABASE.get_task_v2_by_workflow_run_id(
        workflow_run_id=workflow_run_id,
        organization_id=current_org.organization_id,
    )
    if task_v2:
        return_dict["task_v2"] = task_v2.model_dump(by_alias=True)
    return return_dict


@base_router.get(
    "/workflows/{workflow_id}/runs/{workflow_run_id}/timeline",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_workflow_run_timeline",
    },
)
@base_router.get(
    "/workflows/{workflow_id}/runs/{workflow_run_id}/timeline/",
    include_in_schema=False,
)
async def get_workflow_run_timeline(
    workflow_run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[WorkflowRunTimeline]:
    return await _flatten_workflow_run_timeline(current_org.organization_id, workflow_run_id)


@base_router.get(
    "/workflows/runs/{workflow_run_id}",
    response_model=WorkflowRunResponse,
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_workflow_run",
    },
)
@base_router.get(
    "/workflows/runs/{workflow_run_id}/",
    response_model=WorkflowRunResponse,
    include_in_schema=False,
)
async def get_workflow_run(
    workflow_run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowRunResponse:
    analytics.capture("skyvern-oss-agent-workflow-run-get")
    return await app.WORKFLOW_SERVICE.build_workflow_run_status_response_by_workflow_id(
        workflow_run_id=workflow_run_id,
        organization_id=current_org.organization_id,
    )


@base_router.post(
    "/workflows",
    openapi_extra={
        "requestBody": {
            "content": {"application/x-yaml": {"schema": WorkflowCreateYAMLRequest.model_json_schema()}},
            "required": True,
        },
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "create_workflow",
    },
    response_model=Workflow,
    tags=["agent"],
)
@base_router.post(
    "/workflows/",
    openapi_extra={
        "requestBody": {
            "content": {"application/x-yaml": {"schema": WorkflowCreateYAMLRequest.model_json_schema()}},
            "required": True,
        },
    },
    response_model=Workflow,
    include_in_schema=False,
)
async def create_workflow(
    request: Request,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Workflow:
    analytics.capture("skyvern-oss-agent-workflow-create")
    raw_yaml = await request.body()
    try:
        workflow_yaml = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        raise HTTPException(status_code=422, detail="Invalid YAML")

    try:
        workflow_create_request = WorkflowCreateYAMLRequest.model_validate(workflow_yaml)
        return await app.WORKFLOW_SERVICE.create_workflow_from_request(
            organization=current_org, request=workflow_create_request
        )
    except WorkflowParameterMissingRequiredValue as e:
        raise e
    except Exception as e:
        LOG.error("Failed to create workflow", exc_info=True, organization_id=current_org.organization_id)
        raise FailedToCreateWorkflow(str(e))


@base_router.put(
    "/workflows/{workflow_permanent_id}",
    openapi_extra={
        "requestBody": {
            "content": {"application/x-yaml": {"schema": WorkflowCreateYAMLRequest.model_json_schema()}},
            "required": True,
        },
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "update_workflow",
    },
    response_model=Workflow,
    tags=["agent"],
)
@base_router.put(
    "/workflows/{workflow_permanent_id}/",
    openapi_extra={
        "requestBody": {
            "content": {"application/x-yaml": {"schema": WorkflowCreateYAMLRequest.model_json_schema()}},
            "required": True,
        },
    },
    response_model=Workflow,
    include_in_schema=False,
)
async def update_workflow(
    workflow_permanent_id: str,
    request: Request,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Workflow:
    analytics.capture("skyvern-oss-agent-workflow-update")
    # validate the workflow
    raw_yaml = await request.body()
    try:
        workflow_yaml = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        raise HTTPException(status_code=422, detail="Invalid YAML")

    try:
        workflow_create_request = WorkflowCreateYAMLRequest.model_validate(workflow_yaml)
        return await app.WORKFLOW_SERVICE.create_workflow_from_request(
            organization=current_org,
            request=workflow_create_request,
            workflow_permanent_id=workflow_permanent_id,
        )
    except WorkflowParameterMissingRequiredValue as e:
        raise e
    except Exception as e:
        LOG.exception(
            "Failed to update workflow",
            workflow_permanent_id=workflow_permanent_id,
            organization_id=current_org.organization_id,
        )
        raise FailedToUpdateWorkflow(workflow_permanent_id, f"<{type(e).__name__}: {str(e)}>")


@base_router.delete(
    "/workflows/{workflow_permanent_id}",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "delete_workflow",
    },
)
@base_router.delete("/workflows/{workflow_permanent_id}/", include_in_schema=False)
async def delete_workflow(
    workflow_permanent_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    analytics.capture("skyvern-oss-agent-workflow-delete")
    await app.WORKFLOW_SERVICE.delete_workflow_by_permanent_id(workflow_permanent_id, current_org.organization_id)


@base_router.get(
    "/workflows",
    response_model=list[Workflow],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_workflows",
    },
)
@base_router.get(
    "/workflows/",
    response_model=list[Workflow],
    include_in_schema=False,
)
async def get_workflows(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    only_saved_tasks: bool = Query(False),
    only_workflows: bool = Query(False),
    title: str = Query(""),
    current_org: Organization = Depends(org_auth_service.get_current_org),
    template: bool = Query(False),
) -> list[Workflow]:
    """
    Get all workflows with the latest version for the organization.
    """
    analytics.capture("skyvern-oss-agent-workflows-get")

    if template:
        global_workflows_permanent_ids = await app.STORAGE.retrieve_global_workflows()
        if not global_workflows_permanent_ids:
            return []
        workflows = await app.WORKFLOW_SERVICE.get_workflows_by_permanent_ids(
            workflow_permanent_ids=global_workflows_permanent_ids,
            page=page,
            page_size=page_size,
            title=title,
            statuses=[WorkflowStatus.published, WorkflowStatus.draft],
        )
        return workflows

    if only_saved_tasks and only_workflows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only_saved_tasks and only_workflows cannot be used together",
        )

    return await app.WORKFLOW_SERVICE.get_workflows_by_organization_id(
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
        only_saved_tasks=only_saved_tasks,
        only_workflows=only_workflows,
        title=title,
        statuses=[WorkflowStatus.published, WorkflowStatus.draft],
    )


@base_router.get(
    "/workflows/templates",
    response_model=list[Workflow],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_workflow_templates",
    },
)
@base_router.get(
    "/workflows/templates/",
    response_model=list[Workflow],
    include_in_schema=False,
)
async def get_workflow_templates() -> list[Workflow]:
    global_workflows_permanent_ids = await app.STORAGE.retrieve_global_workflows()

    if not global_workflows_permanent_ids:
        return []

    workflows = await app.WORKFLOW_SERVICE.get_workflows_by_permanent_ids(
        workflow_permanent_ids=global_workflows_permanent_ids,
        statuses=[WorkflowStatus.published, WorkflowStatus.draft],
    )

    return workflows


@base_router.get(
    "/workflows/{workflow_permanent_id}",
    response_model=Workflow,
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_workflow",
    },
)
@base_router.get("/workflows/{workflow_permanent_id}/", response_model=Workflow, include_in_schema=False)
async def get_workflow(
    workflow_permanent_id: str,
    version: int | None = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    template: bool = Query(False),
) -> Workflow:
    analytics.capture("skyvern-oss-agent-workflows-get")
    if template:
        if workflow_permanent_id not in await app.STORAGE.retrieve_global_workflows():
            raise InvalidTemplateWorkflowPermanentId(workflow_permanent_id=workflow_permanent_id)

    return await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=None if template else current_org.organization_id,
        version=version,
    )


@base_router.post(
    "/suggest/{ai_suggestion_type}",
    include_in_schema=False,
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "suggest",
    },
)
@base_router.post("/suggest/{ai_suggestion_type}/", include_in_schema=False)
async def suggest(
    ai_suggestion_type: AISuggestionType,
    data: AISuggestionRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> AISuggestionBase:
    llm_prompt = ""

    if ai_suggestion_type == AISuggestionType.DATA_SCHEMA:
        llm_prompt = prompt_engine.load_prompt("suggest-data-schema", input=data.input, additional_context=data.context)

    try:
        new_ai_suggestion = await app.DATABASE.create_ai_suggestion(
            organization_id=current_org.organization_id,
            ai_suggestion_type=ai_suggestion_type,
        )

        llm_response = await app.LLM_API_HANDLER(
            prompt=llm_prompt, ai_suggestion=new_ai_suggestion, prompt_name="suggest-data-schema"
        )
        parsed_ai_suggestion = AISuggestionBase.model_validate(llm_response)

        return parsed_ai_suggestion

    except LLMProviderError:
        LOG.error("Failed to suggest data schema", exc_info=True)
        raise HTTPException(status_code=400, detail="Failed to suggest data schema. Please try again later.")


@base_router.post(
    "/generate/task",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "generate_task",
    },
)
@base_router.post("/generate/task/", include_in_schema=False)
async def generate_task(
    data: GenerateTaskRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TaskGeneration:
    analytics.capture("skyvern-oss-agent-generate-task")
    return await task_v1_service.generate_task(
        user_prompt=data.prompt,
        organization=current_org,
    )


@base_router.put(
    "/organizations",
    tags=["server"],
    openapi_extra={
        "x-fern-sdk-group-name": "server",
        "x-fern-sdk-method-name": "update_organization",
    },
)
@base_router.put(
    "/organizations",
    include_in_schema=False,
)
async def update_organization(
    org_update: OrganizationUpdate,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Organization:
    return await app.DATABASE.update_organization(
        current_org.organization_id,
        max_steps_per_run=org_update.max_steps_per_run,
    )


@base_router.get(
    "/organizations",
    tags=["server"],
    openapi_extra={
        "x-fern-sdk-group-name": "server",
        "x-fern-sdk-method-name": "get_organizations",
    },
)
@base_router.get(
    "/organizations/",
    include_in_schema=False,
)
async def get_organizations(
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> GetOrganizationsResponse:
    return GetOrganizationsResponse(organizations=[current_org])


@base_router.get(
    "/organizations/{organization_id}/apikeys/",
    tags=["server"],
    openapi_extra={
        "x-fern-sdk-group-name": "server",
        "x-fern-sdk-method-name": "get_api_keys",
    },
)
@base_router.get(
    "/organizations/{organization_id}/apikeys",
    include_in_schema=False,
)
async def get_api_keys(
    organization_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> GetOrganizationAPIKeysResponse:
    if organization_id != current_org.organization_id:
        raise HTTPException(status_code=403, detail="You do not have permission to access this organization")
    api_keys = []
    org_auth_token = await app.DATABASE.get_valid_org_auth_token(organization_id, OrganizationAuthTokenType.api)
    if org_auth_token:
        api_keys.append(org_auth_token)
    return GetOrganizationAPIKeysResponse(api_keys=api_keys)


async def _validate_file_size(file: UploadFile) -> UploadFile:
    try:
        file.file.seek(0, 2)  # Move the pointer to the end of the file
        size = file.file.tell()  # Get the current position of the pointer, which represents the file size
        file.file.seek(0)  # Reset the pointer back to the beginning
    except Exception as e:
        raise HTTPException(status_code=500, detail="Could not determine file size.") from e

    if size > app.SETTINGS_MANAGER.MAX_UPLOAD_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File size exceeds the maximum allowed size ({app.SETTINGS_MANAGER.MAX_UPLOAD_FILE_SIZE / 1024 / 1024} MB)",
        )
    return file


@base_router.post(
    "/upload_file",
    tags=["server"],
    openapi_extra={
        "x-fern-sdk-group-name": "server",
        "x-fern-sdk-method-name": "upload_file",
    },
)
@base_router.post(
    "/upload_file/",
    include_in_schema=False,
)
async def upload_file(
    file: UploadFile = Depends(_validate_file_size),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    bucket = app.SETTINGS_MANAGER.AWS_S3_BUCKET_UPLOADS
    todays_date = datetime.datetime.now().strftime("%Y-%m-%d")

    # First try uploading with original filename
    try:
        sanitized_filename = os.path.basename(file.filename)  # Remove any path components
        s3_uri = (
            f"s3://{bucket}/{app.SETTINGS_MANAGER.ENV}/{current_org.organization_id}/{todays_date}/{sanitized_filename}"
        )
        uploaded_s3_uri = await aws_client.upload_file_stream(s3_uri, file.file)
    except Exception:
        LOG.error("Failed to upload file to S3", exc_info=True)
        uploaded_s3_uri = None

    # If upload fails, try again with UUID prefix
    if not uploaded_s3_uri:
        uuid_prefixed_filename = f"{str(uuid.uuid4())}_{file.filename}"
        s3_uri = f"s3://{bucket}/{app.SETTINGS_MANAGER.ENV}/{current_org.organization_id}/{todays_date}/{uuid_prefixed_filename}"
        file.file.seek(0)  # Reset file pointer
        uploaded_s3_uri = await aws_client.upload_file_stream(s3_uri, file.file)

    if not uploaded_s3_uri:
        raise HTTPException(status_code=500, detail="Failed to upload file to S3.")

    # Generate a presigned URL for the uploaded file
    presigned_urls = await aws_client.create_presigned_urls([uploaded_s3_uri])
    if not presigned_urls:
        raise HTTPException(status_code=500, detail="Failed to generate presigned URL.")

    presigned_url = presigned_urls[0]
    return ORJSONResponse(
        content={"s3_uri": uploaded_s3_uri, "presigned_url": presigned_url},
        status_code=200,
        media_type="application/json",
    )


@v2_router.post(
    "/tasks",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "run_task_v2",
    },
)
@v2_router.post(
    "/tasks/",
    include_in_schema=False,
)
async def run_task_v2(
    request: Request,
    background_tasks: BackgroundTasks,
    data: TaskV2Request,
    organization: Organization = Depends(org_auth_service.get_current_org),
    x_max_iterations_override: Annotated[int | str | None, Header()] = None,
    x_max_steps_override: Annotated[int | str | None, Header()] = None,
) -> dict[str, Any]:
    if x_max_iterations_override or x_max_steps_override:
        LOG.info(
            "Overriding max steps for task v2",
            max_iterations_override=x_max_iterations_override,
            max_steps_override=x_max_steps_override,
        )
    await PermissionCheckerFactory.get_instance().check(organization, browser_session_id=data.browser_session_id)

    try:
        task_v2 = await task_v2_service.initialize_task_v2(
            organization=organization,
            user_prompt=data.user_prompt,
            user_url=str(data.url) if data.url else None,
            totp_identifier=data.totp_identifier,
            totp_verification_url=data.totp_verification_url,
            webhook_callback_url=data.webhook_callback_url,
            proxy_location=data.proxy_location,
            publish_workflow=data.publish_workflow,
            create_task_run=True,
            extracted_information_schema=data.extracted_information_schema,
            error_code_mapping=data.error_code_mapping,
        )
    except LLMProviderError:
        LOG.error("LLM failure to initialize task v2", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Skyvern LLM failure to initialize task v2. Please try again later."
        )
    analytics.capture("skyvern-oss-agent-task-v2", data={"url": task_v2.url})
    await AsyncExecutorFactory.get_executor().execute_task_v2(
        request=request,
        background_tasks=background_tasks,
        organization_id=organization.organization_id,
        task_v2_id=task_v2.observer_cruise_id,
        max_steps_override=x_max_steps_override or x_max_iterations_override,
        browser_session_id=data.browser_session_id,
    )
    return task_v2.model_dump(by_alias=True)


@v2_router.get(
    "/tasks/{task_id}",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "get_task_v2",
    },
)
@v2_router.get(
    "/tasks/{task_id}/",
    include_in_schema=False,
)
async def get_task_v2(
    task_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> dict[str, Any]:
    task_v2 = await task_v2_service.get_task_v2(task_id, organization.organization_id)
    if not task_v2:
        raise HTTPException(status_code=404, detail=f"Task v2 {task_id} not found")
    return task_v2.model_dump(by_alias=True)


@base_router.get(
    "/browser_sessions/{browser_session_id}",
    response_model=BrowserSessionResponse,
    tags=["browser"],
    openapi_extra={
        "x-fern-sdk-group-name": "browser",
        "x-fern-sdk-method-name": "get_browser_session",
    },
)
@base_router.get(
    "/browser_sessions/{browser_session_id}/",
    response_model=BrowserSessionResponse,
    include_in_schema=False,
)
async def get_browser_session(
    browser_session_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> BrowserSessionResponse:
    analytics.capture("skyvern-oss-agent-workflow-run-get")
    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
        browser_session_id,
        current_org.organization_id,
    )
    if not browser_session:
        raise HTTPException(status_code=404, detail=f"Browser session {browser_session_id} not found")
    return BrowserSessionResponse.from_browser_session(browser_session)


@base_router.get(
    "/browser_sessions",
    response_model=list[BrowserSessionResponse],
    tags=["browser"],
    openapi_extra={
        "x-fern-sdk-group-name": "browser",
        "x-fern-sdk-method-name": "get_browser_sessions",
    },
)
@base_router.get(
    "/browser_sessions/",
    response_model=list[BrowserSessionResponse],
    include_in_schema=False,
)
async def get_browser_sessions(
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[BrowserSessionResponse]:
    """Get all active browser sessions for the organization"""
    analytics.capture("skyvern-oss-agent-browser-sessions-get")
    browser_sessions = await app.PERSISTENT_SESSIONS_MANAGER.get_active_sessions(current_org.organization_id)
    return [BrowserSessionResponse.from_browser_session(browser_session) for browser_session in browser_sessions]


@base_router.post(
    "/browser_sessions",
    response_model=BrowserSessionResponse,
    tags=["browser"],
    openapi_extra={
        "x-fern-sdk-group-name": "browser",
        "x-fern-sdk-method-name": "create_browser_session",
    },
)
@base_router.post(
    "/browser_sessions/",
    response_model=BrowserSessionResponse,
    include_in_schema=False,
)
async def create_browser_session(
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> BrowserSessionResponse:
    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(current_org.organization_id)
    return BrowserSessionResponse.from_browser_session(browser_session)


@base_router.post(
    "/browser_sessions/{session_id}/close",
    tags=["browser"],
    openapi_extra={
        "x-fern-sdk-group-name": "browser",
        "x-fern-sdk-method-name": "close_browser_session",
    },
)
@base_router.post(
    "/browser_sessions/{session_id}/close/",
    include_in_schema=False,
)
async def close_browser_session(
    session_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ORJSONResponse:
    await app.PERSISTENT_SESSIONS_MANAGER.close_session(current_org.organization_id, session_id)
    return ORJSONResponse(
        content={"message": "Browser session closed"},
        status_code=200,
        media_type="application/json",
    )


async def _flatten_workflow_run_timeline(organization_id: str, workflow_run_id: str) -> list[WorkflowRunTimeline]:
    """
    Get the timeline workflow runs including the nested workflow runs in a flattened list
    """

    # get task v2 by workflow run id
    task_v2_obj = await app.DATABASE.get_task_v2_by_workflow_run_id(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )
    # get all the workflow run blocks
    workflow_run_block_timeline = await app.WORKFLOW_SERVICE.get_workflow_run_timeline(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )
    # loop through the run block timeline, find the task_v2 blocks, flatten the timeline for task_v2
    final_workflow_run_block_timeline = []
    for timeline in workflow_run_block_timeline:
        if not timeline.block:
            continue
        if timeline.block.block_type != BlockType.TaskV2:
            # flatten the timeline for task_v2
            final_workflow_run_block_timeline.append(timeline)
            continue
        if not timeline.block.block_workflow_run_id:
            LOG.warning(
                "Block workflow run id is not set for task_v2 block",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                task_v2_id=task_v2_obj.observer_cruise_id if task_v2_obj else None,
            )
            continue
        # in the future if we want to nested taskv2 shows up as a nested block, we should not flatten the timeline
        workflow_blocks = await _flatten_workflow_run_timeline(
            organization_id=organization_id,
            workflow_run_id=timeline.block.block_workflow_run_id,
        )
        final_workflow_run_block_timeline.extend(workflow_blocks)

    if task_v2_obj and task_v2_obj.observer_cruise_id:
        thought_timeline = await task_v2_service.get_thought_timelines(
            task_v2_id=task_v2_obj.observer_cruise_id,
            organization_id=organization_id,
        )
        final_workflow_run_block_timeline.extend(thought_timeline)
    final_workflow_run_block_timeline.sort(key=lambda x: x.created_at, reverse=True)
    return final_workflow_run_block_timeline


@official_api_router.post(
    "/tasks",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "run_task",
    },
)
@official_api_router.post("/tasks/", include_in_schema=False)
async def run_task(
    request: Request,
    background_tasks: BackgroundTasks,
    run_request: TaskRunRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> TaskRunResponse:
    analytics.capture("skyvern-oss-run-task", data={"url": run_request.url})
    await PermissionCheckerFactory.get_instance().check(current_org, browser_session_id=run_request.browser_session_id)

    if run_request.engine == RunEngine.skyvern_v1:
        # create task v1
        # if there's no url, call task generation first to generate the url, data schema if any
        url = run_request.url
        data_extraction_goal = None
        data_extraction_schema = run_request.data_extraction_schema
        navigation_goal = run_request.goal
        navigation_payload = None
        if not url:
            task_generation = await task_v1_service.generate_task(
                user_prompt=run_request.goal,
                organization=current_org,
            )
            url = task_generation.url
            navigation_goal = task_generation.navigation_goal or run_request.goal
            navigation_payload = task_generation.navigation_payload
            data_extraction_goal = task_generation.data_extraction_goal
            data_extraction_schema = data_extraction_schema or task_generation.extracted_information_schema

        task_v1_request = TaskRequest(
            title=run_request.title,
            url=url,
            navigation_goal=navigation_goal,
            navigation_payload=navigation_payload,
            data_extraction_goal=data_extraction_goal,
            extracted_information_schema=data_extraction_schema,
            error_code_mapping=run_request.error_code_mapping,
            proxy_location=run_request.proxy_location,
            browser_session_id=run_request.browser_session_id,
        )
        task_v1_response = await task_v1_service.run_task(
            task=task_v1_request,
            organization=current_org,
            x_max_steps_override=run_request.max_steps,
            x_api_key=x_api_key,
            request=request,
            background_tasks=background_tasks,
        )
        # build the task run response
        return TaskRunResponse(
            run_id=task_v1_response.task_id,
            title=task_v1_response.title,
            status=str(task_v1_response.status),
            created_at=task_v1_response.created_at,
            updated_at=task_v1_response.modified_at,
            engine=RunEngine.skyvern_v1,
            goal=task_v1_response.navigation_goal,
            url=task_v1_response.url,
            output=task_v1_response.extracted_information,
            failure_reason=task_v1_response.failure_reason,
            data_extraction_schema=task_v1_response.extracted_information_schema,
            error_code_mapping=task_v1_response.error_code_mapping,
            proxy_location=task_v1_response.proxy_location,
            totp_identifier=task_v1_response.totp_identifier,
            totp_url=task_v1_response.totp_verification_url,
            webhook_url=task_v1_response.webhook_callback_url,
            max_steps=task_v1_response.max_steps_per_run,
        )
    if run_request.engine == RunEngine.skyvern_v2:
        # create task v2
        try:
            task_v2 = await task_v2_service.initialize_task_v2(
                organization=current_org,
                user_prompt=run_request.goal,
                user_url=run_request.url,
                totp_identifier=run_request.totp_identifier,
                totp_verification_url=run_request.totp_url,
                webhook_callback_url=run_request.webhook_url,
                proxy_location=run_request.proxy_location,
                publish_workflow=run_request.publish_workflow,
                extracted_information_schema=run_request.data_extraction_schema,
                error_code_mapping=run_request.error_code_mapping,
                create_task_run=True,
            )
        except LLMProviderError:
            LOG.error("LLM failure to initialize task v2", exc_info=True)
            raise HTTPException(
                status_code=500, detail="Skyvern LLM failure to initialize task v2. Please try again later."
            )
        await AsyncExecutorFactory.get_executor().execute_task_v2(
            request=request,
            background_tasks=background_tasks,
            organization_id=current_org.organization_id,
            task_v2_id=task_v2.observer_cruise_id,
            max_steps_override=run_request.max_steps,
            browser_session_id=run_request.browser_session_id,
        )
        return TaskRunResponse(
            run_id=task_v2.observer_cruise_id,
            title=run_request.title,
            status=str(task_v2.status),
            engine=RunEngine.skyvern_v2,
            goal=task_v2.prompt,
            url=task_v2.url,
            output=task_v2.output,
            failure_reason=task_v2.failure_reason,
            webhook_url=task_v2.webhook_callback_url,
            totp_identifier=task_v2.totp_identifier,
            totp_url=task_v2.totp_verification_url,
            proxy_location=task_v2.proxy_location,
            error_code_mapping=task_v2.error_code_mapping,
            data_extraction_schema=task_v2.extracted_information_schema,
            created_at=task_v2.created_at,
            modified_at=task_v2.modified_at,
        )
    raise HTTPException(status_code=400, detail=f"Invalid agent engine: {run_request.engine}")
