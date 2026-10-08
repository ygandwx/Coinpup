import { execFileSync } from "node:child_process";

export function seed(photo = false, identity?: string, rows: 1 | 3 = 1, missingLast = false) {
    return JSON.parse(
        execFileSync(
            "docker",
            [
                "compose",
                "exec",
                "-T",
                "-e",
                "COINPUP_ENVIRONMENT=test",
                "-e",
                "COINPUP_CREATE_OCR_BROWSER_FIXTURE=1",
                "-e",
                `COINPUP_BROWSER_FIXTURE_IMAGE=${photo ? "1" : "0"}`,
                "-e",
                `COINPUP_BROWSER_FIXTURE_ROWS=${rows}`,
                "-e",
                `COINPUP_BROWSER_MISSING_LAST=${missingLast ? "1" : "0"}`,
                ...(identity ? ["-e", `COINPUP_BROWSER_FIXTURE_IDENTITY=${identity}`] : []),
                "api",
                "python",
                "scripts/create_ocr_browser_fixture.py",
            ],
            { encoding: "utf8" },
        ),
    );
}

export function workerOutcome(job: string, action: "fail" | "finish") {
    return JSON.parse(
        execFileSync(
            "docker",
            [
                "compose",
                "exec",
                "-T",
                "-e",
                "COINPUP_ENVIRONMENT=test",
                "-e",
                "COINPUP_CREATE_OCR_BROWSER_FIXTURE=1",
                "-e",
                `COINPUP_BROWSER_JOB_ID=${job}`,
                "-e",
                `COINPUP_BROWSER_JOB_ACTION=${action}`,
                "api",
                "python",
                "scripts/finish_ocr_browser_fixture.py",
            ],
            { encoding: "utf8" },
        ),
    );
}
