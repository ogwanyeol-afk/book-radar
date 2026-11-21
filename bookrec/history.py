"""시청·검색 기록 파싱과 관심사 추출.

지원 입력
- 구글 테이크아웃 YouTube 시청기록 / 검색기록 (watch-history.json, search-history.json, .html)
- 구글 테이크아웃 내 활동 > 검색 (MyActivity.json, .html)
- 네이버 검색기록 등 붙여넣은 텍스트 (한 줄에 하나)

원문은 메모리에서만 처리하고 저장하지 않는다.
"""
from __future__ import annotations

import html as htmllib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .topics import TOPICS, topic_of


@dataclass
class Entry:
    text: str
    kind: str              # watch | search | visit | text
    source: str            # youtube | google | naver | text
    time: datetime | None = None
    channel: str | None = None


# ---------------------------------------------------------------- 파싱

_PREFIX = [  # (정규식, kind) — 영어/한국어 테이크아웃 문구
    (re.compile(r"^Watched\s+(.+)$", re.S), "watch"),
    (re.compile(r"^Searched for\s+(.+)$", re.S), "search"),
    (re.compile(r"^Visited\s+(.+)$", re.S), "visit"),
    (re.compile(r"^(.+?)\s*을\(를\)\s*시청했습니다\.?$", re.S), "watch"),
    (re.compile(r"^(.+?)\s*을\(를\)\s*검색했습니다\.?$", re.S), "search"),
    (re.compile(r"^(.+?)\s*을\(를\)\s*방문했습니다\.?$", re.S), "visit"),
    (re.compile(r"^(.+?)\s*시청함$", re.S), "watch"),
    (re.compile(r"^(.+?)\s*검색함$", re.S), "search"),
]


def _classify(title: str) -> tuple[str, str]:
    t = title.strip()
    for rx, kind in _PREFIX:
        m = rx.match(t)
        if m:
            return m.group(1).strip(), kind
    return t, "text"


def _ptime(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _source_of(header: str, products: list[str], hint: str) -> str:
    h = (header or "").lower() + " " + " ".join(products or []).lower()
    if "youtube" in h:
        return "youtube"
    if "search" in h or "검색" in h or "google" in h:
        return "google"
    return hint if hint in ("youtube", "google", "naver") else "text"


def _parse_takeout_json(data: list, hint: str) -> list[Entry]:
    out: list[Entry] = []
    for it in data:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        text, kind = _classify(it["title"])
        if text.startswith("http") or "removed" in text.lower() and "video" in text.lower():
            continue  # '삭제된 동영상' 같은 껍데기
        ch = None
        subs = it.get("subtitles") or []
        if subs and isinstance(subs[0], dict):
            ch = subs[0].get("name")
        out.append(Entry(text=text, kind=kind,
                         source=_source_of(it.get("header", ""), it.get("products", []), hint),
                         time=_ptime(it.get("time")), channel=ch))
    return out


_CELL = re.compile(r'<div class="content-cell[^"]*mdl-typography--body-1"[^>]*>(.*?)</div>', re.S)
_DATE_KO = re.compile(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.")
_DATE_EN = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})")
_MON = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def _parse_takeout_html(raw: str, hint: str) -> list[Entry]:
    out: list[Entry] = []
    for cell in _CELL.findall(raw):
        parts = [htmllib.unescape(re.sub(r"<[^>]+>", "", p)).replace("\xa0", " ").strip()
                 for p in re.split(r"<br\s*/?>", cell)]
        parts = [p for p in parts if p]
        if not parts:
            continue
        text, kind = _classify(parts[0])
        when = None
        for p in parts[1:]:
            m = _DATE_KO.search(p)
            if m:
                when = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
                break
            m = _DATE_EN.search(p)
            if m and m[1] in _MON:
                when = datetime(int(m[3]), _MON[m[1]], int(m[2]), tzinfo=timezone.utc)
                break
        ch = parts[1] if kind == "watch" and len(parts) > 2 else None
        out.append(Entry(text=text, kind=kind, source="youtube" if kind == "watch" else (hint or "google"),
                         time=when, channel=ch))
    return out


