import 'package:coinpup_mobile/exact_amount.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('preserves full chain identity and 18-place precision', () {
    const token = 'token:fictional-chain:unit';
    final left = ExactAmount.parse(
      '99999999999999999999.999999999999999998',
      token,
      18,
    );
    final unit = ExactAmount.parse('0.000000000000000001', token, 18);
    expect(
      (left + unit).toDecimal(),
      '99999999999999999999.999999999999999999',
    );
    expect(
      () => left + ExactAmount.parse('1', 'token:other:unit', 18),
      throwsArgumentError,
    );
  });

  test(
    'accepts signed decimal strings and rejects rounding or exponent notation',
    () {
      expect(ExactAmount.parse('-10.01', 'USD', 2).toDecimal(), '-10.01');
      expect(ExactAmount.parse('3', 'JPY', 0).toDecimal(), '3');
      expect(ExactAmount.parse('-0', 'USD', 2).toDecimal(), '0.00');
      for (final text in ['0.001', '1e2', ' 10', '1,000.00', '10.00\n']) {
        expect(() => ExactAmount.parse(text, 'USD', 2), throwsFormatException);
      }
    },
  );
}
