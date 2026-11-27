import assert from "node:assert/strict";
import { test } from "node:test";
import { INSTITUTIONS, accountInstitution, getInstitution } from "../src/institutions.ts";

test("institution ids and SWIFT prefixes are unique across supported regions", () => {
    assert.equal(new Set(INSTITUTIONS.map((item) => item.id)).size, INSTITUTIONS.length);
    assert.equal(new Set(INSTITUTIONS.map((item) => item.swiftPrefix)).size, INSTITUTIONS.length);
    for (const institution of INSTITUTIONS) {
        assert.match(institution.swiftPrefix, /^[A-Z]{6}[A-Z0-9]{2}$/);
        assert.equal(getInstitution(institution.id), institution);
    }
});

test("use the explicitly selected institution or recognize an exact legacy identity", () => {
    assert.equal(accountInstitution({ institution: "lhv-ee", bank_name: "HSBC" })?.id, "lhv-ee");
    assert.equal(accountInstitution({ swift_bic: " hsbchkhhhkh " })?.id, "hsbc-hk");
    assert.equal(accountInstitution({ bank_name: "中国工商银行" })?.id, "icbc-cn");
    assert.equal(accountInstitution({ bank_name: "HSBC" }), undefined);
    assert.equal(accountInstitution({ institution: "unknown", bank_name: "LHV Bank" }), undefined);
});
