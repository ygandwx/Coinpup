"""Validate card metadata at the account boundary and project it without full PANs."""

import re
import unicodedata

CARD_KINDS = frozenset({"credit_card", "debit_card"})
_PAN_KEYS = {
    "cardnumber",
    "creditcardnumber",
    "debitcardnumber",
    "cardno",
    "creditcardno",
    "debitcardno",
    "cardpan",
    "pan",
    "primaryaccountnumber",
    "卡号",
    "信用卡号",
    "借记卡号",
    "银行卡号",
}
_SECURITY_KEYS = {
    "pin",
    "cid",
    "csc",
    "pinno",
    "pinnumber",
    "pincode",
    "personalidentificationnumber",
    "cardsecuritycode",
    "cardverificationcode",
    "cardverificationvalue",
    "cardvalidationcode",
    "cardvalidationvalue",
    "cardpin",
    "cardpassword",
    "securitycode",
    "verificationcode",
    "validationcode",
    "卡密码",
    "卡片密码",
    "银行卡密码",
    "取款密码",
    "交易密码",
    "卡安全码",
    "卡片安全码",
    "信用卡安全码",
    "银行卡安全码",
    "安全码",
    "验证码",
    "安全验证码",
    "privatekey",
    "walletprivatekey",
    "seed",
    "seedphrase",
    "walletseed",
    "recoveryphrase",
    "mnemonic",
    "mnemonicphrase",
    "bip39mnemonic",
    "secretkey",
    "私钥",
    "私鑰",
    "助记词",
    "助記詞",
    "恢复短语",
    "恢復短語",
}
# These named fields can legitimately contain digit sequences unrelated to the card.
_NON_PAN_FIELDS = {
    "institution",
    "bankregion",
    "bankname",
    "accountholder",
    "branchname",
    "swiftbic",
    "cnapscode",
    "routingnumber",
    "iban",
    "hkbankcode",
    "realname",
    "email",
    "paymentaccountid",
    "linkedaccountnumber",
    "issuername",
    "statementday",
    "paymentdueday",
    "walletnetwork",
    "walletaddress",
    "walletlabel",
    "walletstorage",
    "cashlocation",
    "creditlimit",
    "cardholder",
}


def _key(key: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", key).casefold()
        if character.isalnum()
    )


def _security_field(key: str) -> bool:
    return (
        key in _SECURITY_KEYS
        or key.endswith(
            ("privatekey", "seedphrase", "recoveryphrase", "mnemonic", "mnemonicphrase")
        )
        or any(marker in key for marker in ("cvv", "cvc", "cvn", "pinnumber", "pincode"))
        or bool(
            re.fullmatch(r"(?:card|creditcard|debitcard|atm|security|transaction|payment)pin", key)
        )
    )


def valid_card_number(digits: str) -> bool:
    if not re.fullmatch(r"[0-9]{12,19}", digits):
        return False
    total = 0
    for position, character in enumerate(reversed(digits)):
        value = int(character)
        if position % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _pan_details(kind: str, key: str, value: str) -> bool:
    if key in _PAN_KEYS:
        return True
    if kind not in CARD_KINDS or not value or key in _NON_PAN_FIELDS:
        return False
    digits = re.sub(r"[\s-]+", "", unicodedata.normalize("NFKC", value))
    return bool(re.fullmatch(r"[0-9]{12,19}", digits)) and (
        key == "accountnumber" or valid_card_number(digits)
    )


def valid_account_details(kind: str, details: dict[str, str], previous: dict[str, str]) -> bool:
    """Permit editing old metadata while rejecting new secret fields and malformed values."""
    for key, value in details.items():
        normalized = _key(key)
        if _security_field(normalized):
            return False
        if previous.get(key) == value or not value:
            continue
        if _pan_details(kind, normalized, value):
            return False
        if normalized == "cardlastfour" and not re.fullmatch(r"[0-9]{4}", value):
            return False
        if normalized in {"expiry", "cardexpiry", "cardexpirydate", "expirationdate"}:
            if not re.fullmatch(r"(?:0[1-9]|1[0-2])/[0-9]{2}", value):
                return False
        if kind != "credit_card":
            continue
        if normalized in {"statementday", "paymentdueday"} and not re.fullmatch(
            r"(?:[1-9]|[12][0-9]|3[01])", value
        ):
            return False
        if normalized == "creditlimit" and not re.fullmatch(
            r"(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,18})?", value
        ):
            return False
    return True


def _contains_card_copy(value: str, reference: str) -> bool:
    # A legacy linked account ID or note can repeat the PAN. Identify candidates
    # using the permitted suffix without fetching the dedicated encrypted number.
    for match in re.finditer(
        r"(?<![0-9])(?:[0-9][ -]?){11,18}[0-9](?![0-9])", unicodedata.normalize("NFKC", value)
    ):
        digits = re.sub(r"[ -]", "", match.group())
        if (digits == reference or digits[-4:] == reference) and valid_card_number(digits):
            return True
    return False


def without_card_copies(details: dict[str, str], reference: str | None) -> dict[str, str]:
    if reference is None:
        return details
    return {
        key: value
        for key, value in details.items()
        if not (_contains_card_copy(key, reference) or _contains_card_copy(value, reference))
    }


def public_account_details(
    kind: str, details: dict[str, str], *, saved_card_suffix: str | None = None
) -> dict[str, str]:
    """Keep stored legacy values private when they contain security data or a PAN."""
    details = without_card_copies(details, saved_card_suffix)
    return {
        key: value
        for key, value in details.items()
        if not (
            _security_field(normalized := _key(key))
            or _pan_details(kind, normalized, value)
            or (normalized == "cardlastfour" and not re.fullmatch(r"[0-9]{4}", value))
        )
    }
