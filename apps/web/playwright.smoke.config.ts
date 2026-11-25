import { defineConfig } from "@playwright/test";

import base from "./playwright.config";

// Core path checked on every pull request. Push to main, nightly and manual runs use the full suite.
export const smokeSpecs = [
    "auth.spec.ts",
    "structure.spec.ts",
    "posting.spec.ts",
    "revision.spec.ts",
    "files.spec.ts",
    "master-data.spec.ts",
    "periods.spec.ts",
    "business-drafts.spec.ts",
    "business-confirmations.spec.ts",
    "business-payment-web.spec.ts",
    "business-refunds.spec.ts",
    "business-invoice-issuance.spec.ts",
    "business-project-activity.spec.ts",
    "interledger.spec.ts",
    "valuation.spec.ts",
    "reminder-email.spec.ts",
];

export default defineConfig({ ...base, testMatch: smokeSpecs });
