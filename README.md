# 북레이더 MCP — 시청·검색 기록 기반 도서 추천 (v0.1)

유튜브 시청기록, 구글·네이버 검색기록 → 관심 키워드·주제 추출 → 알라딘/네이버 책 검색 → 점수화해 추천

## 도구

| 도구 | 하는 일 |
|---|---|
| `history_export_guide` | 유튜브·구글·네이버 기록 내보내기/복사 방법 안내 |
| `analyze_interests` | 기록 → 관심 키워드·주제·기간 통계 (추천 전 확인용) |
| `recommend_books` | 기록 또는 키워드 → 추천 도서 + 추천 이유 |
| `book_purchase_links` | ISBN/제목 → 알라딘·교보문고·예스24·인터파크 구매 페이지 링크 |
| `similar_books` | ISBN13 → 함께 많이 읽힌 책 (정보나루 대출데이터 → 없으면 알라딘 같은 분류·저자) |

`recommend_books` 주요 옵션: `keywords`(기록 없이 관심사만), `purpose`(입문/실전/에세이), `exclude_titles`(이미 읽은 책), `days`(최근 N일), `exclude_keywords`(업무용 검색어 등 제외), `include_study`(수험서 포함)

## 구매 연결

- 추천 결과마다 `buy_links`(알라딘·교보문고·예스24·인터파크, 네이버 결과면 가격비교 링크 추가)
- 주문·결제는 하지 않음: 서점들이 외부 주문 API를 공개하지 않고, 결제는 사용자 본인 인증이 필요
- 알라딘 링크는 API가 준 TTB 링크를 그대로 사용 (알라딘 TTB 파트너 적립 조건은 알라딘에서 확인)
- 교보·예스24 ISBN 검색 링크는 해당 책 1건으로 연결 확인(2026-11-20), 인터파크는 배포 전 직접 확인 필요

## 기록 입력 방식 (왜 자동 연동이 아닌가)

| 출처 | 자동 API | 이 MCP의 방식 |
|---|---|---|
| 유튜브 시청기록 | 없음 (YouTube Data API는 2016년 이후 시청기록 미제공) | 구글 테이크아웃 `watch-history.json`/`.html`, 또는 myactivity 화면 복사 |
| 구글 검색기록 | 없음 | 테이크아웃 `내 활동 > 검색` JSON/HTML, 또는 화면 복사 |
| 네이버 검색기록 | 없음 (내보내기 기능도 없음) | '최근 검색어' 목록 복사·붙여넣기 |

- 영어·한국어 테이크아웃 문구(`Watched …`, `…을(를) 시청했습니다.`) 모두 인식
- 유튜브 음악 자동채널(`- Topic`)은 제외, 검색은 시청보다 1.5배 가중, 90일 반감기로 최근 관심 우선
- 전화번호·이메일·주민번호 형태와 URL은 키워드화 전에 제거, 원문은 저장·로그하지 않음

## 추천 로직

1. 기록 → 토큰화(조사 제거·불용어) → 출처·최근성 가중 → 한 번만 나온 단어 감점, 주제사전 단어 가점
2. 17개 관심 주제(재테크·세금·IT·AI·인문 등)로 묶음 (`bookrec/topics.py`에서 수정)
3. 검색어 최대 10개: 주제별 대표어 → 자주 같이 나온 두 단어 → 주제별 2순위어 → 사전에 없는 강한 관심어
4. 알라딘·네이버 결과 병합(ISBN 중복 제거) → 관심어 매칭(제목 1.6배)·분류 일치·여러 검색어 동시 등장·평점·최신성 점수
5. 주제별 상한으로 한 주제 독식 방지, 수험서·참고서 기본 제외

## API 키 (하나 이상)

- 알라딘 TTB: https://www.aladin.co.kr/ttb/wblog_manage.aspx → `ALADIN_TTB_KEY` (분류·평점이 있어 추천 품질이 가장 좋음)
- 네이버 검색 API(책): https://developers.naver.com → `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`
- 도서관 정보나루(선택): https://www.data4library.kr → `DATA4LIBRARY_KEY`

키가 없으면 도서 대신 생성된 검색어만 돌려줍니다.

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # 키 입력
# 원격 (PlayMCP용): http://<서버>:8000/mcp , 헬스체크 /health
set -a; . ./.env; set +a; python server.py
# 로컬 Claude 데스크톱 (테이크아웃 파일 경로 직접 읽기 가능)
MCP_TRANSPORT=stdio BOOKREC_LOCAL_FILES=1 python server.py
# 테스트 (네트워크 불필요)
python -m pytest -q
```

### 카카오클라우드 (세무 기장 도우미 MCP와 같은 VM에 함께)

```bash
docker build -t book-radar .
docker run -d --name book-radar --env-file .env -p 127.0.0.1:8001:8000 --restart unless-stopped book-radar
# Caddyfile 예: books.도메인 { reverse_proxy 127.0.0.1:8001 }
```

## PlayMCP 등록 시 참고

- 원격 엔드포인트 `https://<도메인>/mcp`, stateless + JSON 응답
- 대화형 AI가 붙여넣기 텍스트를 그대로 넘기므로 3MB 제한, 길면 최근 몇 달치만
- 심사 대비: 개인정보 미저장·미로깅을 서비스 설명에 명시 권장

## 다음 버전 후보

- 유튜브 OAuth 연동: 시청기록은 불가하지만 `좋아요 표시한 동영상`·구독 채널은 가져올 수 있음 (PlayMCP의 사용자 인증 지원 여부 확인 필요)
- 정보나루 연령·성별·지역 인기대출 반영, 도서관 소장·대출가능 조회
- 사무실 거래처용: 업종별 추천 도서 안내문(뉴스레터) 자동 생성
