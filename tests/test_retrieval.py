from types import SimpleNamespace

from app.rag.retrieval import cosine_similarity_score, hybrid_rerank_chunks


def test_cosine_similarity_identical_normalized():
    v = [0.6, 0.8]
    assert abs(cosine_similarity_score(v, v) - 1.0) < 1e-9


def test_cosine_similarity_orthogonal():
    assert cosine_similarity_score([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_hybrid_rerank_prefers_higher_semantic_similarity():
    q_emb = [1.0, 0.0]
    low = SimpleNamespace(
        id=1,
        review_id=10,
        summary="a",
        embedding=[0.5, 0.8660254],  # ~60° from query → lower cosine with [1,0]
        review=SimpleNamespace(product_name="x", body="y"),
    )
    high = SimpleNamespace(
        id=2,
        review_id=11,
        summary="b",
        embedding=[1.0, 0.0],
        review=SimpleNamespace(product_name="x", body="y"),
    )
    ranked = hybrid_rerank_chunks(
        query_text="query",
        query_embedding=q_emb,
        chunks=[low, high],
        vector_rank_by_review={10: 0, 11: 1},
        keyword_rank_by_review={10: 0, 11: 1},
        candidate_k=10,
        top_k=2,
    )
    assert ranked[0][0].review_id == 11
    assert ranked[0][2]["semantic_similarity"] >= ranked[1][2]["semantic_similarity"]
