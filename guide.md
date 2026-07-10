# GitHub 팀 협업 가이드 (초보자용)

> 이 문서는 GitHub이 처음인 팀원 4명이 함께 프로젝트를 진행하기 위한 가이드입니다.
> 순서대로 따라하면 됩니다.

---

## 0. 시작 전에 알아둘 개념 4가지

| 용어 | 쉬운 설명 |
|---|---|
| **Repository (저장소)** | 프로젝트가 저장되는 폴더. 이 안에 모든 코드와 변경 이력이 담김 |
| **Commit (커밋)** | 변경한 내용을 "저장"하는 것. "여기까지 작업 완료"라는 스냅샷 |
| **Branch (브랜치)** | 원본을 건드리지 않고 안전하게 작업할 수 있는 "복사본 공간" |
| **Pull Request (PR)** | "제 작업 확인하고 원본에 합쳐주세요"라고 요청하는 것 |

비유하자면: `main` 브랜치는 **팀 전체가 보는 완성본**이고, 각자의 `branch`는 **개인 작업 노트**입니다. 작업 노트에서 충분히 다듬은 후 PR로 "이거 완성본에 합쳐도 될까요?"라고 요청하는 흐름입니다.

---

## 1. 사전 준비 (팀원 4명 모두, 한 번만 하면 됨)