_NAVER_NOISE = re.compile(r"^(삭제|전체삭제|검색기록|자동저장|최근검색어|\d{2}\.\d{2}\.?|\d{4}\.\d{2}\.\d{2}\.?)$")


def _parse_lines(raw: str, hint: str) -> list[Entry]:
    out = []
    src = hint if hint in ("youtube", "google", "naver") else "text"
    for line in raw.splitlines():
        line = line.strip(" \t-•·*")
        if not line or _NAVER_NOISE.match(line):
            continue
        line = re.sub(r"\s+\d{2}\.\d{2}\.?$", "", line)  # 네이버 '키워드 11.19.' 날짜 꼬리
        text, kind = _classify(line)
        if kind == "text" and src in ("naver", "google"):
            kind = "search"
        out.append(Entry(text=text, kind=kind, source=src))
    return out


def parse_history(raw: str, source: str = "auto") -> list[Entry]:
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw[0] in "[{":
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = data.get("items") or data.get("data") or [data]
            if isinstance(data, list) and data and isinstance(data[0], str):
                return _parse_lines("\n".join(data), source)
            return _parse_takeout_json(data, source)
        except json.JSONDecodeError:
            pass
    if "content-cell" in raw and "<div" in raw:
        return _parse_takeout_html(raw, source)
    return _parse_lines(raw, source)


# ---------------------------------------------------------------- 관심사 추출

