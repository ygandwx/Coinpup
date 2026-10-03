"""Shared browser-session boundary for authenticated business routes."""

import logging
from typing import Annotated

from fastapi import Header, HTTPException, Request
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.auth import (
    AuthError,
    AuthService,
    Identity,
    check_origin,
    cookie_name,
    translate_error,
)
from coinpup_api.config import Settings
from coinpup_api.security import valid_csrf_token

logger = logging.getLogger("coinpup.access")


class BrowserAccess:
    def __init__(self, settings: Settings, engine: Engine | None):
        self.settings = settings
        self.auth = AuthService(settings, engine)
        self.cookie = cookie_name(settings)

    def read(self, request: Request) -> Identity:
        try:
            return self.auth.get_session(request.cookies.get(self.cookie))
        except AuthError as error:
            raise translate_error(error) from None
        except SQLAlchemyError:
            logger.warning("Business session verification unavailable")
            raise HTTPException(503, "authentication_unavailable") from None

    def write(
        self,
        request: Request,
        csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> Identity:
        check_origin(request, self.settings)
        identity = self.read(request)
        token = request.cookies.get(self.cookie)
        if not valid_csrf_token(token, csrf):
            raise HTTPException(403, "csrf_failed")
        return identity
