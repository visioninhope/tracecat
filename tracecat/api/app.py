import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import polars as pl
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.params import Body
from fastapi.responses import ORJSONResponse, StreamingResponse
from pydantic_core import ValidationError
from sqlalchemy import Engine, or_
from sqlalchemy.exc import NoResultFound
from sqlmodel import Session, select

from tracecat import config
from tracecat.api.completions import (
    CategoryConstraint,
    FieldCons,
    stream_case_completions,
)
from tracecat.auth import (
    Role,
    authenticate_service,
    authenticate_user,
    authenticate_user_or_service,
)
from tracecat.contexts import ctx_role
from tracecat.db import converters
from tracecat.db.engine import clone_workflow, create_vdb_conn, get_engine
from tracecat.db.schemas import (
    Action,
    ActionRun,
    CaseAction,
    CaseContext,
    CaseEvent,
    Schedule,
    Secret,
    UDFSpec,
    User,
    Webhook,
    Workflow,
    WorkflowDefinition,
    WorkflowRun,
)

# TODO: Clean up API params / response "zoo"
# lots of repetition and inconsistency
from tracecat.dsl.dispatcher import dispatch_workflow
from tracecat.dsl.workflow import DSLInput

# from loguru import logger
from tracecat.logging import logger
from tracecat.middleware import RequestLoggingMiddleware
from tracecat.registry import registry
from tracecat.types.api import (
    ActionMetadataResponse,
    ActionResponse,
    ActionRunEventParams,
    ActionRunResponse,
    CaseActionParams,
    CaseContextParams,
    CaseEventParams,
    CaseParams,
    CopyWorkflowParams,
    CreateActionParams,
    CreateScheduleParams,
    CreateSecretParams,
    CreateWorkflowParams,
    Event,
    EventSearchParams,
    SearchSecretsParams,
    SecretResponse,
    StartWorkflowParams,
    StartWorkflowResponse,
    TriggerWorkflowRunParams,
    UDFArgsValidationResponse,
    Undefined,
    UpdateActionParams,
    UpdateSecretParams,
    UpdateUserParams,
    UpdateWorkflowParams,
    UpsertWebhookParams,
    UpsertWorkflowDefinitionParams,
    WebhookResponse,
    WorkflowMetadataResponse,
    WorkflowResponse,
    WorkflowRunEventParams,
    WorkflowRunResponse,
)
from tracecat.types.cases import Case, CaseMetrics
from tracecat.types.exceptions import TracecatException, TracecatValidationError

engine: Engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = get_engine()
    yield


def create_app(**kwargs) -> FastAPI:
    global logger
    if config.TRACECAT__APP_ENV == "production":
        # NOTE: If you are using Tracecat self-hosted
        # please replace with your own domain
        cors_origins_kwargs = {
            "allow_origins": [
                "https://platform.tracecat.com",
                config.TRACECAT__RUNNER_URL,
            ]
        }
    elif config.TRACECAT__APP_ENV == "staging":
        cors_origins_kwargs = {
            # "allow_origins": [config.TRACECAT__RUNNER_URL],
            # "allow_origin_regex": r"https://tracecat-.*-tracecat\.vercel\.app",
            "allow_origins": "*"
        }
    else:
        cors_origins_kwargs = {
            "allow_origins": "*",
        }
    app = FastAPI(**kwargs)
    app.logger = logger
    app.add_middleware(
        CORSMiddleware,
        **cors_origins_kwargs,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestLoggingMiddleware)
    logger.warning(
        "App started", env=config.TRACECAT__APP_ENV, origins=cors_origins_kwargs
    )
    return app


app = create_app(lifespan=lifespan, default_response_class=ORJSONResponse)


# ----- Utility ----- #


# Catch-all exception handler to prevent stack traces from leaking
@app.exception_handler(Exception)
async def custom_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unexpected error: {!s}",
        exc,
        role=ctx_role.get(),
        params=request.query_params,
        path=request.url.path,
    )
    return ORJSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"message": "An unexpected error occurred. Please try again later."},
    )


@app.exception_handler(TracecatValidationError)
@app.exception_handler(TracecatException)
async def tracecat_exception_handler(request: Request, exc: TracecatException):
    """Generic exception handler for Tracecat exceptions.

    We can customize exceptions to expose only what should be user facing.
    """
    msg = exc.detail if hasattr(exc, "detail") else str(exc)
    logger.error(
        msg,
        role=ctx_role.get(),
        params=request.query_params,
        path=request.url.path,
    )
    return ORJSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"type": type(exc).__name__, "message": msg},
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Hello world. I am the API."}


@app.get("/health")
def check_health() -> dict[str, str]:
    return {"message": "Hello world. I am the API. This is the health endpoint."}


# ----- Trigger handlers ----- #


def validate_incoming_webhook(
    webhook_id: str, secret: str, request: Request
) -> WorkflowDefinition:
    """Validate incoming webhook request.

    NOte: The webhook ID here is the workflow ID.
    """
    with Session(engine) as session:
        result = session.exec(select(Webhook).where(Webhook.workflow_id == webhook_id))
        try:
            # One webhook per workflow
            webhook = result.one()
        except NoResultFound as e:
            logger.opt(exception=e).error("Webhook does not exist", error=e)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized webhook request.",
            ) from e

        if secret != webhook.secret:
            logger.error("Secret does not match")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized webhook request",
            )

        # If we're here, the webhook has been validated
        if webhook.status == "offline":
            logger.error("Webhook is offline")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook is offline",
            )

        if webhook.method.lower() != request.method.lower():
            logger.error("Method does not match")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Request method not allowed",
            )

        # Reaching here means the webhook is online and connected to an entrypoint

        # Match the webhook id with the workflow id and get the latest version
        # of the workflow defitniion.
        result = session.exec(
            select(WorkflowDefinition)
            .where(WorkflowDefinition.workflow_id == webhook_id)
            .order_by(WorkflowDefinition.version.desc())
        )
        try:
            defn = result.first()
            if not defn:
                raise NoResultFound("No workflow definition found for workflow ID")
        except NoResultFound as e:
            # No workflow associated with the webhook
            logger.opt(exception=e).error("Invalid workflow ID", error=e)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Invalid workflow ID"
            ) from e

        # Check if the workflow is active

        if defn.workflow.status == "offline":
            logger.error("Workflow is inactive")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workflow is inactive",
            )

        # If we are here, all checks have passed
        return WorkflowDefinition.model_validate(defn)