_URL = re.compile(r"https?://\S+|www\.\S+")
_PII = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|01[016789][- ]?\d{3,4}[- ]?\d{4}|\d{6}-?[1-4]\d{6}")
_TOKEN = re.compile(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9+#]{1,}")

_JOSA = sorted(["으로부터", "에서부터", "이라는", "에게서", "까지는", "에서는", "으로는", "이라고", "라는",
                "에서", "으로", "에게", "부터", "까지", "처럼", "보다", "하고", "이랑", "이나",
                "하는", "하기", "하면", "했다", "합니다", "해요", "하자", "되는", "된다",
                "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만", "랑"],
               key=len, reverse=True)

STOP = set("""
영상 시청 방법 추천 정리 오늘 진짜 완전 리뷰 브이로그 하는법 이유 총정리 꿀팁 공식 라이브 풀버전 하이라이트
모음 무조건 최고 역대급 충격 사람 우리 그냥 이거 저거 요즘 지금 처음 마지막 이번 다음 정말 너무 가장 모든
시간 하루 일상 ep 편 화 회 부 탄 vlog shorts short official video live full mv teaser trailer the and for with
you your this that from what how why are was 유튜브 youtube 구독 좋아요 알림 채널 클립 쇼츠 뉴스 속보 단독
네이버 구글 검색 사이트 홈페이지 로그인 다운로드 날씨 번역 지도 시간표 뜻 의미 가격 후기 비교 순위
vs 만들기 미치 영향 이것 얼마 언제 어디 누구 하는 있는 없는 같은 대한 위한 통해 관련 이제 이렇게 그래서 근데 그리고 하지만 결국 드디어 바로 직접
""".split())


def _tokens(text: str) -> list[str]:
    text = _PII.sub(" ", _URL.sub(" ", text))
    text = re.sub(r"#\S+", lambda m: " " + m.group(0)[1:] + " ", text)  # 해시태그는 단어로
    out = []
    for tok in _TOKEN.findall(text):
        t = tok.lower()
        if re.match(r"[가-힣]", t):
            for j in _JOSA:
                if t.endswith(j) and len(t) - len(j) >= 2:
                    t = t[: -len(j)]
                    break
        if len(t) < 2 or t in STOP or t.isdigit():
            continue
        out.append(t)
    return out


KIND_WEIGHT = {"like": 2.0, "search": 1.5, "watch": 1.0, "text": 1.0, "visit": 0.4}


@dataclass
class Profile:
    keywords: list[tuple[str, float]]
    topics: list[tuple[str, float, list[str]]]      # (주제, 점수, 대표키워드)
    phrases: list[tuple[str, float]]
    stats: dict = field(default_factory=dict)

    def as_dict(self, top: int = 20) -> dict:
        return {
            "top_keywords": [{"keyword": k, "score": round(s, 2)} for k, s in self.keywords[:top]],
            "top_topics": [{"topic": t, "score": round(s, 2), "keywords": ks}
                           for t, s, ks in self.topics[:8]],
            "top_phrases": [{"phrase": p, "score": round(s, 2)} for p, s in self.phrases[:10]],
            "stats": self.stats,
        }


def build_profile(entries: list[Entry], *, days: int | None = None, max_items: int = 5000,
                  half_life_days: float = 90.0, exclude: list[str] | None = None) -> Profile:
    exclude_l = [e.lower() for e in (exclude or [])]
    timed = [e.time for e in entries if e.time]
    latest = max(timed) if timed else None

    # 최근 것부터 max_items개만
    entries = sorted(entries, key=lambda e: e.time or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    kept: list[Entry] = []
    skipped_music = 0
    for e in entries:
        if days and latest and e.time and (latest - e.time).days > days:
            continue
        if e.channel and e.channel.endswith("- Topic"):   # 유튜브 음악 자동채널
            skipped_music += 1
            continue
        if any(x in e.text.lower() for x in exclude_l):
            continue
        kept.append(e)
        if len(kept) >= max_items:
            break

    kw: Counter = Counter()
    ph: Counter = Counter()
    doc_freq: Counter = Counter()
    for e in kept:
        w = KIND_WEIGHT.get(e.kind, 1.0)
        if latest and e.time:
            age = max((latest - e.time).total_seconds() / 86400, 0)
            w *= 0.5 ** (age / half_life_days)
        toks = _tokens(e.text)
        if not toks:
            continue
        uniq = list(dict.fromkeys(toks))
        for t in uniq:
            kw[t] += w
            doc_freq[t] += 1
        for a, b in zip(uniq, uniq[1:]):
            ph[f"{a} {b}"] += w

    # 한 번만 나온 단어는 잡음일 확률이 높아 감점, 주제 사전에 있는 단어는 가점
    scored = {}
    for t, s in kw.items():
        if doc_freq[t] == 1 and len(kept) > 30:
            s *= 0.35
        if topic_of(t):
            s *= 1.3
        scored[t] = s
    keywords = sorted(scored.items(), key=lambda x: -x[1])
    phrases = sorted(((p, s) for p, s in ph.items() if s >= 1.2), key=lambda x: -x[1])

    agg: dict[str, float] = defaultdict(float)
    members: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for t, s in keywords:
        tp = topic_of(t)
        if tp:
            agg[tp] += s
            members[tp].append((t, s))
    topics = sorted(((tp, sc, [k for k, _ in members[tp][:5]]) for tp, sc in agg.items()), key=lambda x: -x[1])

    src = Counter(e.source for e in kept)
    kinds = Counter(e.kind for e in kept)
    stats = {
        "parsed_items": len(entries), "used_items": len(kept),
        "skipped_music_items": skipped_music,
        "by_source": dict(src), "by_kind": dict(kinds),
        "period": ({"from": min(e.time for e in kept if e.time).date().isoformat(),
                    "to": max(e.time for e in kept if e.time).date().isoformat()}
                   if any(e.time for e in kept) else None),
    }
    return Profile(keywords=keywords, topics=topics, phrases=phrases, stats=stats)


def profile_from_keywords(keywords: list[str]) -> Profile:
    """LLM이 이미 뽑아준 키워드 목록으로 바로 프로필 생성 (앞에 있을수록 가중치 높음)."""
    n = len(keywords)
    entries = []
    for i, k in enumerate(keywords):
        k = k.strip()
        if k:
            entries.append((k.lower(), float(n - i) / n * 3 + 1))
    agg: dict[str, float] = defaultdict(float)
    members: dict[str, list[str]] = defaultdict(list)
    for k, s in entries:
        tp = topic_of(k)
        if tp:
            agg[tp] += s
            members[tp].append(k)
    topics = sorted(((t, s, members[t][:5]) for t, s in agg.items()), key=lambda x: -x[1])
    return Profile(keywords=entries, topics=topics, phrases=[], stats={"used_items": len(entries),
                                                                      "by_source": {"keywords": len(entries)}})


def topic_names() -> list[str]:
    return list(TOPICS)
