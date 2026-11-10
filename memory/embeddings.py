import requests
from config import Config


def get_embedding(text: str) -> list[float]:
    """Get embedding for a single text string."""
    return get_embeddings([text])[0]


def get_embeddings(texts: list[str]) -> list[list[float]]:
    """Get embeddings for a batch of texts. Routes to configured provider."""
    provider = Config.MEMORY_EMBEDDING_PROVIDER

    if provider == "lmstudio":
        return _lmstudio_embeddings(texts)
    elif provider == "openai":
        return _openai_embeddings(texts)
    elif provider == "gemini":
        return _gemini_embeddings(texts)
    else:
        raise ValueError(f"Unknown embedding provider: {provider}")


def _lmstudio_embeddings(texts: list[str]) -> list[list[float]]:
    """LM Studio local embeddings via OpenAI-compatible endpoint."""
    resp = requests.post(
        f"{Config.LM_STUDIO_HOST}/embeddings",
        json={
            "model": Config.MEMORY_EMBEDDING_MODEL,
            "input": texts,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    # Sort by index to guarantee order matches input
    data.sort(key=lambda x: x["index"])
    return [item["embedding"] for item in data]


def _openai_embeddings(texts: list[str]) -> list[list[float]]:
    """OpenAI embeddings API."""
    resp = requests.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {Config.OPENAI_API_KEY}"},
        json={
            "model": Config.MEMORY_EMBEDDING_MODEL,
            "input": texts,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    data.sort(key=lambda x: x["index"])
    return [item["embedding"] for item in data]


def _gemini_embeddings(texts: list[str]) -> list[list[float]]:
    """Google Gemini embeddings (google-genai SDK; reads GEMINI_API_KEY)."""
    from google import genai

    client = genai.Client()
    result = client.models.embed_content(model=Config.MEMORY_EMBEDDING_MODEL, contents=texts)
    return [e.values for e in result.embeddings]
