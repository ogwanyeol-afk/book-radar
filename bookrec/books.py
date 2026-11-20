"""도서 검색(알라딘·네이버·도서관정보나루)과 추천 점수화."""
from __future__ import annotations

import asyncio
import html as htmllib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date

import httpx

from .history import Profile
from .topics import EXCLUDED_CATEGORIES, TOPICS, topic_of

TIMEOUT = httpx.Timeout(8.0, connect=4.0)
_TRANSPORT: httpx.AsyncBaseTransport | None = None   # 테스트에서 MockTransport 주입


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=_TRANSPORT)


@dataclass
class Book:
    title: str
    author: str = ""
    publisher: str = ""
    pub_date: str = ""
    isbn13: str = ""
    price: int | None = None
    cover: str = ""
    link: str = ""
    description: str = ""
    category: str = ""
    review_rank: float | None = None
    sources: set[str] = field(default_factory=set)
    queries: list[dict] = field(default_factory=list)
    score: float = 0.0
    reason: str = ""
    naver_link: str = ""

    def key(self) -> str:
        if self.isbn13:
            return self.isbn13
        return re.sub(r"[\s\W]+", "", self.title.split("-")[0].split("(")[0]).lower()

    def as_dict(self) -> dict:
        d = {
            "title": self.title, "author": self.author, "publisher": self.publisher,
            "pub_date": self.pub_date, "isbn13": self.isbn13, "price": self.price,
            "category": self.category, "link": self.link, "cover": self.cover,
            "description": (self.description[:180] + "…") if len(self.description) > 180 else self.description,
            "reason": self.reason, "score": round(self.score, 3),
            "buy_links": buy_links(self.isbn13, self.title,
                                   self.link if "aladin" in self.sources else "", self.naver_link),
        }
        return {k: v for k, v in d.items() if v not in (None, "")}


# 서점별 구매 페이지. ISBN13으로 검색하면 대부분 해당 상품 1건으로 바로 연결된다.
# (인터파크 주소는 이 개발 환경에서 접속 확인이 안 됨 — 배포 전 브라우저로 확인할 것)
STORES = {
    "알라딘": "https://www.aladin.co.kr/search/wsearch.aspx?SearchTarget=Book&SearchWord={q}",
    "교보문고": "https://search.kyobobook.co.kr/search?keyword={q}",
    "예스24": "https://www.yes24.com/Product/Search?domain=BOOK&query={q}",
    "인터파크": "https://bsearch.interpark.com/dsearch/book.jsp?query={q}",
}


def buy_links(isbn13: str = "", title: str = "", aladin_link: str = "", naver_link: str = "") -> dict:
    """서점별 구매 링크. 알라딘 API 링크는 TTB 파트너 링크라 그대로 쓴다."""
    from urllib.parse import quote
    q = quote(isbn13 or title)
    links = {name: url.format(q=q) for name, url in STORES.items()}
    if aladin_link:
        links["알라딘"] = aladin_link
    if naver_link:
        links["네이버 가격비교"] = naver_link   # 네이버 도서 상세: 여러 서점 판매가 비교
    return links


def _clean(s: str | None) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


# ---------------------------------------------------------------- 캐시

class _TTLCache:
    def __init__(self, ttl: float = 6 * 3600, size: int = 2000):
        self.ttl, self.size, self.d = ttl, size, {}

    def get(self, k):
        v = self.d.get(k)
        if v and time.time() - v[0] < self.ttl:
            return v[1]
        return None

    def put(self, k, val):
        if len(self.d) >= self.size:
            self.d.pop(next(iter(self.d)))
        self.d[k] = (time.time(), val)


_cache = _TTLCache()


# ---------------------------------------------------------------- 제공자

class Provider:
    name = "base"

    async def search(self, client: httpx.AsyncClient, query: str, n: int = 20) -> list[Book]:
        raise NotImplementedError