async def handle_incoming_webhook(
    request: Request, path: str, secret: str
) -> WorkflowDefinition:
    """Handle incoming webhook requests."""
    # TODO(perf): Replace this when we get async sessions
    with logger.contextualize(webhook_id=path):
        defn = await asyncio.to_thread(
            validate_incoming_webhook, webhook_id=path, secret=secret, request=request
        )
    ctx_role.set(
        Role(type="service", user_id=defn.owner_id, service_id="tracecat-runner")
    )
    return defn


@app.post("/webhooks/{path}/{secret}", tags=["public"])
async def incoming_webhook(
    defn: Annotated[WorkflowDefinition, Depends(handle_incoming_webhook)],
    path: str,
    payload: dict[str, Any] | None = None,
):
    """Webhook endpoint to trigger a workflow.

    Params
    ------
    path: str
        The webhook path, equivalent to the workflow id

    Notes
    -----
    The `Workflow` object holds the RF graph

    Steps
    -----
    0. Authenticate the user
    1. Fetch the DSL from the workflow object
    2. Construct the DSLInput object
    3. Dispatch the workflow

    Todos
    -----
    We're going to use Svix to manage our webhooks.
    """
    role = ctx_role.get()
    logger.info("Webhook hit", path=path, payload=payload)

    # Fetch the DSL from the workflow object
    logger.info("Incoming webhook role", role=role)
    dsl_input = defn.content
    logger.info(dsl_input.dump_yaml())

    asyncio.create_task(dispatch_workflow(dsl_input, workflow_id=path))
    return {"status": "ok"}


# ----- Workflows ----- #


@app.get("/workflows")
def list_workflows(
    role: Annotated[Role, Depends(authenticate_user)],
    library: bool = False,
) -> list[WorkflowMetadataResponse]:
    """List all Workflows in database."""
    query_user_id = role.user_id if not library else "tracecat"
    with Session(engine) as session:
        statement = select(Workflow).where(Workflow.owner_id == query_user_id)
        results = session.exec(statement)
        workflows = results.all()
    workflow_metadata = [
        WorkflowMetadataResponse(
            id=workflow.id,
            title=workflow.title,
            description=workflow.description,
            status=workflow.status,
            icon_url=workflow.icon_url,
        )
        for workflow in workflows
    ]
    return workflow_metadata


@app.post("/workflows", status_code=status.HTTP_201_CREATED)
def create_workflow(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    params: CreateWorkflowParams,
) -> WorkflowMetadataResponse:
    """Create new Workflow with title and description."""

    title = (
        params.title
        if params.title != Undefined.Value
        else datetime.now().strftime("%b %d, %Y, %H:%M:%S")
    )
    # Create the message
    description = (
        params.description
        if params.description != Undefined.Value
        else f"New workflow created {title}"
    )

    with Session(engine) as session:
        workflow = Workflow(
            title=title,
            description=description,
            owner_id=role.user_id,
        )
        # When we create a workflow, we automatically create a webhook
        webhook = Webhook(
            owner_id=role.user_id,
            workflow_id=workflow.id,
        )
        session.add(workflow)
        session.add(webhook)
        session.commit()
        session.refresh(workflow)
        session.refresh(webhook)

    return WorkflowMetadataResponse(
        id=workflow.id,
        title=workflow.title,
        description=workflow.description,
        status=workflow.status,
        icon_url=workflow.icon_url,
    )


@app.get("/workflows/{workflow_id}")
def get_workflow(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
) -> WorkflowResponse:
    """Return Workflow as title, description, list of Action JSONs, adjacency list of Action IDs."""
    with Session(engine) as session:
        # Get Workflow given workflow_id
        statement = select(Workflow).where(
            Workflow.owner_id == role.user_id,
            Workflow.id == workflow_id,
        )
        result = session.exec(statement)
        try:
            workflow = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        actions_responses = {
            action.id: ActionResponse(**action.model_dump())
            for action in workflow.actions or []
        }
        # Add webhook/schedules
        whresponse = WebhookResponse(**workflow.webhook.model_dump())
        return WorkflowResponse(
            **workflow.model_dump(),
            actions=actions_responses,
            webhook=whresponse,
            schedules=workflow.schedules,
        )


@app.post("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def update_workflow(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    params: UpdateWorkflowParams,
) -> None:
    """Update Workflow."""
    with Session(engine) as session:
        statement = select(Workflow).where(
            Workflow.owner_id == role.user_id,
            Workflow.id == workflow_id,
        )
        result = session.exec(statement)
        try:
            workflow = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        for key, value in params.model_dump().items():
            if value is not Undefined.Value:
                setattr(workflow, key, value)

        session.add(workflow)
        session.commit()


@app.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
) -> None:
    """Delete Workflow."""

    with Session(engine) as session:
        statement = select(Workflow).where(
            Workflow.owner_id == role.user_id,
            Workflow.id == workflow_id,
        )
        result = session.exec(statement)
        try:
            workflow = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        session.delete(workflow)
        session.commit()


