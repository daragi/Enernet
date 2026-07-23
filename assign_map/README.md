# Daejeon Assignment Map

대전 안전점검 오더를 주소 좌표와 SHP 기반 블럭 경계에 매칭하고, 서비스처리센터별 인원에게 순차 배정하는 지도형 운영 도구입니다. 브라우저에서 지도, 필지/블럭 경계, 주소 오더, 인원별 배정 결과를 함께 확인할 수 있습니다.

## 주요 기능

- 엑셀 오더 파일 업로드 및 센터/월별 데이터 처리
- 네이버 지오코딩 결과 캐시 활용 및 주소 좌표 보완
- 연속지적도(SHP) 필지에 주소를 공간 매칭
- 도로 경계와 교통 장벽을 반영한 SHP 기반 assignment block 생성
- 블럭별 인접 관계(neighbor)와 블럭 그래프 생성
- 센터별 인원 목표량 계산과 순차 배정
- 500m 초과 이동 제한 및 잔여 오더 분리
- 지도에서 블럭 경계, 오더 점, 인원별 색상, 상세 주소 확인
- H072/H074 대상 route neighbor 및 block-unit 배정 검증 HTML 생성
- 배정 결과를 `H[센터번호]_[연.월]_인원배정.xlsx` 형식으로 저장

## 디렉터리 구조

```text
assign_map/
├─ app.py                         # FastAPI 서버와 업로드/처리 API
├─ daejeon_map.html               # 운영용 지도 화면
├─ run_assign_map.bat             # Windows 실행 스크립트
├─ requirements.txt               # Python 의존성
├─ process_geocode.json           # 주소별 좌표 및 처리 결과 캐시
├─ geocodes/geocoding.json        # 지오코딩 캐시
├─ processed_assignment_blocks.json# SHP 기반 최종 블럭 데이터
├─ 07.06/
│  ├─ preprocess_blocks.py        # 지적도 필지 매칭/기본 neighbor 처리
│  └─ preprocess_transport_blocks.py # 도로/교통장벽 반영 assignment block 처리
├─ dong/                           # 법정동 경계 SHP 보조 데이터
└─ boundary_map/
   ├─ generate_selected_block_maps.py # H072/H074 블럭 경계 시각화
   ├─ generate_route_debug_maps.py    # route/block-unit 검증 시각화
   ├─ assignment_blocks_H*.html        # 블럭 경계 확인 결과
   └─ debug_route_H*.html              # 순로/neighbor/인원 배정 검증 결과
```

## 설치

Python 3.11 이상을 권장합니다.

```powershell
cd C:\DA\assign_map
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## API 키

네이버 지도 및 지오코딩 API 키는 저장소에 커밋하지 않습니다. 로컬에서 `api_keys.json`을 만들거나 환경변수를 사용합니다.

```json
{
  "NAVER_MAP_CLIENT_ID": "...",
  "NAVER_MAP_CLIENT_SECRET": "...",
  "NAVER_GEOCODE_KEY_ID": "...",
  "NAVER_GEOCODE_KEY": "..."
}
```

키 파일은 `.gitignore`로 제외됩니다. GitHub에 키가 올라간 적이 있다면 키를 폐기하고 재발급해야 합니다.

## 실행

```powershell
cd C:\DA\assign_map
python app.py
```

또는 Windows에서 `run_assign_map.bat`을 실행합니다. 기본 주소는 `http://127.0.0.1:8000`입니다.

## 데이터 처리 흐름

1. 관리자 화면에서 센터와 월이 포함된 오더 엑셀을 업로드합니다.
2. `app.py`가 주소를 정규화하고 기존 지오코딩 캐시를 우선 사용합니다.
3. 캐시에 없는 주소는 네이버 지오코딩 API로 좌표를 조회합니다.
4. 좌표를 연속지적도 필지에 매칭합니다.
5. 실제 공유 경계가 있는 필지를 우선 연결하고, 도로 경계 및 교통 장벽을 반영해 assignment block을 생성합니다.
6. 생성된 블럭의 인접 관계와 좌표를 `processed_assignment_blocks.json`으로 제공합니다.
7. 지도 화면에서 센터별 인원 목표를 계산하고 순차 배정합니다.
8. 결과는 지도에 표시하거나 엑셀로 저장합니다.

