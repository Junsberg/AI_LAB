# Claude Code statusline (로컬 CLI 전용)

3줄 상태줄: 라임 모델 배지 / 컨텍스트 수치 / 5시간·주간 사선 게이지. 클라우드 세션에는 적용되지 않는다.

## 연결 (로컬 PC에서 한 번)
`~/.claude/settings.json` (Windows: `%USERPROFILE%\.claude\settings.json`) 에 추가. 기존 `statusLine`이 있으면 먼저 백업.

```json
{
  "statusLine": {
    "type": "command",
    "command": "python C:/path/to/AI_LAB/tools/statusline/statusline.py",
    "refreshInterval": 5
  }
}
```

Python 3.11+ 필요. 특수문자가 깨지는 터미널이면 `STATUSLINE_ASCII=1` 환경변수로 ASCII 대체.

## 로컬 세션에서 할 말
> `tools/statusline/README.md` 대로 statusline 연결해줘. 연결 후 실제 stdin JSON 필드명이 코드와 다르면 코드를 맞춰줘.

## 테스트
```
python tools/statusline/tests/run.py
```
0% / 47% / 90% / 100% / 누락 / 긴 브랜치 / 좁은 화면 케이스를 출력·폭 검사한다.

## 데이터 출처 (stdin JSON)
- `model.display_name`, `effort.level`, `workspace.current_dir`, `cost.total_duration_ms`
- `context_window.used_percentage`, `context_window.context_window_size`, `context_window.current_usage.{input_tokens,cache_creation_input_tokens,cache_read_input_tokens}`
- `rate_limits.five_hour.{used_percentage,resets_at}`, `rate_limits.seven_day.{used_percentage,resets_at}`

없는 값은 항목과 구분자를 함께 숨긴다. null은 0%로 만들지 않는다.
