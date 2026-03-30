import logging
import torch
from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)
_model = None
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


def get_model():
    global _model
    if _model is None:
        log.info("loading embedding model...")
        _model = SentenceTransformer("intfloat/multilingual-e5-small")
        _model.max_seq_length = 512  # можно увеличить при необходимости
        _model.to(DEVICE)
    return _model


def create_embedding(text: str) -> list[float] | None:
    if not text:
        return None

    try:
        model = get_model()
        text = f"passage: {text}"

        emb = model.encode(text, normalize_embeddings=True)
        return emb.tolist()

    except Exception as e:
        log.warning(f"embedding error: {e}")
        return None