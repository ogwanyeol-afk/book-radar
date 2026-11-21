"""유튜브 '좋아요 표시한 동영상' 가져오기 (YouTube Data API v3, OAuth youtube.readonly).

A단계(대표님 전용): 환경변수의 refresh token 하나로 동작
B단계(이용자별): TokenStore를 이용자별 저장소로 바꾸면 같은 함수 재사용
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from .history import Entry

TOKEN_URL = "https://oauth2.googleapis.com/token"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
SKIP_CATEGORIES = {"10"}          # 10 = Music (음악은 책 관심사와 거리가 멀어 제외)
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class TokenStore:
    """refresh token 보관소. A단계는 환경변수 하나, B단계에서 이용자별 DB로 교체."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self._access: str | None = None
        self._exp = 0.0

    @classmethod
    def from_env(cls) -> "TokenStore | None":
        cid, sec, rt = (os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET"),
                        os.getenv("OWNER_YOUTUBE_REFRESH_TOKEN"))
        return cls(cid, sec, rt) if cid and sec and rt else None

    async def access_token(self, client: httpx.AsyncClient) -> str:
        if self._access and time.time() < self._exp - 60:
            return self._access
        r = await client.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
        if r.status_code != 200:
            raise RuntimeError(f"구글 토큰 갱신 실패({r.status_code}): {r.text[:200]} "
                               "— refresh token이 만료됐으면 OAuth Playground에서 다시 발급하세요.")
        d = r.json()
        self._access, self._exp = d["access_token"], time.time() + d.get("expires_in", 3600)
        return self._access


async def fetch_liked(store: TokenStore, limit: int = 200,
                      transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """좋아요 누른 영상(최신순) — 제목·채널·태그·카테고리."""
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        token = await store.access_token(client)
        page = None
        while len(out) < limit:
            params = {"part": "snippet", "myRating": "like", "maxResults": 50}
            if page:
                params["pageToken"] = page
            r = await client.get(VIDEOS_URL, params=params, headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                raise RuntimeError(f"유튜브 API 오류({r.status_code}): {r.text[:200]}")
            d = r.json()
            for it in d.get("items", []):
                sn = it.get("snippet", {})
                out.append({"id": it.get("id"), "title": sn.get("title", ""),
                            "channel": sn.get("channelTitle", ""), "tags": sn.get("tags", [])[:8],
                            "category": sn.get("categoryId", "")})
            page = d.get("nextPageToken")
            if not page:
                break
    return out[:limit]


def liked_to_entries(videos: list[dict]) -> list[Entry]:
    """API는 '좋아요 누른 날짜'를 주지 않으므로 목록 순서(최신순)를 하루 간격 시각으로 바꿔 최근 가중치를 살린다."""
    now = datetime.now(timezone.utc)
    entries = []
    for i, v in enumerate(videos):
        if v.get("category") in SKIP_CATEGORIES:
            continue
        text = " ".join([v["title"], *v.get("tags", [])])
        entries.append(Entry(text=text, kind="like", source="youtube",
                             time=now - timedelta(days=i), channel=v.get("channel")))
    return entries
