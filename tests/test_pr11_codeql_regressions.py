"""Regression cases for the concrete parsing findings reported on PR 11."""

import time

import pytest

from app.cabinet.schemas.news import _sanitize_html_content
from app.database.database import with_db_retry
from app.services.server_status_service import ServerStatusService
from app.utils.rich_admin import _inline_newlines_to_rich
from app.utils.validators import validate_html_structure, validate_html_tags


@pytest.mark.parametrize('end_tag', ['</script>', '</script >', '</script\n>', '</script ignored>'])
def test_script_body_is_removed_before_tag_stripping(end_tag):
    assert _sanitize_html_content(f'<p>before</p><script>alert(1){end_tag}<b>after</b>') == '<p>before</p><b>after</b>'


def test_db_retry_rejects_zero_attempts_at_decoration_time():
    with pytest.raises(ValueError, match='attempts'):
        with_db_retry(attempts=0)


def test_prometheus_escaped_label_without_closing_quote_does_not_backtrack():
    started = time.monotonic()
    assert ServerStatusService()._parse_labels('A="' + '\\\\!' * 10000) == {}
    assert time.monotonic() - started < 2


@pytest.mark.parametrize('validate', [validate_html_structure, validate_html_tags])
def test_unclosed_long_tag_name_is_linear(validate):
    started = time.monotonic()
    validate('<A' + '-' * 100000)
    assert time.monotonic() - started < 2


@pytest.mark.parametrize('tag', ['pre', 'blockquote'])
def test_unclosed_repeated_blocks_are_linear(tag):
    started = time.monotonic()
    _inline_newlines_to_rich(f'<{tag}>' * 10000)
    assert time.monotonic() - started < 2
