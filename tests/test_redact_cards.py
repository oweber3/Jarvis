"""Card numbers are scrubbed; dates, times and other long digit runs that are not card numbers are kept."""
import pytest

from jarvis.utils.redact import redact, scrub_secrets


@pytest.mark.parametrize('card', ['4111 1111 1111 1111', '5555-5555-5555-4444', '378282246310005',
                                  '6011111111111117'])
def test_valid_card_numbers_are_redacted(card):
    for scrub in (redact, scrub_secrets):
        text = scrub(f'paid with {card} yesterday')
        assert '[REDACTED_CARD]' in text and card not in text


@pytest.mark.parametrize('text', [
    r'C:\Users\you\Desktop\Screenshot 2026-10-05 141207.png',
    'IMG 2025-03-14 093012.jpg',
    'order 1234567890123 shipped',
])
def test_digit_runs_that_fail_the_card_checksum_are_kept(text):
    assert scrub_secrets(text) == text
    assert redact(text) == text