class Aladin(Provider):
    """알라딘 TTB OpenAPI — https://www.aladin.co.kr/ttb/wblog_manage.aspx 에서 키 발급"""
    name = "aladin"
    BASE = "https://www.aladin.co.kr/ttb/api"

    def __init__(self, key: str):
        self.key = key

    @staticmethod
    def _json(text: str) -> dict:
        return json.loads(text.strip().rstrip(";"), strict=False)

    @staticmethod
    def _book(it: dict) -> Book:
        rr = it.get("customerReviewRank")
        return Book(title=_clean(it.get("title")), author=_clean(it.get("author")),
                    publisher=_clean(it.get("publisher")), pub_date=it.get("pubDate", ""),
                    isbn13=it.get("isbn13") or "", price=it.get("priceSales") or it.get("priceStandard"),
                    cover=it.get("cover", ""), link=it.get("link", ""),
                    description=_clean(it.get("description")), category=it.get("categoryName", ""),
                    review_rank=float(rr) if rr not in (None, "") else None, sources={"aladin"})

    async def search(self, client, query, n=20):
        r = await client.get(f"{self.BASE}/ItemSearch.aspx", params={
            "ttbkey": self.key, "Query": query, "QueryType": "Keyword", "MaxResults": n, "start": 1,
            "SearchTarget": "Book", "Sort": "Accuracy", "Cover": "MidBig", "output": "js", "Version": "20131101"})
        r.raise_for_status()
        data = self._json(r.text)
        if "errorCode" in data:
            raise RuntimeError(f"알라딘 오류 {data.get('errorCode')}: {data.get('errorMessage')}")
        return [self._book(it) for it in data.get("item", [])]

    async def lookup(self, client, isbn13: str) -> Book | None:
        r = await client.get(f"{self.BASE}/ItemLookUp.aspx", params={
            "ttbkey": self.key, "itemIdType": "ISBN13", "ItemId": isbn13, "Cover": "MidBig",
            "output": "js", "Version": "20131101"})
        r.raise_for_status()
        items = self._json(r.text).get("item", [])
        return self._book(items[0]) if items else None


class NaverBook(Provider):
    """네이버 검색 API(책) — https://developers.naver.com 애플리케이션 등록 후 Client ID/Secret"""
    name = "naver"
    URL = "https://openapi.naver.com/v1/search/book.json"

    def __init__(self, cid: str, secret: str):
        self.h = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}

    async def search(self, client, query, n=20):
        r = await client.get(self.URL, headers=self.h, params={"query": query, "display": min(n, 100), "sort": "sim"})
        r.raise_for_status()
        out = []
        for it in r.json().get("items", []):
            pd = it.get("pubdate", "")
            isbn = (it.get("isbn") or "").split()
            out.append(Book(title=_clean(it.get("title")), author=_clean(it.get("author")).replace("^", ", "),
                            publisher=_clean(it.get("publisher")),
                            pub_date=f"{pd[:4]}-{pd[4:6]}-{pd[6:8]}" if len(pd) == 8 else pd,
                            isbn13=next((x for x in isbn if len(x) == 13), ""),
                            price=int(it["discount"]) if str(it.get("discount", "")).isdigit() else None,
                            cover=it.get("image", ""), naver_link=it.get("link", ""),
                            description=_clean(it.get("description")), sources={"naver"}))
        return out


class Data4Library:
    """도서관 정보나루 — https://www.data4library.kr 인증키. 실제 대출 데이터 기반 '함께 빌린 책'."""
    URL = "https://data4library.kr/api/recommandList"

    def __init__(self, key: str):
        self.key = key

    async def recommend(self, client, isbn13: str, kind: str = "reader") -> list[Book]:
        r = await client.get(self.URL, params={"authKey": self.key, "isbn13": isbn13, "type": kind, "format": "json"})
        r.raise_for_status()
        docs = (r.json().get("response") or {}).get("docs") or []
        out = []
        for d in docs:
            b = d.get("book", d)
            out.append(Book(title=_clean(b.get("bookname")), author=_clean(b.get("authors")),
                            publisher=_clean(b.get("publisher")), pub_date=str(b.get("publication_year", "")),
                            isbn13=b.get("isbn13", ""), cover=b.get("bookImageURL", ""),
                            link=b.get("bookDtlUrl", ""), sources={"data4library"}))
        return out


def providers_from_env() -> list[Provider]:
    ps: list[Provider] = []
    if os.getenv("ALADIN_TTB_KEY"):
        ps.append(Aladin(os.environ["ALADIN_TTB_KEY"]))
    if os.getenv("NAVER_CLIENT_ID") and os.getenv("NAVER_CLIENT_SECRET"):
        ps.append(NaverBook(os.environ["NAVER_CLIENT_ID"], os.environ["NAVER_CLIENT_SECRET"]))
    return ps


# ---------------------------------------------------------------- 검색어 만들기

