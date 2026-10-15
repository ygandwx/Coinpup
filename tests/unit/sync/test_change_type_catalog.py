"""Every registered business notification must be readable through the public contract."""

import re
from typing import get_args

from coinpup_api.sync.models import ChangeLog
from coinpup_api.sync.schemas import EntityType


def test_change_response_types_match_the_complete_database_catalog():
    constraint = next(
        value
        for value in ChangeLog.__table__.constraints
        if value.name == "ck_change_log_entity_type"
    )
    assert set(get_args(EntityType)) == set(re.findall(r"'([^']+)'", str(constraint.sqltext)))
