# Lattice Stock Analyzer

무료 데이터 소스로 한국/미국 주식의 파일럿 LFS(Lattice Fundamental Score)를 계산하는 모바일 웹 앱입니다.

## 데이터 소스
- 한국: OpenDART
- 미국: SEC EDGAR Company Facts
- 가격: Yahoo Finance chart endpoint (무료 fallback, 비공식)

## Vercel 환경변수
- DART_API_KEY: OpenDART 40자리 인증키
- SEC_USER_AGENT: 선택. 예: lattice-stock-analyzer/1.0 your-contact

API 키는 코드나 GitHub에 커밋하지 마세요.

## 사용 예시
- 한국: 삼성전자 또는 005930
- 미국: NVDA

## 주의
현재 LFS는 pilot-0.1이며 업종별 percentile 정규화 전입니다. 투자 판단을 자동화하는 최종 모델이 아니라 데이터 파이프라인과 계산식 검증용입니다.
