import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

const webGlobals = {
    AbortController: "readonly",
    AbortSignal: "readonly",
    Blob: "readonly",
    DOMException: "readonly",
    File: "readonly",
    FormData: "readonly",
    Headers: "readonly",
    Request: "readonly",
    Response: "readonly",
    TextDecoder: "readonly",
    TextEncoder: "readonly",
    URL: "readonly",
    URLSearchParams: "readonly",
    clearInterval: "readonly",
    clearTimeout: "readonly",
    console: "readonly",
    crypto: "readonly",
    fetch: "readonly",
    queueMicrotask: "readonly",
    setInterval: "readonly",
    setTimeout: "readonly",
    structuredClone: "readonly",
};

const browserGlobals = {
    ...webGlobals,
    document: "readonly",
    localStorage: "readonly",
    navigator: "readonly",
    sessionStorage: "readonly",
    window: "readonly",
};

const nodeGlobals = {
    ...webGlobals,
    Buffer: "readonly",
    clearImmediate: "readonly",
    global: "readonly",
    process: "readonly",
    setImmediate: "readonly",
};

export default [
    { ignores: ["dist/**", "src/generated/**", "playwright-report/**", "test-results/**"] },
    js.configs.recommended,
    ...tseslint.configs.recommended,
    {
        files: ["src/**/*.{js,jsx,ts,tsx}"],
        languageOptions: { globals: browserGlobals },
        plugins: { "react-hooks": reactHooks },
        rules: {
            "react-hooks/rules-of-hooks": "error",
            "react-hooks/exhaustive-deps": "warn",
        },
    },
    {
        files: ["*.config.{js,ts}", "tests/**/*.{js,mjs,cjs,ts}"],
        languageOptions: { globals: nodeGlobals },
    },
    {
        // Playwright runs in Node; page.evaluate and init scripts execute in the browser.
        files: ["e2e/**/*.{js,ts}"],
        languageOptions: { globals: { ...browserGlobals, ...nodeGlobals } },
    },
];
