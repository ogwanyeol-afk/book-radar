"""관심사 기반 도서 추천 MCP 서버.

유튜브·구글·네이버 시청/검색 기록 → 관심 키워드·주제 → 알라딘/네이버 책 검색 → 점수화 추천

실행
  원격(PlayMCP·카카오클라우드):  python server.py            (MCP_TRANSPORT=streamable-http 기본)
  로컬(Claude 데스크톱 등):        MCP_TRANSPORT=stdio BOOKREC_LOCAL_FILES=1 python server.py
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from bookrec.books import buy_links, recommend, similar
from bookrec.history import build_profile, parse_history, profile_from_keywords

LOCAL_FILES = os.getenv("BOOKREC_LOCAL_FILES") == "1"
MAX_TEXT = 3_000_000  # 붙여넣기 기록 최대 3MB

mcp = MCPServer(
    name="book-radar",
    title="관심사 도서 추천",
    version="0.1.0",
    instructions=(
        "사용자의 유튜브 시청기록, 구글·네이버 검색기록(붙여넣기 텍스트 또는 구글 테이크아웃 JSON/HTML)을 분석해 "
        "관심 주제를 뽑고 국내 도서를 추천합니다. 기록이 없으면 history_export_guide로 내보내는 방법을 안내하고, "
        "사용자가 관심사를 말로만 알려주면 keywords로 recommend_books를 호출하세요. "
        "추천 결과의 buy_links로 서점별 구매 페이지를 안내하세요(주문·결제는 사용자가 서점에서 직접). "
        "기록 원문은 저장하지 않으며, 답변에서 사용자의 개별 검색어를 그대로 나열하지 마세요."
    ),
)
RO = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

GUIDE = {
    "youtube": [
        "PC에서 takeout.google.com 접속 → '모두 선택 해제' → 'YouTube 및 YouTube Music'만 선택",
        "'모든 YouTube 데이터 포함' → '기록'만 체크, '여러 형식' → 기록 형식을 JSON으로 변경",
        "내보내기 생성 → 메일로 온 압축파일의 'YouTube 및 YouTube Music/기록/시청 기록.json'(watch-history.json) 사용",
        "빠른 방법: myactivity.google.com → YouTube 기록에서 최근 항목을 드래그·복사해 붙여넣어도 됩니다",
        "참고: YouTube Data API는 2016년 이후 시청기록을 제공하지 않아 자동 연동 대신 내보내기 파일을 씁니다",
    ],
    "google": [
        "takeout.google.com → '내 활동' 선택 → '모든 활동 데이터 포함'에서 '검색'만 체크, 형식 JSON",
        "압축파일의 '내 활동/검색/내활동.json'(MyActivity.json) 사용",
        "빠른 방법: myactivity.google.com/product/search 화면을 복사해 붙여넣기",
    ],
    "naver": [
        "네이버는 검색기록 내보내기·API가 없습니다",
        "네이버 앱/PC 검색창 클릭 → '최근 검색어'(검색기록 관리) 목록을 길게 복사해 붙여넣으세요",
        "검색기록 저장이 꺼져 있으면 기록이 없으니 관심 키워드를 직접 알려주셔도 됩니다",
    ],
}


def _read_files(file_paths: list[str]) -> list[str]:
    if not LOCAL_FILES:
        raise ValueError("file_paths는 로컬 실행(BOOKREC_LOCAL_FILES=1)에서만 쓸 수 있습니다. 내용을 붙여넣어 주세요.")
    out = []
    for fp in file_paths:
        p = Path(fp).expanduser()
        if p.suffix.lower() not in (".json", ".html", ".htm", ".txt", ".csv"):
            raise ValueError(f"지원하지 않는 파일 형식: {p.name}")
        out.append(p.read_text(encoding="utf-8", errors="ignore"))
    return out


def _profile(history_text, file_paths, source, days, exclude_keywords):
    if history_text and len(history_text) > MAX_TEXT:
        raise ValueError("기록이 너무 깁니다(3MB 초과). 최근 몇 달치만 붙여넣어 주세요.")
    entries = parse_history(history_text or "", source)
    for raw in _read_files(file_paths) if file_paths else []:   # 파일마다 형식이 달라 따로 파싱
        entries += parse_history(raw, source)
    if not entries:
        raise ValueError("기록에서 항목을 찾지 못했습니다. history_export_guide를 참고해 다시 붙여넣어 주세요.")
    return build_profile(entries, days=days, exclude=exclude_keywords)


Source = Literal["auto", "youtube", "google", "naver"]


@mcp.tool(annotations=RO)
def history_export_guide(source: Literal["youtube", "google", "naver", "all"] = "all") -> dict:
    """유튜브 시청기록·구글 검색기록·네이버 검색기록을 내보내거나 복사하는 방법을 안내합니다."""
    return GUIDE if source == "all" else {source: GUIDE[source]}


@mcp.tool(annotations=RO)
def analyze_interests(
    history_text: str | None = None,
    source: Source = "auto",
    days: int | None = None,
    exclude_keywords: list[str] | None = None,
    file_paths: list[str] | None = None,
) -> dict:
    """시청·검색 기록에서 관심 키워드와 관심 주제를 뽑아 보여줍니다(추천 전 확인용).

    history_text: 구글 테이크아웃 JSON/HTML 내용 또는 한 줄에 하나씩 붙여넣은 기록
    source: 붙여넣은 텍스트의 출처(네이버 붙여넣기면 'naver')
    days: 최근 N일만 분석
    exclude_keywords: 분석에서 뺄 단어(예: 업무용 검색어)
    file_paths: 로컬 실행일 때만, 테이크아웃 파일 경로
    """
    p = _profile(history_text, file_paths, source, days, exclude_keywords)
    return p.as_dict(25)


@mcp.tool(annotations=RO)
async def recommend_books(
    history_text: str | None = None,
    keywords: list[str] | None = None,
    source: Source = "auto",
    count: int = 10,
    purpose: str | None = None,
    exclude_titles: list[str] | None = None,
    include_study: bool = False,
    days: int | None = None,
    exclude_keywords: list[str] | None = None,
    file_paths: list[str] | None = None,
) -> dict:
    """시청·검색 기록(또는 관심 키워드)을 바탕으로 도서를 추천합니다.

    history_text 또는 keywords 중 하나는 필요합니다. keywords는 중요한 것부터 순서대로.
    purpose: '입문', '실전', '에세이'처럼 원하는 책 성격(검색어에 덧붙임)
    exclude_titles: 이미 읽은 책 제목(일부만 써도 됨)
    include_study: 수험서·참고서도 포함할지
    """
    count = max(1, min(count, 30))
    if keywords and not (history_text or file_paths):
        p = profile_from_keywords(keywords)
    elif history_text or file_paths:
        p = _profile(history_text, file_paths, source, days, exclude_keywords)
        if keywords:  # 기록 + 사용자가 강조한 키워드
            extra = profile_from_keywords(keywords)
            boost = p.keywords[0][1] if p.keywords else 1
            p.keywords = sorted(p.keywords + [(k, s * boost / 4) for k, s in extra.keywords], key=lambda x: -x[1])
    else:
        raise ValueError("history_text(기록) 또는 keywords(관심 키워드)를 넣어 주세요.")
    res = await recommend(p, count=count, exclude_titles=exclude_titles, purpose=purpose,
                          include_study=include_study)
    res["interest_summary"] = [{"topic": t, "keywords": ks} for t, _, ks in p.topics[:5]]
    res["stats"] = p.stats
    return res


@mcp.tool(annotations=RO)
async def similar_books(isbn13: str, count: int = 8) -> dict:
    """마음에 든 책(ISBN13)과 함께 많이 읽힌 책을 찾아줍니다. 도서관 정보나루 대출데이터 우선."""
    return await similar(isbn13.replace("-", "").strip(), max(1, min(count, 20)))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def book_purchase_links(isbn13: str | None = None, title: str | None = None) -> dict:
    """책 한 권의 알라딘·교보문고·예스24·인터파크 구매 페이지 링크를 만듭니다.
    결제는 사용자가 서점 사이트에서 직접 합니다(이 도구는 주문·결제를 하지 않음)."""
    if not (isbn13 or title):
        raise ValueError("isbn13 또는 title이 필요합니다.")
    return {"links": buy_links((isbn13 or "").replace("-", "").strip(), title or ""),
            "note": "ISBN으로 검색되어 해당 책 상품 페이지로 바로 연결됩니다. 결제는 각 서점에서 진행하세요."}


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    from starlette.responses import JSONResponse
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    if transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")),
                stateless_http=True, json_response=True)
