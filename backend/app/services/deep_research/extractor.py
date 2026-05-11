from __future__ import annotations

import re

from app.services.deep_research.ledger import EvidenceLedger
from app.services.deep_research.schemas import ClaimRecord, ClaimStatus, SourceRecord


_FETCH_ENVELOPE_PATTERNS = (
    re.compile(
        r"^📄\s*\*\*(?:Fetched|Firecrawl|Xcrawl|Scraped|Crawled) content from:.*?\*\*\s*",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:Fetched|Firecrawl|Xcrawl|Scraped|Crawled) content from:\s*\S+\s*", re.IGNORECASE),
)
_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*]\([^)]*\)")
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]{0,240})]\([^)]*\)")
_FOOTER_MARKERS = (
    "一键已读",
    "系统通知",
    "登录 / 注册",
    "copyright ©",
    "copyright",
)
_ARTICLE_BODY_MARKERS = (
    "# 引言",
    "## 引言",
    "### 引言",
    "# introduction",
    "## introduction",
    "### introduction",
)
_BOILERPLATE_TERMS = {
    "login",
    "subscribe",
    "cookie",
    "navigation",
    "menu",
    "search",
    "contact",
    "privacy policy",
    "terms of use",
    "forgot password",
    "会员登录",
    "快速登录",
    "忘记密码",
    "操作指引",
    "登录中",
    "身份核验登录",
    "密码",
    "执业证号",
    "统一社会信用代码",
    "大写锁定",
    "扫码登录",
    "会员须知",
    "申请入口",
    "申请实习证",
    "网上投稿",
    "切换新版",
    "协会介绍",
    "行业党建",
    "行业资讯",
    "律师文化",
    "会员服务",
    "研究成果",
    "业务指引",
    "专业委信息",
    "要闻",
    "立法动态",
    "当前位置",
    "首页",
    "业务研究大厅",
    "专业委员会",
    "专业论文",
    "研究动态",
    "专业委通知",
    "案例评析",
    "法讯",
    "律师文库",
}
_MATERIAL_TERMS = {
    "adoption",
    "market",
    "growth",
    "risk",
    "regulatory",
    "compliance",
    "issuer",
    "custody",
    "liquidity",
    "tokenized",
    "rwa",
    "asset",
    "fund",
    "treasury",
    "collateral",
    "institutional",
    "采用",
    "市场",
    "增长",
    "风险",
    "监管",
    "合规",
    "发行",
    "资产",
    "代币化",
    "国债",
    "流动性",
    "托管",
    "机构",
}


def extract_claims_from_source(
    ledger: EvidenceLedger,
    source: SourceRecord,
    *,
    max_claims: int = 2,
    max_claim_chars: int = 320,
) -> list[ClaimRecord]:
    """Extract bounded material claims from a fetched source.

    The Deep Research report must never treat search snippets, fetch envelopes,
    page chrome, or entire fetched pages as claims. This extractor keeps only
    short source-bound sentences that can be cited back to the fetched source.
    """
    claims: list[ClaimRecord] = []
    for sentence in _candidate_sentences(source.content):
        claim_text = _bounded_claim(sentence, max_claim_chars=max_claim_chars)
        if not claim_text:
            continue
        claims.append(
            ledger.add_claim(
                text=claim_text,
                status=ClaimStatus.VERIFIED,
                source_ids=[source.source_id],
                evidence=claim_text,
            )
        )
        if len(claims) >= max_claims:
            break

    if claims:
        return claims

    return []


def clean_fetched_text(content: str) -> str:
    """Remove tool wrappers and obvious page chrome before claim extraction."""
    cleaned_lines: list[str] = []
    for raw_line in (content or "").splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        line = _strip_tool_wrappers(line)
        line = _MARKDOWN_IMAGE_PATTERN.sub(" ", line)
        line = _MARKDOWN_LINK_PATTERN.sub(r"\1", line)
        line = _strip_site_footer(line)
        line = " ".join(line.split())
        if not line:
            continue
        lowered = line.casefold()
        if _looks_like_boilerplate(lowered):
            continue
        if lowered.startswith(("title:", "url:", "source:", "published:", "retrieved:")):
            continue
        cleaned_lines.append(line)
    return " ".join(cleaned_lines)


def _strip_tool_wrappers(line: str) -> str:
    cleaned = line
    for pattern in _FETCH_ENVELOPE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.strip()


def _strip_site_footer(line: str) -> str:
    lowered = line.casefold()
    positions = [idx for marker in _FOOTER_MARKERS if (idx := lowered.find(marker.casefold())) >= 0]
    if not positions:
        return line
    return line[: min(positions)].strip()


def _candidate_sentences(content: str) -> list[str]:
    text = clean_fetched_text(content)
    text = _drop_article_preamble(text)
    pieces = re.split(r"(?<=[。！？.!?])\s+", text)
    candidates: list[str] = []
    for piece in pieces:
        normalized = " ".join(piece.strip().split())
        if not _is_material_sentence(normalized):
            continue
        candidates.append(normalized)
    return candidates


def _drop_article_preamble(text: str) -> str:
    lowered = text.casefold()
    for marker in _ARTICLE_BODY_MARKERS:
        idx = lowered.find(marker.casefold())
        if 0 <= idx <= 1500:
            return text[idx + len(marker):].lstrip(" #:：-—").strip()
    return text


def _bounded_claim(sentence: str, *, max_claim_chars: int) -> str:
    if not sentence:
        return ""
    if len(sentence) <= max_claim_chars:
        return sentence

    cut = sentence[:max_claim_chars].rstrip()
    boundary = max(cut.rfind(". "), cut.rfind("。"), cut.rfind("; "), cut.rfind("；"))
    if boundary >= 80:
        return cut[: boundary + 1].rstrip()
    return cut.rstrip(" ,;，；") + "..."


def _is_material_sentence(sentence: str) -> bool:
    if len(sentence) < 40:
        return False
    lowered = sentence.casefold()
    if _looks_like_boilerplate(lowered):
        return False
    if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", sentence):
        return False
    return any(term in lowered for term in _MATERIAL_TERMS) or bool(re.search(r"\d", sentence))


def _looks_like_boilerplate(lowered_line: str) -> bool:
    hits = sum(1 for term in _BOILERPLATE_TERMS if term in lowered_line)
    if hits >= 2:
        return True
    if "当前位置" in lowered_line and (">>" in lowered_line or "首页" in lowered_line):
        return True
    if lowered_line.count("|") >= 4:
        return True
    words = lowered_line.split()
    return len(words) <= 12 and hits >= 1
