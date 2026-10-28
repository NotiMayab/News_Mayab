"""Deduplicación exacta y casi-exacta mediante Shingling de palabras (k=3), Jaccard y SimHash 64-bit."""

from __future__ import annotations

import hashlib
import re
import unicodedata

SPANISH_STOPWORDS = frozenset(
    {
        "el", "la", "los", "las", "un", "una", "unos", "unas", "al", "del",
        "lo", "le", "les", "se", "su", "sus", "mi", "tu", "es", "son", "fue",
        "era", "ser", "estar", "esta", "este", "estos", "estas", "ese", "esa",
        "esos", "esas", "aquel", "aquella", "en", "de", "por", "para", "con",
        "sin", "sobre", "entre", "hacia", "hasta", "desde", "como", "mas",
        "pero", "sino", "o", "u", "y", "e", "ni", "que", "quien", "cual",
        "cuando", "donde", "ante", "bajo", "cabe", "contra", "segun", "tras",
        "durante", "mediante", "todo", "toda", "todos", "todas", "otro", "otra",
        "otros", "otras", "mismo", "misma", "ya", "no", "si", "tambien", "muy",
        "ha", "han", "habia", "hubo", "hay", "puede", "pueden", "asi", "nos",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")

# Umbral inicial definido en Fase A (Sección 2.1): J(A, B) >= 0.82 -> cable/republicación casi-exacta
DEFAULT_NEAR_DUPLICATE_JACCARD_THRESHOLD = 0.82
DEFAULT_SIMHASH_MAX_HAMMING_DISTANCE = 3


def fold_spanish_text(text: str) -> str:
    """Convierte a minúsculas y elimina signos diacríticos (acentos/diéresis) para matching robusto."""
    nfd = unicodedata.normalize("NFD", text.lower())
    without_accents = "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", without_accents)


def tokenize_content_words(text: str, remove_stopwords: bool = True) -> list[str]:
    """Extrae tokens alfanuméricos normalizados excluyendo stopwords vacías."""
    folded = fold_spanish_text(text)
    tokens = _TOKEN_RE.findall(folded)
    if not remove_stopwords:
        return tokens
    return [tok for tok in tokens if tok not in SPANISH_STOPWORDS]


def build_word_shingles(text: str, k: int = 3) -> set[str]:
    """Construye el conjunto de k-shingles de palabras significativas."""
    tokens = tokenize_content_words(text, remove_stopwords=True)
    if not tokens:
        return set()
    if len(tokens) < k:
        return {" ".join(tokens)}
    return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def jaccard_similarity(shingles_a: set[str], shingles_b: set[str]) -> float:
    """Calcula el índice de similitud de Jaccard entre dos conjuntos de shingles."""
    if not shingles_a or not shingles_b:
        return 0.0
    intersection_size = len(shingles_a & shingles_b)
    if intersection_size == 0:
        return 0.0
    union_size = len(shingles_a | shingles_b)
    return intersection_size / union_size


def _hash64(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=False)


def compute_simhash_hex(text: str, k: int = 3) -> str:
    """Calcula la firma SimHash de 64 bits (16 caracteres hexadecimales) sobre k-shingles."""
    shingles = build_word_shingles(text, k=k)
    if not shingles:
        return "0000000000000000"

    bit_weights = [0] * 64
    for shingle in sorted(shingles):
        h = _hash64(shingle)
        for bit_idx in range(64):
            if (h >> bit_idx) & 1:
                bit_weights[bit_idx] += 1
            else:
                bit_weights[bit_idx] -= 1

    fingerprint = 0
    for bit_idx, weight in enumerate(bit_weights):
        if weight > 0:
            fingerprint |= 1 << bit_idx

    return f"{fingerprint:016x}"


def simhash_hamming_distance(hex_a: str, hex_b: str) -> int:
    """Calcula la distancia de Hamming (número de bits distintos) entre dos firmas SimHash hex de 64 bits."""
    val_a = int(hex_a, 16)
    val_b = int(hex_b, 16)
    return (val_a ^ val_b).bit_count()


def is_near_duplicate(
    text_a: str,
    text_b: str,
    jaccard_threshold: float = DEFAULT_NEAR_DUPLICATE_JACCARD_THRESHOLD,
    max_hamming_bits: int = DEFAULT_SIMHASH_MAX_HAMMING_DISTANCE,
) -> tuple[bool, float, int]:
    """Determina si dos artículos son republicaciones casi-exactas del mismo cable.

    Devuelve (es_duplicado, jaccard_score, hamming_distance).
    """
    shingles_a = build_word_shingles(text_a, k=3)
    shingles_b = build_word_shingles(text_b, k=3)
    j_score = jaccard_similarity(shingles_a, shingles_b)

    sim_a = compute_simhash_hex(text_a, k=3)
    sim_b = compute_simhash_hex(text_b, k=3)
    h_dist = simhash_hamming_distance(sim_a, sim_b)

    duplicate = (j_score >= jaccard_threshold) or (
        h_dist <= max_hamming_bits and j_score >= 0.65
    )
    return duplicate, j_score, h_dist
