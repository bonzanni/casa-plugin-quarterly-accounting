# tests/test_job_progress.py
"""The progress summary: the words the job reports carry no machinery."""
from tests._base import StoreCase


class Progress(StoreCase):
    def test_the_summary_carries_no_machinery_words(self):
        import loop
        import views
        for unit, text in loop.WORDS.items():
            for word in views.FORBIDDEN:
                self.assertNotIn(word, text, unit)
