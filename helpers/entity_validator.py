"""
Entity validation and PII detection for _graph_memory.
Prevents garbage entities (filenames, URLs, tokens) from entering the graph.
"""

import re
import unicodedata

# ─── Validation Rules (Design Spec Section 7) ────────────────

VALIDATION_RULES = {
    "min_length": 2,
    "max_length": 100,
    "require_letter": True,
    "forbidden_prefixes": ["/", "#", ".", "{", "<", "http", "api/"],
    "forbidden_patterns": [
        r"\.py$", r"\.json$", r"\.env$", r"\.yaml$", r"\.md$",
        r"^/api/", r"^/health", r"^/v1/", r"^#",
        r"\d{4,}",
    ],
    "min_confidence": 0.3,
    "require_proper_noun": True,
}

PII_DETECTION = {
    "email_pattern": r"[\w.-]+@[\w.-]+\.\w+",
    "api_key_patterns": [r"sk-", r"ghp_", r"hf_", r"nvapi-"],
    "token_patterns": [r"[A-Za-z0-9]{32,}"],
}

# Technical terms allowed even though they're uppercase or short
ALLOWED_TECHNICAL_TERMS = frozenset({
    "API", "FAISS", "VLLM", "Ollama", "Docker", "Kubernetes", "K8s",
    "Redis", "SQLite", "Python", "Node", "React", "Vue", "Rust",
    "CUDA", "GPU", "CPU", "RAM", "SSD", "NVMe", "HDD",
    "REST", "GraphQL", "gRPC", "SSH", "TLS", "SSL", "DNS",
    "AWS", "GCP", "Nginx", "Apache", "Linux", "MacOS",
    "JSON", "YAML", "TOML", "HTML", "CSS", "SQL", "TDD",
    "CI", "CD", "PR", "LLM", "GPT", "RAG", "NLP",
    "Elastic", "Kibana", "Logstash", "Vector", "Prometheus",
    "NCCL", "RoCE", "InfiniBand", "PCIE", "MIG",
    "MoE", "GGUF", "AWQ", "GPTQ", "FP8", "BF16", "FP16",
})

# Compile patterns once
_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in VALIDATION_RULES["forbidden_patterns"]]
_EMAIL_RE = re.compile(PII_DETECTION["email_pattern"])
_API_KEY_RES = [re.compile(p) for p in PII_DETECTION["api_key_patterns"]]
_TOKEN_RE = re.compile(PII_DETECTION["token_patterns"][0])
_LETTER_RE = re.compile(r"[a-zA-Z]")


def normalize_name(raw: str) -> str:
    """NFKC normalization + strip. Preserves case for proper nouns."""
    if not raw:
        return ""
    normalized = unicodedata.normalize("NFKC", str(raw)).strip()
    # Collapse internal whitespace
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def validate_entity_name(raw: str) -> tuple[bool, str]:
    """
    Validate an entity name against all rules.
    Returns (is_valid, reason).
    """
    name = normalize_name(raw)
    if not name:
        return False, "empty"

    # Length checks
    if len(name) < VALIDATION_RULES["min_length"]:
        return False, "too_short"
    if len(name) > VALIDATION_RULES["max_length"]:
        return False, "too_long"

    # Allowlist bypass (technical terms are always valid)
    if name.upper() in ALLOWED_TECHNICAL_TERMS:
        return True, "ok"

    # Require at least one letter
    if VALIDATION_RULES["require_letter"] and not _LETTER_RE.search(name):
        return False, "no_letter"

    # Forbidden prefixes
    for prefix in VALIDATION_RULES["forbidden_prefixes"]:
        if name.lower().startswith(prefix.lower()):
            return False, f"forbidden_prefix:{prefix}"

    # Forbidden patterns
    for pat in _FORBIDDEN_RE:
        if pat.search(name):
            return False, "forbidden_pattern"

    # Proper noun heuristic: must have at least one uppercase letter OR
    # be a multi-word phrase (e.g. "large language model")
    if VALIDATION_RULES["require_proper_noun"]:
        has_upper = any(c.isupper() for c in name)
        is_multiword = len(name.split()) >= 2
        if not has_upper and not is_multiword:
            return False, "not_proper_noun"

    return True, "ok"


def detect_pii(text: str) -> list[str]:
    """
    Detect PII/secrets in text. Returns list of findings.
    """
    findings = []
    if not text:
        return findings

    if _EMAIL_RE.search(text):
        findings.append("email")
    for pat in _API_KEY_RES:
        if pat.search(text):
            findings.append("api_key")
            break
    # Token check — only flag long alphanumeric runs that look like tokens,
    # not normal words or technical terms
    for match in _TOKEN_RE.finditer(text):
        token = match.group()
        if token.upper() not in ALLOWED_TECHNICAL_TERMS:
            # Avoid false positives from long normal words
            if not any(c.islower() and c.isalpha() for c in token[:8]):
                findings.append("token")
                break

    return findings


def is_valid_entity(raw_name: str, raw_type: str = "", confidence: float = 0.5) -> bool:
    """Quick boolean check for entity validity."""
    if confidence < VALIDATION_RULES["min_confidence"]:
        return False
    valid, _ = validate_entity_name(raw_name)
    if not valid:
        return False
    if detect_pii(raw_name):
        return False
    return True


# Valid entity types and domains for reference
VALID_ENTITY_TYPES = frozenset({
    "person", "organization", "technology", "concept",
    "project", "skill", "location", "tool", "framework", "language",
})

VALID_DOMAINS = frozenset({
    "work", "personal", "platform", "research", "general",
})

VALID_REL_TYPES = frozenset({
    "uses", "depends_on", "runs_on", "related_to", "part_of", "owns",
    "built_with", "alternative_to", "predecessor_of", "competes_with",
})
