import { test as base, type TestInfo, type TestInfoError } from "@playwright/test";

const privateValues = new WeakMap<TestInfo, Set<string>>();

// Generated account identifiers can appear in a failed fill, comparison, or accessibility snapshot.
export function protectFixtureValues(...values: string[]) {
    const fields = privateValues.get(base.info());
    for (const value of values) fields?.add(value);
}

function redact(error: TestInfoError, values: Set<string>) {
    delete error.errorContext;
    for (const key of ["message", "stack", "value"] as const) {
        let message = error[key];
        if (!message) continue;
        for (const value of values) message = message.replaceAll(value, "[private fixture]");
        error[key] = message;
    }
    if (error.cause) redact(error.cause, values);
}

export const test = base.extend({
    screenshot: "off",
    trace: "off",
    video: "off",
    page: async ({ page }, use, info) => {
        const fields = new Set<string>();
        if (process.env.E2E_PASSWORD) fields.add(process.env.E2E_PASSWORD);
        privateValues.set(info, fields);
        await use(page);
        // Close before automatic context teardown so reports cannot snapshot the private form.
        await page.close();
        for (const error of info.errors) redact(error, fields);
    },
});
