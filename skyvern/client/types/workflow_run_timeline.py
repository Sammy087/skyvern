# This file was auto-generated by Fern from our API Definition.

from __future__ import annotations
from ..core.pydantic_utilities import UniversalBaseModel
from .workflow_run_timeline_type import WorkflowRunTimelineType
import typing
from .workflow_run_block import WorkflowRunBlock
from .observer_thought import ObserverThought
import datetime as dt
from ..core.pydantic_utilities import IS_PYDANTIC_V2
import pydantic
from ..core.pydantic_utilities import update_forward_refs


class WorkflowRunTimeline(UniversalBaseModel):
    type: WorkflowRunTimelineType
    block: typing.Optional[WorkflowRunBlock] = None
    thought: typing.Optional[ObserverThought] = None
    children: typing.Optional[typing.List["WorkflowRunTimeline"]] = None
    created_at: dt.datetime
    modified_at: dt.datetime

    if IS_PYDANTIC_V2:
        model_config: typing.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(extra="allow", frozen=True)  # type: ignore # Pydantic v2
    else:

        class Config:
            frozen = True
            smart_union = True
            extra = pydantic.Extra.allow


update_forward_refs(WorkflowRunTimeline)
