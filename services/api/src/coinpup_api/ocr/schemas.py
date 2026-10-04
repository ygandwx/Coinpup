"""Browser commands for existing OCR tasks; worker leases are never API inputs."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class JobRetry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_version: Annotated[StrictInt, Field(ge=1)]
