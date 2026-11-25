import 'dart:io';

import 'package:material_ui/material_ui.dart';
import 'package:image_picker/image_picker.dart';

import 'mobile_runtime.dart';
import 'ui/coinpup_app.dart';
import 'ui/mobile_adapter.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  final camera = ImagePicker();
  runApp(
    CoinpupApp(
      adapter: createMobileAdapter(
        retrieveCameraReceipt: Platform.isAndroid
            ? () async {
                final result = await camera.retrieveLostData();
                if (result.isEmpty) return null;
                final files = result.files;
                if (result.exception != null ||
                    result.type != RetrieveType.image ||
                    files == null ||
                    files.length != 1) {
                  throw const MobileFailure(MobileErrorCode.photoRecovery);
                }
                return files.single.path;
              }
            : null,
      ),
      captureReceipt: () async => (await camera.pickImage(
        source: ImageSource.camera,
        requestFullMetadata: false,
      ))?.path,
    ),
  );
}
