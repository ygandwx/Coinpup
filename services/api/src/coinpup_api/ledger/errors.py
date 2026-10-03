"""Stable domain error codes; invalid values are not echoed in error messages."""


class MoneyError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
