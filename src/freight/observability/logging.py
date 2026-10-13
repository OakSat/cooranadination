"""Structured JSON logs with PII redaction.

Raw emails stay in S3; logs carry ids and decisions. As a second line of defence, anything that
looks like an email address, IBAN or phone number is masked before a log line is written.
"""

import logging
import re

from aws_lambda_powertools import Logger
from aws_lambda_powertools.logging.formatter import LambdaPowertoolsFormatter

_PATTERNS = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}\b"), "<iban>"),
    # International format only, so dates and ids ("2026-10-13") are left alone.
    (re.compile(r"\+\d[\d\s/-]{8,}\d"), "<phone>"),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(LambdaPowertoolsFormatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(logger: Logger) -> None:
    from aws_lambda_powertools.logging.utils import copy_config_to_registered_loggers

    for handler in logger.handlers:
        handler.setFormatter(RedactingFormatter())
    # Our modules log with the stdlib; give them the same JSON + redaction.
    copy_config_to_registered_loggers(source_logger=logger, include={"freight"})
    logging.getLogger("freight").setLevel(logging.INFO)
