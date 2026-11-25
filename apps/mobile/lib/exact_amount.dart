/// An exact quantity in the full identity and precision of one asset.
final class ExactAmount {
  const ExactAmount._(this.assetId, this.scale, this.units);

  factory ExactAmount.parse(String text, String assetId, int scale) {
    if (assetId.isEmpty || scale < 0 || scale > 18) {
      throw const FormatException('Invalid asset definition');
    }
    final match = _quantity.firstMatch(text);
    if (match == null || match.end != text.length) {
      throw const FormatException('Invalid decimal amount');
    }
    final integer = match.group(2)!;
    final fraction = match.group(3) ?? '';
    if (fraction.length > scale) {
      throw const FormatException('Amount exceeds the asset precision');
    }
    final magnitude = BigInt.parse(integer + fraction.padRight(scale, '0'));
    return ExactAmount._(
      assetId,
      scale,
      match.group(1)!.isEmpty ? magnitude : -magnitude,
    );
  }

  static final _quantity = RegExp(
    r'^(-?)(0|[1-9][0-9]{0,19})(?:\.([0-9]{1,18}))?$',
  );

  final String assetId;
  final int scale;
  final BigInt units;

  ExactAmount operator +(ExactAmount other) {
    if (assetId != other.assetId || scale != other.scale) {
      throw ArgumentError('Amounts use different asset definitions');
    }
    return ExactAmount._(assetId, scale, units + other.units);
  }

  String toDecimal() {
    final sign = units.isNegative ? '-' : '';
    final digits = units.abs().toString().padLeft(scale + 1, '0');
    if (scale == 0) return '$sign$digits';
    final split = digits.length - scale;
    return '$sign${digits.substring(0, split)}.${digits.substring(split)}';
  }
}
