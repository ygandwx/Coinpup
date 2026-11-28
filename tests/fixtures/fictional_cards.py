"""Generate a new unissued test number for the current test process."""

import secrets

from coinpup_api.ledger.card_details import valid_card_number


def fictional_pan():
    digits = "000000" + f"{secrets.randbelow(10**9):09d}"
    return next(digits + check for check in "0123456789" if valid_card_number(digits + check))