@app.post("/workflows/{workflow_id}/copy", status_code=status.HTTP_204_NO_CONTENT)
def copy_workflow(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
    params: Annotated[CopyWorkflowParams | None, Body(...)] = None,
) -> None:
    """Copy a Workflow.

    We currently only permit copying workflows from the tracecat user into the user's own account.
    """
    if role.type == "user" and params is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Users can only clone tracecat workflows",
        )
    # Users cannot pass owner_id, and defaults to 'tracecat'
    owner_id = params.owner_id if params else "tracecat"
    with Session(engine) as session:
        statement = select(Workflow).where(
            Workflow.owner_id == owner_id,
            Workflow.id == workflow_id,
        )
        result = session.exec(statement)
        try:
            workflow = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        # Assign it a new workflow ID and owner ID
        new_workflow = clone_workflow(workflow, session, role.user_id)
        session.commit()
        session.refresh(new_workflow)


@app.post("/workflows/{workflow_id}/commit", status_code=status.HTTP_204_NO_CONTENT)
def commit_workflow(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
) -> None:
    """Convert an application state workflow into a `WorkflowDefinition`."""

    # Grab workflow and actions
    with Session(engine) as session:
        statement = select(Workflow).where(
            Workflow.owner_id == role.user_id,
            Workflow.id == workflow_id,
        )
        result = session.exec(statement)
        try:
            workflow = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        # Hydrate actions
        _ = workflow.actions

        # Convert the workflow into a WorkflowDefinition
        dsl = converters.workflow_to_dsl(workflow)
        _upsert_workflow_definition(session, role, workflow_id, dsl)


# ----- Workflow Definitions ----- #


@app.get("/workflows/{workflow_id}/definition")
async def list_workflow_definitions(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
) -> list[WorkflowDefinition]:
    with Session(engine) as session:
        statement = select(WorkflowDefinition).where(
            WorkflowDefinition.owner_id == role.user_id,
        )
        if workflow_id:
            statement = statement.where(WorkflowDefinition.workflow_id == workflow_id)
        result = session.exec(statement)
        return result.all()


@app.get("/workflows/{workflow_id}/definition")
def get_workflow_definition(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
    version: int | None = None,
) -> WorkflowDefinition:
    with Session(engine) as session:
        statement = select(WorkflowDefinition).where(
            WorkflowDefinition.owner_id == role.user_id,
            WorkflowDefinition.workflow_id == workflow_id,
        )
        if version:
            statement = statement.where(WorkflowDefinition.version == version)
        else:
            # Get the latest version
            statement = statement.order_by(WorkflowDefinition.version.desc())

        result = session.exec(statement)
        try:
            defn = result.first()
            if not defn:
                raise NoResultFound
            return defn
        except NoResultFound as e:
            logger.opt(exception=e).error("Workflow definition does not exist", error=e)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Invalid workflow ID"
            ) from e


@app.post("/workflows/{workflow_id}/definition", status_code=status.HTTP_204_NO_CONTENT)
def upsert_workflow_definition(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
    params: UpsertWorkflowDefinitionParams,
) -> None:
    with Session(engine) as session:
        _upsert_workflow_definition(session, role, workflow_id, params.content)


def _upsert_workflow_definition(
    session: Session, role: Role, workflow_id: str, content: DSLInput
) -> WorkflowDefinition:
    statement = (
        select(WorkflowDefinition)
        .where(
            WorkflowDefinition.owner_id == role.user_id,
            WorkflowDefinition.workflow_id == workflow_id,
        )
        .order_by(WorkflowDefinition.version.desc())
    )
    result = session.exec(statement)
    latest_defn = result.first()

    version = latest_defn.version + 1 if latest_defn else 1
    defn = WorkflowDefinition(
        owner_id=role.user_id,
        workflow_id=workflow_id,
        content=content.model_dump(),
        version=version,
    )
    session.add(defn)
    session.commit()
    session.refresh(defn)

    workflow = defn.workflow  # Hydrate relationship attr
    workflow.version = version
    session.add(workflow)
    session.commit()
    session.refresh(workflow)


# ----- Workflow Runs ----- #


@app.get("/workflows/{workflow_id}/runs")
def list_workflow_runs(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    limit: int | None = None,
) -> list[WorkflowRunResponse]:
    """List all Workflow Runs for a Workflow."""
    with Session(engine) as session:
        # Being here means the user has access to the workflow
        statement = select(WorkflowRun).where(
            WorkflowRun.owner_id == role.user_id,
            WorkflowRun.workflow_id == workflow_id,
        )
        if limit is not None:
            statement = statement.limit(limit)
        results = session.exec(statement)
        workflow_runs = results.all()

    workflow_runs_metadata = [
        WorkflowRunResponse(**workflow_run.model_dump())
        for workflow_run in workflow_runs
    ]
    return workflow_runs_metadata


@app.post("/workflows/{workflow_id}/runs", status_code=status.HTTP_201_CREATED)
def create_workflow_run(
    role: Annotated[Role, Depends(authenticate_service)],  # M2M
    workflow_id: str,
    params: WorkflowRunEventParams,
) -> None:
    """Create a Workflow Run."""

    with Session(engine) as session:
        workflow_run = WorkflowRun(workflow_id=workflow_id, **params.model_dump())
        session.add(workflow_run)
        session.commit()
        session.refresh(workflow_run)


@app.get("/workflows/{workflow_id}/runs/{workflow_run_id}")
def get_workflow_run(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    workflow_run_id: str,
) -> WorkflowRunResponse:
    """Return WorkflowRun as title, description, list of Action JSONs, adjacency list of Action IDs."""

    with Session(engine) as session:
        # Get Workflow given workflow_id
        statement = select(WorkflowRun).where(
            WorkflowRun.owner_id == role.user_id,
            WorkflowRun.id == workflow_run_id,
            WorkflowRun.workflow_id == workflow_id,  # Redundant, but for clarity
        )
        result = session.exec(statement)
        try:
            workflow_run = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        # Need this classmethod to instantiate the lazy-laoded action_runs list
        return WorkflowRunResponse.from_orm(workflow_run)


