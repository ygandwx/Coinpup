import { execFileSync } from "node:child_process";

export function seed(photo = false, identity?: string) {
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
                ...(identity ? ["-e", `COINPUP_BROWSER_FIXTURE_IDENTITY=${identity}`] : []),
                "api",
                "python",
                "scripts/create_ocr_browser_fixture.py",
            ],
            { encoding: "utf8" },
        ),
    );
}
