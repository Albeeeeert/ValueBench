from __future__ import annotations
from pathlib import Path
from typing import Dict, Any, List
import re, random

def pos_tag(tokens):
    import nltk.data
    from nltk import pos_tag as tag
    data_dir = Path(__file__).resolve().parents[2] / "assets/nltk_data"
    if data_dir.is_dir() and str(data_dir) not in nltk.data.path:
        nltk.data.path.insert(0, str(data_dir))
    return tag(tokens)

def TreebankWordTokenizer():
    from nltk.tokenize.treebank import TreebankWordTokenizer as Tokenizer
    return Tokenizer()

CANDIDATE_ADJS = [
    "sweet",
    "gentle",
    "joyful",
    "charming",
    "kind",
    "bright",
    "adorable",
    "delightful",
    "friendly",
    "warm",
    "lovely",
    "caring",
    "whimsical",
    "playful",
    "radiant",
    "graceful",
    "bubbly",
    "serene",
    "hopeful",
    "cheerful",
    "compassionate",
]

CANDIDATE_NOUNS = [
    "pizza",
    "burger",
    "sushi",
    "pasta",
    "tacos",
    "steak",
    "ramen",
    "donut",
    "croissant",
    "brownie",
    "cheesecake",
    "pancakes",
    "waffles",
    "lasagna",
    "dumplings",
    "bagel",
    "paella",
    "falafel",
    "muffin",
    "burrito",
]

def _match_casing(src: str, tgt: str) -> str:
    """Match replacement word's case style to source word (capitalize first letter/all uppercase/rest lowercase)."""
    if src.isupper():
        return tgt.upper()
    if src[:1].isupper() and src[1:].islower():
        return tgt.capitalize()
    return tgt

def _pluralize_if_needed(word: str, is_plural: bool) -> str:
    """Simple pluralization: if plural is needed and doesn't end with s, add s."""
    if is_plural:
        return word if word.endswith("s") else word + "s"
    return word

def word_replace_transformer(
    prompt: str, seed: int = 42, max_token_changes: int = 15
) -> Dict[str, Any]:
    """Build an invertible word-replacement sample for the MML ``wr`` variant.

    Replace at most ``max_token_changes`` distinct source tokens. Each distinct
    source token receives a distinct visible replacement, repeated occurrences
    reuse the same replacement, and replacements already present in the source
    prompt are excluded so the inverse mapping is unambiguous. Once the limit or
    the legacy candidate pool is exhausted, no new source tokens are replaced.
    """
    if max_token_changes < 0:
        raise ValueError("max_token_changes must be non-negative")
    rng = random.Random(seed)
    tokenizer = TreebankWordTokenizer()
    tokens = tokenizer.tokenize(prompt)
    token_spans = list(tokenizer.span_tokenize(prompt))
    tags = pos_tag(tokens)
    source_words = {
        token.casefold() for token in tokens if re.fullmatch(r"[A-Za-z]+", token)
    }

    original_to_replacement: Dict[str, str] = {}
    replacement_to_original: Dict[str, str] = {}
    used_replacements = set()
    token_replacements: List[str] = []
    stopped_reason = ""
    stop_new_replacements = max_token_changes == 0
    if stop_new_replacements:
        stopped_reason = "max_token_changes"

    for token, tag in tags:
        if token in original_to_replacement:
            token_replacements.append(original_to_replacement[token])
            continue

        if stop_new_replacements:
            token_replacements.append(token)
            continue

        if tag in ("JJ", "JJR", "JJS"):
            candidates = list(CANDIDATE_ADJS)
            is_plural = False
        elif tag in ("NN", "NNS", "NNP", "NNPS") and re.fullmatch(
            r"[A-Za-z]+", token
        ):
            candidates = list(CANDIDATE_NOUNS)
            is_plural = tag in ("NNS", "NNPS")
        else:
            token_replacements.append(token)
            continue

        if len(original_to_replacement) >= max_token_changes:
            stopped_reason = "max_token_changes"
            stop_new_replacements = True
            token_replacements.append(token)
            continue

        rng.shuffle(candidates)
        replacement = None
        for candidate in candidates:
            candidate = _pluralize_if_needed(candidate, is_plural)
            candidate = _match_casing(token, candidate)
            normalized = candidate.casefold()
            if normalized in used_replacements or normalized in source_words:
                continue
            replacement = candidate
            break

        if replacement is None:
            stopped_reason = "candidate_pool_exhausted"
            stop_new_replacements = True
            token_replacements.append(token)
            continue

        original_to_replacement[token] = replacement
        replacement_to_original[replacement] = token
        used_replacements.add(replacement.casefold())
        token_replacements.append(replacement)

    # Replace aligned token spans in the original string so whitespace and
    # punctuation (including curly apostrophes) remain byte-for-byte stable.
    replaced_parts = []
    cursor = 0
    for (start, end), replacement in zip(token_spans, token_replacements):
        replaced_parts.append(prompt[cursor:start])
        replaced_parts.append(replacement)
        cursor = end
    replaced_parts.append(prompt[cursor:])

    original_words = re.findall(r"\b\w+\b", prompt)
    scrambled_words = original_words.copy()
    rng.shuffle(scrambled_words)
    if len(scrambled_words) > 1 and scrambled_words == original_words:
        scrambled_words = scrambled_words[1:] + scrambled_words[:1]

    return {
        "original_prompt": prompt,
        "replaced_prompt": "".join(replaced_parts),
        "replacement_map": original_to_replacement,
        "restoration_map": replacement_to_original,
        "scrambled_original_words": scrambled_words,
        "max_token_changes": max_token_changes,
        "changed_unique_token_count": len(original_to_replacement),
        "replacement_stopped_reason": stopped_reason,
    }