@app.post(
    "/workflows/{workflow_id}/runs/{workflow_run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def update_workflow_run(
    role: Annotated[Role, Depends(authenticate_service)],  # M2M
    workflow_id: str,
    workflow_run_id: str,
    params: WorkflowRunEventParams,
) -> None:
    """Update Workflow."""

    with Session(engine) as session:
        statement = select(WorkflowRun).where(
            WorkflowRun.owner_id == role.user_id,
            WorkflowRun.id == workflow_run_id,
            WorkflowRun.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        try:
            workflow_run = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        workflow_run.status = params.status
        # NOTE: We shouldn't use the DB updated_at field like this, but for now we will
        workflow_run.updated_at = params.created_at

        session.add(workflow_run)
        session.commit()
        session.refresh(workflow_run)


# ----- Workflow Controls ----- #


@app.post("/workflows/{workflow_id}/controls/trigger")
async def trigger_workflow_run(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    params: TriggerWorkflowRunParams,
) -> StartWorkflowResponse:
    """Trigger a Workflow Run."""
    # Create service role
    workflow_params = StartWorkflowParams(
        entrypoint_key=params.action_key,
        entrypoint_payload=params.payload,
    )
    logger.debug(
        "Triggering workflow",
        workflow_id=workflow_id,
        workflow_params=workflow_params,
    )
    try:
        ctx_role.get()
    except LookupError:
        # If not previously set by a webhook, set the role here
        ctx_role.set(role)

    path = "workflow4"
    with Path(f"/app/tracecat/static/workflows/{path}.yaml").resolve().open() as f:
        dsl_yaml = f.read()
    await dispatch_workflow(dsl_yaml)

    return StartWorkflowResponse(
        status="ok", message="Workflow started.", id=workflow_id
    )


# ----- Workflow Webhooks ----- #


@app.get("/workflows/{workflow_id}/webhooks")
def list_webhooks(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
) -> list[WebhookResponse]:
    """List all Webhooks for a workflow."""
    with Session(engine) as session:
        statement = select(Webhook).where(
            Webhook.owner_id == role.user_id,
            Webhook.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        return [WebhookResponse(**webhook.model_dump()) for webhook in result.all()]


@app.post("/workflows/{workflow_id}/webhooks", status_code=status.HTTP_201_CREATED)
def create_webhook(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
    params: UpsertWebhookParams,
) -> None:
    """Create a Webhook."""

    webhook = Webhook(
        owner_id=role.user_id,
        entrypoint_ref=params.entrypoint_ref,
        method=params.method or "POST",
        workflow_id=workflow_id,
    )
    with Session(engine) as session:
        session.add(webhook)
        session.commit()
        session.refresh(webhook)


@app.get("/workflows/{workflow_id}/webhooks/{webhook_id}")
def get_webhook(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    webhook_id: str,
    workflow_id: str,
) -> WebhookResponse:
    with Session(engine) as session:
        statement = select(Webhook).where(
            Webhook.owner_id == role.user_id,
            Webhook.id == webhook_id,
            Webhook.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        try:
            webhook = result.one()
            return WebhookResponse(**webhook.model_dump())
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e


@app.delete("/workflows/{workflow_id}/webhooks/{webhook_id}")
def delete_webhook(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
    webhook_id: str,
) -> None:
    with Session(engine) as session:
        statement = select(Webhook).where(
            Webhook.owner_id == role.user_id,
            Webhook.id == webhook_id,
            Webhook.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        try:
            webhook = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        session.delete(webhook)
        session.commit()


@app.patch("/workflows/{workflow_id}/webhooks/{webhook_id}")
def update_webhook(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    webhook_id: str,
    workflow_id: str,
    params: UpsertWebhookParams,
) -> None:
    with Session(engine) as session:
        statement = select(Webhook).where(
            Webhook.owner_id == role.user_id,
            Webhook.id == webhook_id,
            Webhook.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        try:
            webhook = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        if params.entrypoint_ref is not None:
            webhook.entrypoint_ref = params.entrypoint_ref
        if params.method is not None:
            webhook.method = params.method
        if params.status is not None:
            webhook.status = params.status

        session.add(webhook)
        session.commit()
        session.refresh(webhook)


# ----- Workflow Schedules ----- #


@app.get("/workflows/{workflow_id}/schedules")
def list_schedules(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
) -> list[Schedule]:
    """List all Schedules for a workflow."""
    with Session(engine) as session:
        statement = select(Schedule).where(
            Schedule.owner_id == role.user_id,
            Schedule.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        return result.all()


@app.post("/workflows/{workflow_id}/schedules", status_code=status.HTTP_201_CREATED)
def create_schedule(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
    params: CreateScheduleParams,
) -> None:
    """Create a Schedule."""

    schedule = Schedule(
        owner_id=role.user_id,
        cron=params.cron,
        entrypoint_payload=params.entrypoint_payload,
        entrypoint_ref=params.entrypoint_ref,
        workflow_id=workflow_id,
    )
    with Session(engine) as session:
        session.add(schedule)
        session.commit()
        session.refresh(schedule)


@app.get("/workflows/{workflow_id}/schedules/{schedule_id}")
def get_schedule(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    schedule_id: str,
    workflow_id: str,
) -> Schedule:
    with Session(engine) as session:
        statement = select(Schedule).where(
            Schedule.owner_id == role.user_id,
            Schedule.id == schedule_id,
            Schedule.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        try:
            return result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e


@app.delete("/workflows/{workflow_id}/schedules/{schedule_id}")
def delete_schedule(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    schedule_id: str,
    workflow_id: str,
) -> None:
    with Session(engine) as session:
        statement = select(Schedule).where(
            Schedule.owner_id == role.user_id,
            Schedule.id == schedule_id,
            Schedule.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        try:
            schedule = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        session.delete(schedule)
        session.commit()


# ----- Actions ----- #


@app.get("/actions")
def list_actions(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
) -> list[ActionMetadataResponse]:
    """List all Actions related to `workflow_id`."""
    with Session(engine) as session:
        statement = select(Action).where(
            Action.owner_id == role.user_id,
            Action.workflow_id == workflow_id,
        )
        results = session.exec(statement)
        actions = results.all()
    action_metadata = [
        ActionMetadataResponse(
            id=action.id,
            workflow_id=workflow_id,
            type=action.type,
            title=action.title,
            description=action.description,
            status=action.status,
            key=action.key,
        )
        for action in actions
    ]
    return action_metadata


@app.post("/actions")
def create_action(
    role: Annotated[Role, Depends(authenticate_user)],
    params: CreateActionParams,
) -> ActionMetadataResponse:
    with Session(engine) as session:
        action = Action(
            owner_id=role.user_id,
            workflow_id=params.workflow_id,
            type=params.type,
            title=params.title,
            description="",  # Default to empty string
        )
        session.add(action)
        session.commit()
        session.refresh(action)

    action_metadata = ActionMetadataResponse(
        id=action.id,
        workflow_id=params.workflow_id,
        type=params.type,
        title=action.title,
        description=action.description,
        status=action.status,
        key=action.key,
    )
    return action_metadata


@app.get("/actions/{action_id}")
def get_action(
    role: Annotated[Role, Depends(authenticate_user)],
    action_id: str,
    workflow_id: str,
) -> ActionResponse:
    with Session(engine) as session:
        statement = select(Action).where(
            Action.owner_id == role.user_id,
            Action.id == action_id,
            Action.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        try:
            action = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

    return ActionResponse(
        id=action.id,
        type=action.type,
        title=action.title,
        description=action.description,
        status=action.status,
        inputs=action.inputs,
        key=action.key,
    )


@app.post("/actions/{action_id}")
def update_action(
    role: Annotated[Role, Depends(authenticate_user)],
    action_id: str,
    params: UpdateActionParams,
) -> ActionResponse:
    with Session(engine) as session:
        # Fetch the action by id
        statement = select(Action).where(
            Action.owner_id == role.user_id,
            Action.id == action_id,
        )
        result = session.exec(statement)
        try:
            action = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        if params.title is not None:
            action.title = params.title
        if params.description is not None:
            action.description = params.description
        if params.status is not None:
            action.status = params.status
        if params.inputs is not None:
            action.inputs = params.inputs

        session.add(action)
        session.commit()
        session.refresh(action)

    return ActionResponse(
        id=action.id,
        type=action.type,
        title=action.title,
        description=action.description,
        status=action.status,
        inputs=action.inputs,
        key=action.key,
    )


@app.delete("/actions/{action_id}", status_code=204)
def delete_action(
    role: Annotated[Role, Depends(authenticate_user)],
    action_id: str,
) -> None:
    with Session(engine) as session:
        statement = select(Action).where(
            Action.owner_id == role.user_id,
            Action.id == action_id,
        )
        result = session.exec(statement)
        try:
            action = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        # If the user doesn't own this workflow, they can't delete the action
        session.delete(action)
        session.commit()


# ----- Action Runs ----- #


@app.get("/actions/{action_id}/runs")
def list_action_runs(
    role: Annotated[Role, Depends(authenticate_user)],
    action_id: str,
    limit: int | None = None,
) -> list[ActionRunResponse]:
    """List all action Runs for an action."""
    with Session(engine) as session:
        # Being here means the user has access to the action
        statement = select(ActionRun).where(
            ActionRun.owner_id == role.user_id,
            ActionRun.action_id == action_id,
        )
        if limit is not None:
            statement = statement.limit(limit)
        results = session.exec(statement)
        action_runs = results.all()

    action_runs_metadata = [
        ActionRunResponse.from_orm(action_run) for action_run in action_runs
    ]
    return action_runs_metadata


@app.post("/actions/{action_id}/runs", status_code=status.HTTP_201_CREATED)
def create_action_run(
    role: Annotated[Role, Depends(authenticate_service)],  # M2M
    action_id: str,
    params: ActionRunEventParams,
) -> ActionRunResponse:
    """Create a action Run."""

    action_run = ActionRun(action_id=action_id, **params.model_dump())
    with Session(engine) as session:
        session.add(action_run)
        session.commit()
        session.refresh(action_run)

    return ActionRunResponse.from_orm(action_run)


@app.get("/actions/{action_id}/runs/{action_run_id}")
def get_action_run(
    role: Annotated[Role, Depends(authenticate_user)],
    action_id: str,
    action_run_id: str,
) -> ActionRunResponse:
    """Return ActionRun as title, description, of Action JSONs, adjacency list of Action IDs."""

    with Session(engine) as session:
        # Get action given action_id
        statement = select(ActionRun).where(
            ActionRun.owner_id == role.user_id,
            ActionRun.id == action_run_id,
            ActionRun.action_id == action_id,  # Redundant, but for clarity
        )
        result = session.exec(statement)
        try:
            action_run = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

    return ActionRunResponse.from_orm(action_run)


@app.post(
    "/actions/{action_id}/runs/{action_run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def update_action_run(
    role: Annotated[Role, Depends(authenticate_service)],  # M2M
    action_id: str,
    action_run_id: str,
    params: ActionRunEventParams,
) -> None:
    """Update action."""

    with Session(engine) as session:
        statement = select(ActionRun).where(
            ActionRun.owner_id == role.user_id,
            ActionRun.id == action_run_id,
            ActionRun.action_id == action_id,
        )
        result = session.exec(statement)
        try:
            action_run = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        action_run.status = params.status
        # NOTE: We shouldn't use the DB updated_at field like this, but for now we will
        action_run.updated_at = params.created_at
        if params.error_msg is not None:
            action_run.error_msg = params.error_msg
        if params.result is not None:
            action_run.result = params.result

        session.add(action_run)
        session.commit()
        session.refresh(action_run)


# ----- Events Management ----- #


SUPPORTED_EVENT_AGGS = {
    "count": pl.count,
    "max": pl.max,
    "avg": pl.mean,
    "median": pl.median,
    "min": pl.min,
    "std": pl.std,
    "sum": pl.sum,
    "var": pl.var,
}


@app.get("/events/search")
def search_events(
    role: Annotated[Role, Depends(authenticate_user)],
    params: EventSearchParams,
) -> list[Event]:
    """Search for events based on query parameters.

    Note: currently on supports filter by `workflow_id` and sort by `published_at`.
    """
    raise NotImplementedError


# ----- Case Management ----- #


@app.post("/workflows/{workflow_id}/cases", status_code=status.HTTP_201_CREATED)
def create_case(
    role: Annotated[Role, Depends(authenticate_service)],  # M2M
    workflow_id: str,
    cases: list[CaseParams],
):
    db = create_vdb_conn()
    tbl = db.open_table("cases")
    # Should probably also add a check for existing case IDs
    new_cases = [
        Case(**c.model_dump(), owner_id=role.user_id, workflow_id=workflow_id)
        for c in cases
    ]
    tbl.add([case.flatten() for case in new_cases])


@app.get("/workflows/{workflow_id}/cases")
def list_cases(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    limit: int = 100,
) -> list[Case]:
    """List all cases under a workflow.

    Note: currently only supports listing the first 100 cases.
    """
    db = create_vdb_conn()
    tbl = db.open_table("cases")
    result = (
        tbl.search()
        .where(f"(owner_id = {role.user_id!r}) AND (workflow_id = {workflow_id!r})")
        .select(list(Case.model_fields.keys()))
        .limit(limit)
        .to_polars()
        .to_dicts()
    )
    return [Case.from_flattened(c) for c in result]


@app.get("/workflows/{workflow_id}/cases/{case_id}")
def get_case(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    case_id: str,
) -> Case:
    """Get a specific case by ID under a workflow."""
    db = create_vdb_conn()
    tbl = db.open_table("cases")
    result = (
        tbl.search()
        .where(
            f"(owner_id = {role.user_id!r}) AND (workflow_id = {workflow_id!r}) AND (id = {case_id!r})"
        )
        .select(list(Case.model_fields.keys()))
        .limit(1)
        .to_polars()
        .to_dicts()
    )
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
        )
    return Case.from_flattened(result[0])


@app.post("/workflows/{workflow_id}/cases/{case_id}")
def update_case(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    case_id: str,
    params: CaseParams,
):
    """Update a specific case by ID under a workflow."""
    updated_case = Case.from_params(params, owner_id=role.user_id, id=case_id)
    db = create_vdb_conn()
    tbl = db.open_table("cases")
    tbl.update(
        where=f"(owner_id = {role.user_id!r}) AND (workflow_id = {workflow_id!r}) AND (id = {case_id!r})",
        values=updated_case.flatten(),
    )


@app.post(
    "/workflows/{workflow_id}/cases/{case_id}/events",
    status_code=status.HTTP_201_CREATED,
)
def create_case_event(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    case_id: str,
    params: CaseEventParams,
) -> None:
    """Create a new Case Event."""
    case_event = CaseEvent(
        owner_id=role.user_id,
        case_id=case_id,
        workflow_id=workflow_id,
        initiator_role=role.type,
        **params.model_dump(),
    )
    with Session(engine) as session:
        session.add(case_event)
        session.commit()
        session.refresh(case_event)
        return case_event


@app.get("/workflows/{workflow_id}/cases/{case_id}/events")
def list_case_events(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    case_id: str,
) -> list[CaseEvent]:
    """List all Case Events."""
    with Session(engine) as session:
        query = select(CaseEvent).where(
            CaseEvent.owner_id == role.user_id,
            CaseEvent.workflow_id == workflow_id,
            CaseEvent.case_id == case_id,
        )
        case_events = session.exec(query).all()
        return case_events


@app.get("/workflows/{workflow_id}/cases/{case_id}/events/{event_id}")
def get_case_event(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    case_id: str,
    event_id: str,
):
    """Get a specific case event by ID under a workflow."""
    with Session(engine) as session:
        query = select(CaseEvent).where(
            CaseEvent.owner_id == role.user_id,
            CaseEvent.workflow_id == workflow_id,
            CaseEvent.case_id == case_id,
            CaseEvent.id == event_id,
        )
        case_event = session.exec(query).one_or_none()
        if case_event is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            )
        return case_event


@app.get("/workflows/{workflow_id}/cases/{case_id}/metrics")
def get_case_metrics(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    case_id: str,
) -> CaseMetrics:
    """Get a specific case by ID under a workflow."""
    db = create_vdb_conn()
    tbl = db.open_table("cases")
    df = pl.DataFrame(
        tbl.search()
        .where(
            f"(owner_id = {role.user_id!r}) AND (workflow_id = {workflow_id!r}) AND (id = {case_id!r})"
        )
        .select(list(Case.model_fields.keys()))
        .to_arrow()
    ).to_dicts()
    return df


# ----- Available Case Actions ----- #


@app.get("/case-actions")
def list_case_actions(
    role: Annotated[Role, Depends(authenticate_user)],
) -> list[CaseAction]:
    with Session(engine) as session:
        statement = select(CaseAction).where(
            or_(
                CaseAction.owner_id == "tracecat",
                CaseAction.owner_id == role.user_id,
            )
        )
        actions = session.exec(statement).all()
    return actions


@app.post("/case-actions")
def create_case_action(
    role: Annotated[Role, Depends(authenticate_user)],
    params: CaseActionParams,
) -> CaseAction:
    case_action = CaseAction(owner_id=role.user_id, **params.model_dump())
    with Session(engine) as session:
        session.add(case_action)
        session.commit()
        session.refresh(case_action)
    return case_action


@app.delete("/case-actions/{case_action_id}")
def delete_case_action(
    role: Annotated[Role, Depends(authenticate_user)],
    case_action_id: str,
):
    with Session(engine) as session:
        statement = select(CaseAction).where(
            CaseAction.owner_id == role.user_id,
            CaseAction.id == case_action_id,
        )
        result = session.exec(statement)
        try:
            action = result.one()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        session.delete(action)
        session.commit()


# ----- Available Context Labels ----- #


@app.get("/case-contexts")
def list_case_contexts(
    role: Annotated[Role, Depends(authenticate_user)],
) -> list[CaseContext]:
    with Session(engine) as session:
        statement = select(CaseContext).where(
            or_(
                CaseContext.owner_id == "tracecat",
                CaseContext.owner_id == role.user_id,
            )
        )
        actions = session.exec(statement).all()
    return actions


@app.post("/case-contexts")
def create_case_context(
    role: Annotated[Role, Depends(authenticate_user)],
    params: CaseContextParams,
) -> CaseContext:
    case_context = CaseContext(owner_id=role.user_id, **params.model_dump())
    with Session(engine) as session:
        session.add(case_context)
        session.commit()
        session.refresh(case_context)
    return params


@app.delete("/case-contexts/{case_context_id}")
def delete_case_context(
    role: Annotated[Role, Depends(authenticate_user)],
    case_context_id: str,
):
    with Session(engine) as session:
        statement = select(CaseContext).where(
            CaseContext.owner_id == role.user_id,
            CaseContext.id == case_context_id,
        )
        result = session.exec(statement)
        try:
            action = result.one()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        session.delete(action)
        session.delete(action)
        session.commit()
    pass


@app.post("/completions/cases/stream")
async def streaming_autofill_case_fields(
    role: Annotated[Role, Depends(authenticate_user)],
    cases: list[Case],  # TODO: Replace this with case IDs
    fields: list[str],
) -> dict[str, str]:
    """List of case IDs.
    Steps
    -----
    1. Using Case IDs, fetch case data
    2. Figure out  which fields need to be populated - these fields are None
    3. Complete the fields

    """
    fields_set = set(fields)

    if not fields_set:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields provided for completions",
        )
    if not all((f in Case.model_fields) for f in fields_set):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid fields provided for case completions",
        )

    field_cons_map: FieldCons = {}

    if "tags" in fields_set:
        # TODO: Rename context to tags, in DB as well
        case_contexts = list_case_contexts(role)
        contexts_mapping = (
            pl.DataFrame(case_contexts)
            .lazy()
            .select(
                pl.col.tag,
                pl.col.value.str.split(".").list.first(),
            )
            .unique()
            .group_by(pl.col.tag)
            .agg(pl.col.value)
            .collect(streaming=True)
            .to_dicts()
        )
        context_cons = [
            CategoryConstraint.model_validate(d, strict=True) for d in contexts_mapping
        ]
        field_cons_map["tags"] = context_cons

    return StreamingResponse(
        stream_case_completions(cases, field_cons=field_cons_map),
        media_type="application/x-ndjson",
    )


# ----- Users ----- #


@app.put("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    role: Annotated[Role, Depends(authenticate_user)],
) -> User:
    """Create new user.

    Note that this is just for user config, auth is done separately."""

    # Check if user exists

    with Session(engine) as session:
        # Check if user exists
        statement = select(User).where(User.id == role.user_id).limit(1)
        result = session.exec(statement)

        user = result.one_or_none()
        if user is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="User already exists"
            )
        user = User(owner_id="tracecat", id=role.user_id)

        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@app.get("/users")
def get_user(
    role: Annotated[Role, Depends(authenticate_user)],
) -> User:
    """Return user as title, description, list of Action JSONs, adjacency list of Action IDs."""

    with Session(engine) as session:
        # Get user given user_id
        statement = select(User).where(User.id == role.user_id)
        result = session.exec(statement)
        try:
            user = result.one()
            return user
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            ) from e


@app.post("/users", status_code=status.HTTP_204_NO_CONTENT)
def update_user(
    role: Annotated[Role, Depends(authenticate_user)],
    params: UpdateUserParams,
) -> None:
    """Update user."""

    with Session(engine) as session:
        statement = select(User).where(User.id == role.user_id)
        result = session.exec(statement)
        try:
            user = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            ) from e

        if params.tier is not None:
            user.tier = params.tier
        if params.settings is not None:
            user.settings = params.settings

        session.add(user)
        session.commit()


@app.delete("/users", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    role: Annotated[Role, Depends(authenticate_user)],
) -> None:
    """Delete user."""

    with Session(engine) as session:
        statement = select(User).where(User.id == role.user_id)
        result = session.exec(statement)
        try:
            user = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            ) from e
        session.delete(user)
        session.commit()


# ----- Secrets ----- #


@app.get("/secrets")
def list_secrets(
    role: Annotated[Role, Depends(authenticate_user)],
) -> list[SecretResponse]:
    """List all secrets for a user."""
    with Session(engine) as session:
        statement = select(Secret).where(Secret.owner_id == role.user_id)
        result = session.exec(statement)
        secrets = result.all()
        return [
            SecretResponse(
                id=secret.id,
                type=secret.type,
                name=secret.name,
                description=secret.description,
                keys=secret.keys or [],
            )
            for secret in secrets
        ]


@app.get("/secrets/{secret_name}")
def get_secret(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    secret_name: str,
) -> Secret:
    """Get a secret by ID.

    Support access for both user and service roles."""

    with Session(engine) as session:
        # Check if secret exists
        statement = (
            select(Secret)
            .where(Secret.owner_id == role.user_id, Secret.name == secret_name)
            .limit(1)
        )
        result = session.exec(statement)
        secret = result.one_or_none()
        if secret is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Secret not found"
            )
        # NOTE: IMPLICIT TYPE COERCION
        # Encrypted keys as bytes gets cast a string as to be JSON serializable
        return secret


