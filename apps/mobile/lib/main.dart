import 'package:material_ui/material_ui.dart';

import 'mobile_runtime.dart';
import 'ui/coinpup_app.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(CoinpupApp(adapter: createMobileAdapter()));
}
