"""Operate the AOSP camera activity opened by the on-device receipt acceptance test."""

import re
import subprocess
import time
import xml.etree.ElementTree as ET

CAMERA = "com.android.camera2"
LAYOUT = "/data/local/tmp/coinpup-native-camera.xml"
PERMISSION_CONTROLLERS = {
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
}
ALLOW_WHILE_USING = "permission_allow_foreground_only_button"
CAPTURE_INTENT = "android.media.action.IMAGE_CAPTURE"
BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
TOP_ACTIVITY = re.compile(r"topResumedActivity[=:]\s*ActivityRecord\{[^\n]+? u\d+ ([\w.]+)/")


def adb(*arguments: str) -> str:
    return subprocess.run(
        ["adb", *arguments], check=True, capture_output=True, text=True, timeout=15
    ).stdout.strip()


def foreground_package() -> str:
    activities = adb("shell", "dumpsys", "activity", "activities")
    match = TOP_ACTIVITY.search(activities)
    return match.group(1) if match else ""


def tap_control(resource_id: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_seen = ""
    while time.monotonic() < deadline:
        last_seen = foreground_package()
        if last_seen == CAMERA or last_seen in PERMISSION_CONTROLLERS:
            try:
                adb("shell", "uiautomator", "dump", LAYOUT)
                view = ET.fromstring(adb("shell", "cat", LAYOUT))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ET.ParseError):
                time.sleep(1)
                continue
            desired = resource_id if last_seen == CAMERA else ALLOW_WHILE_USING
            node = next(
                (
                    item
                    for item in view.iter("node")
                    if item.get("resource-id") == f"{last_seen}:id/{desired}"
                    and item.get("enabled") == "true"
                ),
                None,
            )
            if node is not None:
                bounds = BOUNDS.fullmatch(node.get("bounds", ""))
                if bounds:
                    left, top, right, bottom = map(int, bounds.groups())
                    adb("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
                    if last_seen == CAMERA:
                        print(f"Native AOSP camera: tapped {desired}")
                        return
        time.sleep(1)
    raise SystemExit(
        f"Camera control {resource_id} unavailable after {timeout}s (foreground: {last_seen})"
    )


if __name__ == "__main__":
    resolved = adb("shell", "cmd", "package", "resolve-activity", "--brief", "-a", CAPTURE_INTENT)
    if not any(line.startswith(f"{CAMERA}/") for line in resolved.splitlines()):
        raise SystemExit(f"Expected the AOSP camera activity, got {resolved}")
    tap_control("shutter_button", timeout=600)
    tap_control("done_button", timeout=45)
