"""The plugin's store. Task 7 adds the schema; this file starts with the one
exception type every module raises for an explained refusal."""
from __future__ import annotations


class Refusal(Exception):
    """An expected, explained refusal. The dispatcher renders it as
    `refused: <message>` and never as an error, so the caller reads it as an
    answer (spec §Error handling: explicit, loud, never silent)."""