@app.put("/secrets", status_code=status.HTTP_201_CREATED)
def create_secret(
    role: Annotated[Role, Depends(authenticate_user)],
    params: CreateSecretParams,
) -> None:
    """Get a secret by ID."""
    with Session(engine) as session:
        # Check if secret exists
        statement = (
            select(Secret)
            .where(Secret.owner_id == role.user_id, Secret.name == params.name)
            .limit(1)
        )
        result = session.exec(statement)
        secret = result.one_or_none()
        if secret is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Secret already exists"
            )
        new_secret = Secret(
            owner_id=role.user_id,
            name=params.name,
            type=params.type,
            description=params.description,
            tags=params.tags,
        )
        new_secret.keys = params.keys  # Set and encrypt the key

        session.add(new_secret)
        session.commit()
        session.refresh(new_secret)


@app.post("/secrets", status_code=status.HTTP_201_CREATED)
def update_secret(
    role: Annotated[Role, Depends(authenticate_user)],
    params: UpdateSecretParams,
) -> Secret:
    """Get a secret by ID."""
    with Session(engine) as session:
        # Check if secret exists
        statement = (
            select(Secret)
            .where(Secret.owner_id == role.user_id, Secret.name == params.name)
            .limit(1)
        )
        result = session.exec(statement)
        secret = result.one_or_none()
        if secret is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Secret does not exist"
            )
        secret.keys = params.keys  # Set and encrypt the key
        session.add(secret)
        session.commit()
        session.refresh(secret)


