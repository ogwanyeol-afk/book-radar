"""python -m pytest -q  (네트워크 없이 알라딘·네이버 응답 형식을 흉내 낸 MockTransport로 검증)"""
import asyncio
import json
import os
from pathlib import Path

import httpx

from bookrec import books
from bookrec.history import build_profile, parse_history, profile_from_keywords

S = Path(__file__).parent.parent / "samples"

CATALOG = [  # (제목, 분류, 설명, 출간일, 평점)
    ("미국 배당 ETF 투자 바이블", "국내도서>경제경영>재테크/투자>주식/증권", "월배당 ETF와 배당 성장주 포트폴리오", "2025-03-01", 9.4),
    ("부동산 경매 처음공부", "국내도서>경제경영>재테크/투자>부동산/경매", "권리분석부터 명도까지 경매 입문", "2024-06-10", 9.0),
    ("상속·증여 절세 가이드", "국내도서>경제경영>재테크/투자>세무/회계", "상속세와 증여세를 줄이는 절세 전략", "2026-01-05", 8.8),
    ("파이썬 업무 자동화", "국내도서>컴퓨터/모바일>프로그래밍 언어>파이썬", "엑셀과 이메일을 파이썬으로 자동화", "2025-09-01", 9.2),
    ("Claude와 MCP로 만드는 AI 에이전트", "국내도서>컴퓨터/모바일>인공지능", "MCP 서버로 업무 자동화 AI 에이전트 만들기", "2026-07-01", 9.6),
    ("니체의 말", "국내도서>인문학>서양철학>독일철학", "니체 철학의 핵심 문장", "2010-01-01", 8.0),
    ("스토아 철학 입문", "국내도서>인문학>서양철학", "스토아 철학으로 배우는 마음의 평정", "2023-02-01", 8.5),
    ("2027 투자자산운용사 문제집", "국내도서>수험서/자격증>금융/보험", "배당 ETF 투자 문제 수록", "2026-10-01", 7.0),
]


def _aladin_handler(req: httpx.Request) -> httpx.Response:
    q = req.url.params.get("Query", "").lower().split()
    items = []
    for i, (t, c, d, pd, rr) in enumerate(CATALOG):
        if all(w in f"{t} {d} {c}".lower() for w in q):
            items.append({"title": t, "author": "저자" + str(i), "publisher": "출판사", "pubDate": pd,
                          "isbn13": f"97911000000{i:02d}", "priceSales": 18000, "categoryName": c,
                          "description": d, "customerReviewRank": rr, "link": "https://aladin.co.kr/x",
                          "cover": ""})
    return httpx.Response(200, text=json.dumps({"item": items}, ensure_ascii=False) + ";")


def _naver_handler(req: httpx.Request) -> httpx.Response:
    q = req.url.params.get("query", "").lower().split()
    items = [{"title": f"<b>{t}</b>", "author": "저자^공저", "publisher": "출판사", "pubdate": pd.replace("-", ""),
              "isbn": f"89000000{i:02d} 97911000000{i:02d}", "discount": "16200", "description": d, "link": "", "image": ""}
             for i, (t, c, d, pd, rr) in enumerate(CATALOG) if all(w in f"{t} {d}".lower() for w in q)]
    return httpx.Response(200, json={"items": items})


def _mock(req):
    return _aladin_handler(req) if "aladin" in req.url.host else _naver_handler(req)


def _all_entries():
    e = parse_history((S / "watch-history.json").read_text())
    e += parse_history((S / "MyActivity-search.json").read_text())
    e += parse_history((S / "naver-search.txt").read_text(), "naver")
    return e


def test_parsers():
    yt = parse_history((S / "watch-history.json").read_text())
    assert yt and all(x.kind == "watch" and x.source == "youtube" for x in yt)
    g = parse_history((S / "MyActivity-search.json").read_text())
    assert all(x.kind in ("search", "visit") for x in g)
    nv = parse_history((S / "naver-search.txt").read_text(), "naver")
    assert [x.text for x in nv][:2] == ["배당주 추천", "양도소득세 비과세"]
    h = parse_history((S / "watch-history.html").read_text())
    assert len(h) == 2 and h[0].channel == "슈카월드" and h[1].time.month == 10


def test_profile_topics_and_music_skip():
    p = build_profile(_all_entries())
    topics = [t for t, _, _ in p.topics]
    assert topics[0] == "재테크·투자" and "IT·AI" in topics[:3]
    assert p.stats["skipped_music_items"] > 0
    assert "newjeans" not in dict(p.keywords)


def test_pii_removed():
    p = build_profile(parse_history("홍길동 010-1234-5678 배당\nabc@naver.com 배당 ETF", "naver"))
    ks = dict(p.keywords)
    assert not any(x in k for k in ks for x in ("010", "abc", "naver.com"))


def test_recommend_end_to_end(monkeypatch=None):
    books._TRANSPORT = httpx.MockTransport(_mock)
    books._cache.d.clear()
    provs = [books.Aladin("TEST"), books.NaverBook("id", "secret")]
    p = build_profile(_all_entries())
    res = asyncio.run(books.recommend(p, count=6, providers=provs, exclude_titles=["니체의 말"]))
    titles = [b["title"] for b in res["books"]]
    assert "2027 투자자산운용사 문제집" not in titles          # 수험서 제외
    assert "니체의 말" not in titles                           # 이미 읽은 책 제외
    assert "미국 배당 ETF 투자 바이블" in titles[:3]
    assert len({b["isbn13"] for b in res["books"]}) == len(titles)  # 알라딘·네이버 중복 병합
    assert all(b["reason"] for b in res["books"])


def test_keywords_only_and_no_key():
    p = profile_from_keywords(["플라워 디자인", "창업", "브랜딩"])
    os.environ.pop("ALADIN_TTB_KEY", None)
    res = asyncio.run(books.recommend(p, providers=[]))
    assert res["books"] == [] and res["queries"] and "warning" in res


def test_buy_links():
    books._TRANSPORT = httpx.MockTransport(_mock)
    books._cache.d.clear()
    res = asyncio.run(books.recommend(build_profile(_all_entries()), count=3,
                                      providers=[books.Aladin("T"), books.NaverBook("a", "b")]))
    bl = res["books"][0]["buy_links"]
    assert set(bl) >= {"알라딘", "교보문고", "예스24", "인터파크"}
    assert bl["알라딘"] == "https://aladin.co.kr/x"            # 알라딘 API(TTB) 링크 우선
    assert res["books"][0]["isbn13"] in bl["교보문고"]