### 1-1. GitHub 계정 만들기
[github.com](https://github.com) 에서 회원가입

### 1-2. Git 설치
- Windows: [git-scm.com](https://git-scm.com/downloads) 에서 다운로드 후 설치
- Mac: 터미널에 `git --version` 입력 → 없으면 자동으로 설치 안내가 뜸

### 1-3. Git에 내 정보 등록
터미널(또는 Git Bash)을 열고 아래 명령어 입력:
```bash
git config --global user.name "본인 이름"
git config --global user.email "본인 GitHub 가입 이메일"
```

### 1-4. (선택이지만 추천) GitHub Desktop 사용
명령어가 아직 어색하다면 **GitHub Desktop** 앱을 쓰면 클릭 몇 번으로 대부분의 작업이 가능합니다.
- 다운로드: [desktop.github.com](https://desktop.github.com)
- 이 문서는 명령어 기준으로 설명하지만, 같은 동작을 GitHub Desktop의 버튼으로도 할 수 있습니다.

---

## 2. 저장소 만들고 팀원 초대하기 (팀장 또는 1명만)

1. GitHub 우측 상단 **+** → **New repository** 클릭
2. 저장소 이름 입력 (예: `team-project`)
3. **Public/Private** 선택 (팀 프로젝트면 보통 Private 추천)
4. **Add a README file** 체크 → Create repository

### 팀원 초대하기
1. 저장소 페이지 → **Settings** → **Collaborators**
2. **Add people** 클릭 → 팀원 GitHub 아이디 또는 이메일 입력
3. 초대받은 팀원은 이메일 또는 GitHub 알림에서 **Accept invitation** 클릭

---

## 3. 내 컴퓨터로 저장소 가져오기 (팀원 4명 모두)

```bash
git clone https://github.com/팀장아이디/team-project.git
cd team-project
```
이 폴더가 앞으로 작업할 공간입니다.

---

## 4. 실제 작업 흐름 (매번 작업할 때마다 반복)

### 4단계 기본 사이클
```
① 브랜치 만들기 → ② 작업 & 커밋 → ③ 업로드(push) → ④ PR 만들기
```

### ① 새 브랜치 만들기
작업 시작 전 항상 새 브랜치를 만듭니다. (이름은 본인+작업내용으로)
```bash
git checkout -b hana/data-preprocessing
```
> 예시: `이름/작업내용` 형식 추천 → `minsu/model-training`, `jiwoo/readme-update`

### ② 작업 후 커밋하기
```bash
git add .
git commit -m "데이터 전처리 코드 추가"
```
- `git add .` : 변경한 파일을 커밋 대상으로 등록
- `git commit -m "..."` : "여기까지 작업 완료"라고 기록 (메시지는 무엇을 했는지 간단히)

### ③ GitHub에 업로드(push)
```bash
git push origin hana/data-preprocessing
```

### ④ Pull Request(PR) 만들기
1. GitHub 저장소 페이지 접속 → 방금 push한 브랜치 알림 배너에서 **Compare & pull request** 클릭
2. 제목/설명 작성 (뭘 했는지 간단히)
3. **Create pull request** 클릭
4. 팀원이 코드 확인 후 **Merge pull request** 클릭하면 `main`에 합쳐짐

---

## 5. 매일 작업 시작 전 꼭 하기

다른 팀원이 작업한 내용을 내 컴퓨터로 먼저 받아옵니다.
```bash
git checkout main
git pull origin main
```
이후 새 작업을 시작할 때 다시 `git checkout -b 새브랜치` 로 시작합니다.

> **왜 필요한가요?** 팀원이 이미 고친 부분을 모르고 작업하면 나중에 충돌(conflict)이 생기기 쉽습니다. 매일 시작 전에 최신 상태로 맞춰두면 이런 문제가 줄어듭니다.

---

## 6. 충돌(Conflict)이 생겼을 때

같은 파일의 같은 부분을 두 사람이 동시에 고치면 발생합니다. 당황하지 않아도 됩니다.

1. 충돌난 파일을 열면 아래처럼 표시됩니다:
```
<<<<<<< HEAD
내 코드
=======
팀원 코드
>>>>>>> branch-name
```
2. 둘 중 맞는 코드만 남기고 `<<<<<<<`, `=======`, `>>>>>>>` 표시는 전부 지웁니다.
3. 저장 후 다시 커밋:
```bash
git add .
git commit -m "충돌 해결"
git push
```

---

## 7. 팀 규칙 정하기 (프로젝트 시작 전 4명이 같이 정하면 좋음)

### 브랜치 이름 규칙
| 접두사 | 용도 |
|---|---|
| `feature/` | 새 기능 추가 |
| `fix/` | 오류 수정 |
| `docs/` | 문서 작업 |

### 커밋 메시지 규칙 (간단 버전)
```
어떤 작업을 했는지 한 줄로 명확하게

예)
전처리 함수 구현
README에 실행 방법 추가
모델 학습 코드 오류 수정
```

### PR 규칙
- 본인이 만든 PR은 **본인이 직접 Merge하지 않고** 다른 팀원 1명 이상 확인 후 Merge
- 작업량이 크면 PR을 잘게 나눠서 자주 올리기 (한 번에 몰아서 X)

---

## 8. GitHub으로 프로젝트(할 일) 관리하기

### 8-1. Issues — 할 일 등록
저장소 → **Issues** 탭 → **New issue**
```
제목: 데이터 전처리 함수 작성
내용: 결측치 처리 + 텍스트 정규화 필요
담당자: 팀원 지정 (Assignees)
```
할 일 하나하나를 이슈로 등록하면, 누가 뭘 맡았는지 한눈에 보입니다.

### 8-2. Projects — 칸반 보드로 진행 상황 보기
저장소 → **Projects** 탭 → **New project** → **Board** 템플릿 선택

```
To Do  |  In Progress  |  Done
-------|---------------|-------
전처리   |   모델링        |  주제 선정
```
이슈를 만들면 이 보드에 카드로 끌어다 놓을 수 있고, 진행 상태에 따라 컬럼을 옮기며 관리합니다.

### 8-3. Issue와 PR 연결하기
PR 설명에 아래처럼 쓰면, PR이 merge될 때 이슈가 자동으로 닫힙니다.
```
Closes #3
```
(3번은 이슈 번호)

---

## 9. 자주 하는 실수 & 해결법

| 상황 | 해결 |
|---|---|
| `main`에서 바로 작업해버림 | 커밋 전이라면 `git stash` 후 새 브랜치 만들어서 `git stash pop` |
| 커밋 메시지 오타 | `git commit --amend -m "새 메시지"` (단, push 전에만) |
| push했는데 브랜치를 잘못 팠음 | 팀원에게 알리고 새 브랜치로 다시 PR 생성 |
| 실수로 중요 파일 삭제 | `git checkout -- 파일명` 으로 마지막 커밋 상태 복원 |

---

## 10. 명령어 요약 치트시트

```bash
git clone [주소]              # 저장소 처음 받아오기
git checkout -b [브랜치명]     # 새 브랜치 만들고 이동
git status                    # 지금 상태 확인
git add .                     # 변경사항 등록
git commit -m "메시지"         # 커밋(저장)
git push origin [브랜치명]     # GitHub에 업로드
git checkout main             # main 브랜치로 이동
git pull origin main          # 최신 내용 받아오기
```

---

## 요약: 매일 작업 루틴

```
1. git checkout main && git pull origin main   (최신화)
2. git checkout -b 이름/작업내용                 (새 브랜치)
3. 작업하기
4. git add . && git commit -m "메시지"
5. git push origin 이름/작업내용
6. GitHub에서 PR 생성 → 팀원 리뷰 → Merge
```