@app.delete("/secrets/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_secret(
    role: Annotated[Role, Depends(authenticate_user)],
    secret_id: str,
) -> None:
    """Get a secret by ID."""
    with Session(engine) as session:
        # Check if secret exists
        statement = (
            select(Secret)
            .where(Secret.owner_id == role.user_id, Secret.id == secret_id)
            .limit(1)
        )
        result = session.exec(statement)
        secret = result.one_or_none()
        if secret is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Secret does not exist"
            )
        session.delete(secret)
        session.commit()


@app.post("/secrets/search")
def search_secrets(
    role: Annotated[Role, Depends(authenticate_user)],
    params: SearchSecretsParams,
) -> list[Secret]:
    """Get a secret by ID."""
    with Session(engine) as session:
        statement = (
            select(Secret)
            .where(Secret.owner_id == role.user_id)
            .filter(*[Secret.name == name for name in params.names])
        )
        result = session.exec(statement)
        secrets = result.all()
        return secrets


# ----- UDFs ----- #


@app.get("/udfs")
def list_udfs(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    limit: int | None = None,
    namespace: str = Query(None),
) -> list[UDFSpec]:
    """List all UDF specs for a user."""
    with Session(engine) as session:
        statement = select(UDFSpec).where(
            or_(
                UDFSpec.owner_id == "tracecat",
                UDFSpec.owner_id == role.user_id,
            )
        )
        if namespace:
            statement = statement.where(UDFSpec.key.startswith(namespace))
        if limit:
            statement = statement.limit(limit)
        result = session.exec(statement)
        udfs = result.all()
        return udfs


