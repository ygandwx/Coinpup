#!/usr/bin/env bash
set -euo pipefail

test "$(adb shell getprop ro.build.version.sdk | tr -d '\r')" = 36
grep -Ex 'Pkg.Revision *= *2' "$ANDROID_HOME/system-images/android-36/default/x86_64/source.properties"
"$ANDROID_HOME/emulator/emulator" -version

app_id=dev.coinpup.coinpup_mobile
adb install apps/mobile/build/app/outputs/flutter-apk/app-release.apk
adb uninstall "$app_id"
adb reverse tcp:8000 tcp:8000

python3 -u .github/scripts/t12-android-camera.py &
camera_pid=$!
trap 'kill "$camera_pid" 2>/dev/null || true' EXIT
(
  cd apps/mobile
  flutter test integration_test/android_acceptance_test.dart -d "$ANDROID_SERIAL" --no-pub \
    --dart-define=COINPUP_TEST_URL=http://127.0.0.1:8000 \
    --dart-define="COINPUP_TEST_USERNAME=$E2E_USERNAME" \
    --dart-define="COINPUP_TEST_PASSWORD=$E2E_PASSWORD"
)
wait "$camera_pid"
