"""
Entity validation, PII detection, and type validation for graph_memory.
"""

import re
import unicodedata

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
    "Mnemograph", "Agent Zero", "LiteLLM",
})

# Entity type reference map (Rec #10)
ENTITY_TYPE_REFERENCE = {
    "docker": "technology",
    "kubernetes": "technology",
    "k8s": "technology",
    "python": "language",
    "redis": "technology",
    "sqlite": "technology",
    "agent zero": "project",
    "ollama": "tool",
    "linux": "technology",
    "nginx": "technology",
    "faiss": "tool",
    "git": "tool",
    "github": "tool",
    "docker": "technology",
    "redis": "technology",
    "postgres": "technology",
    "neo4j": "technology",
    "flask": "framework",
    "fastapi": "framework",
    "asyncio": "framework",
    "mnemograph": "project",
    "litellm": "tool",
}

_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in VALIDATION_RULES["forbidden_patterns"]]
_EMAIL_RE = re.compile(PII_DETECTION["email_pattern"])
_API_KEY_RES = [re.compile(p) for p in PII_DETECTION["api_key_patterns"]]
_TOKEN_RE = re.compile(PII_DETECTION["token_patterns"][0])
_LETTER_RE = re.compile(r"[a-zA-Z]")


def normalize_name(raw: str) -> str:
    if not raw:
        return ""
    normalized = unicodedata.normalize("NFKC", str(raw)).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def validate_entity_name(raw: str) -> tuple[bool, str]:
    name = normalize_name(raw)
    if not name:
        return False, "empty"
    if len(name) < VALIDATION_RULES["min_length"]:
        return False, "too_short"
    if len(name) > VALIDATION_RULES["max_length"]:
        return False, "too_long"
    if name.upper() in ALLOWED_TECHNICAL_TERMS:
        return True, "ok"
    if VALIDATION_RULES["require_letter"] and not _LETTER_RE.search(name):
        return False, "no_letter"
    for prefix in VALIDATION_RULES["forbidden_prefixes"]:
        if name.lower().startswith(prefix.lower()):
            return False, f"forbidden_prefix:{prefix}"
    for pat in _FORBIDDEN_RE:
        if pat.search(name):
            return False, "forbidden_pattern"
    if VALIDATION_RULES["require_proper_noun"]:
        has_upper = any(c.isupper() for c in name)
        is_multiword = len(name.split()) >= 2
        if not has_upper and not is_multiword:
            return False, "not_proper_noun"
    return True, "ok"


def detect_pii(text: str) -> list[str]:
    findings = []
    if not text:
        return findings
    if _EMAIL_RE.search(text):
        findings.append("email")
    for pat in _API_KEY_RES:
        if pat.search(text):
            findings.append("api_key")
            break
    for match in _TOKEN_RE.finditer(text):
        token = match.group()
        if token.upper() not in ALLOWED_TECHNICAL_TERMS:
            if not any(c.islower() and c.isalpha() for c in token[:8]):
                findings.append("token")
                break
    return findings


def is_valid_entity(raw_name: str, raw_type: str = "", confidence: float = 0.5) -> bool:
    if confidence < VALIDATION_RULES["min_confidence"]:
        return False
    valid, _ = validate_entity_name(raw_name)
    if not valid:
        return False
    if detect_pii(raw_name):
        return False
    return True


def validate_entity_type(name: str, proposed_type: str) -> tuple[str, bool]:
    """Validate entity type against known reference.
    Returns (corrected_type, was_corrected).
    """
    canonical = name.lower()
    for known_name, known_type in ENTITY_TYPE_REFERENCE.items():
        if canonical == known_name.lower():
            if proposed_type != known_type:
                return known_type, True
            return proposed_type, False
    return proposed_type, False


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