운영 데이터와 SHP 원본은 용량과 개인정보 문제로 저장소에 포함하지 않습니다. 로컬 입력 경로는 `block_geocodes/`, `road/`, `transport/`, `dong/`입니다.

## SHP 기반 블럭 생성

```powershell
cd C:\DA\assign_map
python 07.06\preprocess_transport_blocks.py `
  --input process_geocode.json `
  --output processed_assignment_blocks.json
```

현재 블럭 연결은 단순 중심점 거리만으로 병합하지 않습니다. 실제 필지 경계 공유 길이, 경계 간 거리, 도로/교통 장벽, 법정동 경계를 함께 사용합니다. `touches`는 실제로 맞닿은 경계 중심, `near`는 생활도로 등 좁은 간격을 둔 후보, 장벽을 가로지르는 관계는 일반 순로에서 제외할 수 있는 메타데이터로 구분합니다.

## 배정 로직 기준

- 인원별 오더 목표는 전체 배정 가능 오더를 인원 수로 나누어 계산합니다.
- 목표량 편차는 기본적으로 ±10% 범위에 맞추도록 합니다.
- 주소 간 이동이 500m를 넘으면 일반 배정에서 제한하고 잔여 오더로 분리합니다.
- 순로는 블럭 단위로 고정하고, 블럭 내부 주소는 진입 방향과 좌표를 기준으로 순서화합니다.
- 색 혼합을 줄이기 위해 원칙적으로 블럭 전체를 하나의 배정 단위로 취급합니다.
- 목표량 때문에 분할이 불가피한 큰 블럭만 내부의 연속된 주소 묶음으로 나눌 수 있습니다.

현재 운영 화면에는 기존 배정 로직이 포함되어 있고, 새 route-neighbor/block-unit 방식은 별도 검증 HTML에서 먼저 확인합니다. 검증 결과가 기준을 만족하면 운영 화면에 통합하는 방식으로 관리합니다.

## 검증 시각화

H072/H074의 SHP 블럭 경계를 확인합니다.

```powershell
python boundary_map\generate_selected_block_maps.py --center H072 H074
```

순로, route용 neighbor, 제외 edge, component/cluster, 인원별 블럭 배정을 확인합니다.

```powershell
python boundary_map\generate_route_debug_maps.py --center H072 H074
```

생성 파일은 `boundary_map/assignment_blocks_H072.html`, `boundary_map/assignment_blocks_H074.html`, `boundary_map/debug_route_H072.html`, `boundary_map/debug_route_H074.html`입니다.

검증 화면에서는 블럭 경계, route neighbor, 교통 장벽 제외 edge, component ID, 500m cluster, route 번호, 인원별 배정 블럭, 색 혼합 여부, 잔여 사유를 확인합니다.

## 엑셀 결과

지도 화면의 엑셀 저장 결과는 다음 형식입니다.

```text
H074_2026.07_인원배정.xlsx
```

내보내기에는 센터, 월, 주소, 도로명/지번 정보, 동, 인원, 오더 정보가 포함되며 내부 매칭용 `block_id`, `match_type`, 거리, 잔여 사유 같은 기술 필드는 제외합니다.

## Git 및 보안

저장소에는 애플리케이션 코드와 재현에 필요한 처리 스크립트만 올립니다. `api_keys.json`, `.env`, `admin/`, `uploaded_workbooks/`, `block_geocodes/`, 실행파일, 로그, Python 캐시는 커밋하지 않습니다. 주소/좌표 등 개인정보가 포함된 데이터는 별도 보안 저장소를 사용합니다.