def build_queries(p: Profile, max_queries: int = 10, purpose: str | None = None) -> list[dict]:
    """관심 프로필 → 도서 검색어 목록. 각 항목: {query, topic, keywords}"""
    qs: list[dict] = []
    seen: set[str] = set()

    def add(q: str, topic: str | None, kws: list[str]):
        q = " ".join(w.upper() if w.isascii() and len(w) <= 4 else w for w in q.split())  # etf→ETF
        if purpose:
            q = f"{q} {purpose}"
        if q and q not in seen and len(qs) < max_queries:
            seen.add(q)
            qs.append({"query": q, "topic": topic, "keywords": kws})

    top_score = p.keywords[0][1] if p.keywords else 1.0
    in_phrase: set[str] = set()

    # 1) 상위 주제마다 대표 키워드 1개씩 — 주제 폭을 먼저 확보
    for tp, _, kws in p.topics[:5]:
        if kws:
            add(kws[0], tp, [kws[0]])

    # 2) 자주 같이 나온 두 단어 묶음 (한쪽이 주제어일 때) 최대 2개
    n_ph = 0
    for ph, s in p.phrases[:8]:
        a, b = ph.split(" ", 1)
        if n_ph < 2 and s >= top_score * 0.25 and (topic_of(a) or topic_of(b)):
            add(ph, topic_of(a) or topic_of(b), [a, b])
            in_phrase |= {a, b}
            n_ph += 1

    # 3) 주제별 두 번째 키워드
    for tp, _, kws in p.topics[:4]:
        for k in kws[1:]:
            if k not in in_phrase:
                add(k, tp, [k])
                break

    # 4) 주제 사전에 없지만 강한 관심어 (새로운 관심사 발견)
    for k, s in p.keywords[:15]:
        if not topic_of(k) and s >= top_score * 0.3 and k not in in_phrase:
            add(k, None, [k])

    # 5) 그래도 모자라면 주제 대표 검색어
    for tp, _, _ in p.topics[:3]:
        for seed in TOPICS[tp]["seeds"]:
            add(seed, tp, [])
    return qs


# ---------------------------------------------------------------- 점수화

def _year(pd: str) -> int | None:
    m = re.match(r"(\d{4})", pd or "")
    return int(m.group(1)) if m else None


def score_books(books: list[Book], p: Profile, include_study: bool = False) -> list[Book]:
    top = p.keywords[:40]
    mx = top[0][1] if top else 1.0
    this_year = date.today().year
    out = []
    for b in books:
        if not include_study and any(x in b.category for x in EXCLUDED_CATEGORIES):
            continue
        text = f"{b.title} {b.description} {b.category}".lower()
        title = b.title.lower()
        matched = [(k, s) for k, s in top if k in text]
        rel = sum(s / mx * (1.6 if k in title else 1.0) for k, s in matched)
        rel += 0.5 * (len(b.queries) - 1)                         # 여러 검색어에서 동시에 나온 책
        topics = {q["topic"] for q in b.queries if q.get("topic")}
        cat_hit = any(c in b.category for t in topics for c in TOPICS[t]["categories"])
        rel += 0.6 if cat_hit else 0
        if b.review_rank:
            rel += b.review_rank * 0.05                            # 알라딘 평점(0~10)
        y = _year(b.pub_date)
        if y:
            rel += 0.3 if this_year - y <= 3 else (-0.3 if this_year - y > 15 else 0)
        best_rank = min(q.get("rank", 20) for q in b.queries)
        rel += max(0, (10 - best_rank)) * 0.04                     # 검색 정확도 순위
        b.score = rel
        kws = [k for k, _ in matched[:3]] or [k for q in b.queries for k in q["keywords"]][:3]
        tp = next(iter(topics), None)
        b.reason = (f"{tp} 관심" if tp else "기록 속 관심어") + (f" — '{', '.join(kws)}' 관련" if kws else "")
        out.append(b)
    return sorted(out, key=lambda x: -x.score)


def diversify(books: list[Book], count: int) -> list[Book]:
    """한 주제가 결과를 독식하지 않게 주제별 상한을 둔다."""
    topics = {(b.queries[0].get("topic") or "기타") for b in books}
    cap = max(2, math.ceil(count / max(len(topics), 1)) + 1)
    picked, per = [], {}
    for b in books:
        t = b.queries[0].get("topic") or "기타"
        if per.get(t, 0) < cap:
            picked.append(b)
            per[t] = per.get(t, 0) + 1
        if len(picked) >= count:
            return picked
    for b in books:   # 상한 때문에 모자라면 채우기
        if b not in picked:
            picked.append(b)
        if len(picked) >= count:
            break
    return picked