@app.get("/udfs/{key}")
def get_udf(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    key: str,
    namespace: str = Query(None),
) -> UDFSpec:
    """Get an UDF spec by its path."""
    with Session(engine) as session:
        statement = select(UDFSpec).where(
            or_(
                UDFSpec.owner_id == "tracecat",
                UDFSpec.owner_id == role.user_id,
            ),
            UDFSpec.key == key,
        )
        if namespace:
            statement = statement.where(UDFSpec.namespace == namespace)
        result = session.exec(statement)
        udf = result.one_or_none()
        if udf is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="udf not found"
            )
        return udf


@app.post("/udfs/{udf_key}")
def create_udf(
    role: Annotated[Role, Depends(authenticate_user)],
    udf_key: str,
) -> UDFSpec:
    """Get an UDF spec by its path."""
    _, platform, name = udf_key.split(".")
    with Session(engine) as session:
        statement = select(UDFSpec).where(
            UDFSpec.owner_id == role.user_id,
            UDFSpec.platform == platform,
            UDFSpec.name == name,
        )
        result = session.exec(statement)
        udf = result.one_or_none()
        if udf is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="udf not found"
            )
        return udf


@app.post("/udfs/{key}/validate")
def validate_udf_args(
    role: Annotated[Role, Depends(authenticate_user)],
    key: str,
    args: dict[str, Any],
) -> UDFArgsValidationResponse:
    """Get an UDF spec by its path."""
    try:
        udf = registry.get(key)
    except KeyError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"UDF {key!r} not found"
        ) from e
    try:
        udf.validate_args(**args)
        return UDFArgsValidationResponse(ok=True, message="UDF args are valid")
    except ValidationError as e:
        logger.opt(exception=e).error("Error validating UDF args")
        return UDFArgsValidationResponse(
            ok=False, message="Error validating UDF args", detail=e.errors()
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unexpected error validating UDF args",
        ) from e
