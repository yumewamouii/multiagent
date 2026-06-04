"""Тесты ingest DocDoc без PostgreSQL."""

from unittest.mock import MagicMock

from app.services.docdoc_ingest import _BatchCommitter, iter_reviews_deduped


def test_iter_reviews_deduped_merges_sources():
    crawl = {
        "services": [
            {
                "ok": True,
                "reviews": [
                    {"review_id": 1, "text": "a"},
                    {"review_id": 2, "text": "b"},
                ],
            }
        ],
        "reviews": [
            {"review_id": 1, "text": "a-dup"},
            {"review_id": 3, "text": "c"},
        ],
    }
    out = list(iter_reviews_deduped(crawl))
    assert len(out) == 3
    assert {r["review_id"] for r in out} == {1, 2, 3}


def test_iter_reviews_skips_invalid_ids():
    crawl = {
        "services": [{"ok": True, "reviews": [{"review_id": 0}, {"review_id": None}, {"review_id": 5, "text": "x"}]}],
        "reviews": [],
    }
    out = list(iter_reviews_deduped(crawl))
    assert len(out) == 1
    assert out[0]["review_id"] == 5


def test_batch_committer_flushes_on_threshold():
    db = MagicMock()
    batch = _BatchCommitter(db, 3, label="test")
    batch.bump()
    batch.bump()
    assert db.commit.call_count == 0
    batch.bump()
    assert db.commit.call_count == 1
    assert batch.commits == 1
    batch.bump()
    batch.flush()
    assert db.commit.call_count == 2
