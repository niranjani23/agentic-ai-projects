"""Topology detection: which *kind* of system is this?

This decides which lens packs run. A RAG lens must never fire on a repo that has
no RAG system in it; inventing retrieval findings for a non-RAG codebase is the
fastest way to lose a reader's trust, so applicability is evidence-based and the
evidence is printed in the review.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .snapshot import Snapshot

MAX_HITS_PER_STAGE = 5


@dataclass
class Hit:
    file: str
    line: int
    quote: str


@dataclass
class Topology:
    rag_applicable: bool = False
    rag_stages: dict[str, list[Hit]] = field(default_factory=dict)
    rag_reason: str = ""
    agentic_applicable: bool = False
    agentic_signals: list[Hit] = field(default_factory=list)
    agentic_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_I = re.IGNORECASE
RAG_STAGE_PATTERNS: dict[str, re.Pattern[str]] = {
    "ingest": re.compile(
        r"TextSplitter|chunk_size|chunk_overlap|split_documents|DocumentLoader|PyPDFLoader|"
        r"def\s+chunk_?\w*\(|\bchunk(?:ing|er)\b|unstructured\.partition",
        _I,
    ),
    "embed": re.compile(
        r"embeddings?\.create|SentenceTransformer|voyageai|OpenAIEmbeddings|HuggingFaceEmbeddings|"
        r"embed_(?:documents|query)|text-embedding|titan[\w\-.]*embed|all-MiniLM|\bget_embedding|\.encode\(.*normalize",
        _I,
    ),
    "store": re.compile(
        r"pgvector|\bfaiss\b|chromadb|Chroma\(|pinecone|qdrant|weaviate|opensearch|milvus|lancedb|"
        r"VectorStore|vector_store|<=>|ivfflat|\bhnsw\b|vector\(\d+\)",
        _I,
    ),
    "retrieve": re.compile(
        r"similarity_search|as_retriever|\bretriever\b|\btop_k\b|\bbm25\b|reciprocal[_ ]rank|hybrid_search|\bretrieve\(",
        _I,
    ),
    "rerank": re.compile(r"\brerank\w*|CrossEncoder|cross-encoder|bge-reranker", _I),
    "eval": re.compile(r"\bragas\b|recall@|\bmrr\b|\bndcg\b|hit_rate|golden[_ ]set|ground_truth|faithfulness|context_precision|deepeval|trulens", _I),
}

AGENTIC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.messages\.create\(|chat\.completions\.create\(|\.responses\.create\(|generate_content\(", _I),
    re.compile(r"stop_reason|tool_use|tool_calls|function_call", _I),
    re.compile(r"\bStateGraph\b|\bAgentExecutor\b|\bcrewai\b|\bautogen\b|\bmastra\b|\bAgent\(", _I),
]


def _is_comment(line: str) -> bool:
    s = line.lstrip()
    return s.startswith(("#", "//", "*", "/*", "<!--"))


def detect_topology(snap: Snapshot) -> Topology:
    topo = Topology()
    stages: dict[str, list[Hit]] = {k: [] for k in RAG_STAGE_PATTERNS}

    for path in snap.code_paths():
        for i, line in enumerate(snap.lines(path), start=1):
            if _is_comment(line) or len(line) > 400:
                continue
            for stage, pat in RAG_STAGE_PATTERNS.items():
                if len(stages[stage]) < MAX_HITS_PER_STAGE and pat.search(line):
                    stages[stage].append(Hit(path, i, line.strip()[:140]))
            if len(topo.agentic_signals) < 8 and any(p.search(line) for p in AGENTIC_PATTERNS):
                topo.agentic_signals.append(Hit(path, i, line.strip()[:140]))

    topo.rag_stages = {k: v for k, v in stages.items() if v}
    present = set(topo.rag_stages)
    # Retrieval-ish words alone are too generic ("top_k", "retriever" show up everywhere);
    # require the storage/embedding half of the pipeline as well.
    rag = ("embed" in present and ("store" in present or "retrieve" in present)) or (
        "store" in present and "retrieve" in present
    )
    topo.rag_applicable = rag
    if rag:
        topo.rag_reason = "RAG signals found in stages: " + ", ".join(sorted(present))
    else:
        found = ", ".join(sorted(present)) or "none"
        topo.rag_reason = (
            "No RAG pipeline detected (needs embedding plus a vector store or retriever). "
            f"Weak signals: {found}."
        )

    topo.agentic_applicable = bool(topo.agentic_signals)
    topo.agentic_reason = (
        f"{len(topo.agentic_signals)} LLM-call/tool-loop signal(s) found"
        if topo.agentic_applicable
        else "No LLM client calls or agent frameworks detected."
    )
    return topo