# ---------------------------------------------------------------- 실행

async def _search_one(client, prov: Provider, q: dict, n: int) -> tuple[dict, list[Book], str | None]:
    ck = (prov.name, q["query"], n)
    hit = _cache.get(ck)
    if hit is not None:
        return q, hit, None
    try:
        res = await prov.search(client, q["query"], n)
        _cache.put(ck, res)
        return q, res, None
    except Exception as e:  # 한 검색어 실패가 전체를 막지 않게
        return q, [], f"{prov.name}:{q['query']} → {type(e).__name__}: {e}"


async def recommend(p: Profile, *, count: int = 10, providers: list[Provider] | None = None,
                    exclude_titles: list[str] | None = None, purpose: str | None = None,
                    include_study: bool = False, max_queries: int = 10) -> dict:
    providers = providers if providers is not None else providers_from_env()
    queries = build_queries(p, max_queries=max_queries, purpose=purpose)
    if not providers:
        return {"books": [], "queries": [q["query"] for q in queries], "warning":
                "도서 API 키가 없습니다. ALADIN_TTB_KEY 또는 NAVER_CLIENT_ID/NAVER_CLIENT_SECRET을 설정하세요. "
                "위 queries로 직접 검색할 수도 있습니다."}
    errors: list[str] = []
    pool: dict[str, Book] = {}
    async with _client() as client:
        results = await asyncio.gather(*[_search_one(client, pr, q, 20) for pr in providers for q in queries])
    excl = [re.sub(r"\s+", "", t).lower() for t in (exclude_titles or []) if t.strip()]
    for q, books, err in results:
        if err:
            errors.append(err)
        for rank, b in enumerate(books):
            if excl and any(x in re.sub(r"\s+", "", b.title).lower() for x in excl):
                continue
            k = b.key()
            cur = pool.get(k)
            meta = {**q, "rank": rank}
            if cur is None:
                b.queries = [meta]
                pool[k] = b
            else:  # 같은 책: 정보 합치기
                if not any(m["query"] == q["query"] for m in cur.queries):
                    cur.queries.append(meta)
                cur.sources |= b.sources
                for f in ("description", "category", "cover", "isbn13", "link", "naver_link"):
                    if not getattr(cur, f) and getattr(b, f):
                        setattr(cur, f, getattr(b, f))
                if cur.review_rank is None:
                    cur.review_rank = b.review_rank
    ranked = diversify(score_books(list(pool.values()), p, include_study), count)
    out = {"books": [b.as_dict() for b in ranked], "queries": [q["query"] for q in queries],
           "candidates": len(pool)}
    if errors:
        out["errors"] = errors[:5]
    return out


async def similar(isbn13: str, count: int = 8) -> dict:
    """특정 책과 함께 읽힌 책 (정보나루 대출데이터 우선, 없으면 알라딘 같은 분류·저자 검색)."""
    async with _client() as client:
        if os.getenv("DATA4LIBRARY_KEY"):
            try:
                bs = await Data4Library(os.environ["DATA4LIBRARY_KEY"]).recommend(client, isbn13)
                if bs:
                    return {"basis": "도서관 정보나루 대출 데이터(이 책을 빌린 사람들이 함께 빌린 책)",
                            "books": [b.as_dict() for b in bs[:count]]}
            except Exception:
                pass  # 정보나루 실패 시 알라딘으로
        if os.getenv("ALADIN_TTB_KEY"):
            al = Aladin(os.environ["ALADIN_TTB_KEY"])
            base = await al.lookup(client, isbn13)
            if not base:
                return {"books": [], "warning": "알라딘에서 ISBN을 찾지 못했습니다."}
            leaf = base.category.split(">")[-1] if base.category else ""
            q1 = await al.search(client, f"{leaf}", 25) if leaf else []
            q2 = await al.search(client, base.author.split(",")[0].split("(")[0].strip(), 10) if base.author else []
            seen, out = {base.key()}, []
            same_cat = [b for b in q1 if b.category == base.category]
            for b in q2 + same_cat:          # 같은 저자 먼저, 다음 같은 세부분류
                if b.key() not in seen:
                    seen.add(b.key())
                    out.append(b)
            return {"basis": f"알라딘 같은 분류({leaf})·같은 저자", "base": base.as_dict(),
                    "books": [b.as_dict() for b in out[:count]]}
    return {"books": [], "warning": "DATA4LIBRARY_KEY 또는 ALADIN_TTB_KEY가 필요합니다."}
