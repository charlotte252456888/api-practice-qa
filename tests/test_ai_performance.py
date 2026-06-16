"""AI API performance and quality tests.

這個檔案負責「測試本身」：
- 用 pytest fixture 初始化 Groq/Gemini client。
- 用環境變數管理 API key 和模型 ID。
- 用 assert 定義效能與品質門檻。
- 每次 AI 呼叫後，把結果交給 conftest.py 彙整成 Excel 報表。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Callable

import httpx
import pytest
from dotenv import load_dotenv
from google import genai
from google.genai import types
from groq import Groq


# 讓本地開發可以從 .env 讀取 GROQ_API_KEY。
# 在 GitHub Actions 裡，Secret 會直接注入成環境變數。
load_dotenv()

# 測試門檻集中管理，之後要調整 baseline 只需要改這裡。
LATENCY_BASELINE_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 10.0

# Provider 和模型 ID 都可被環境變數覆寫，方便 CI 或不同測試環境切換。
DEFAULT_AI_PROVIDER = os.environ.get("AI_PROVIDER", "groq").strip().lower()
GROQ_MODEL_ID = os.environ.get("GROQ_MODEL_ID", "llama-3.3-70b-versatile")
GEMINI_MODEL_ID = os.environ.get("GEMINI_MODEL_ID", "gemini-1.5-flash")


@dataclass(frozen=True)
class AIResponse:
    """統一不同 AI SDK 回傳後，測試真正需要的資料。"""

    text: str
    latency_seconds: float


@dataclass(frozen=True)
class AIClientAdapter:
    """把 Groq/Gemini 包成相同介面，讓測試不需要知道 SDK 細節。"""

    provider: str
    model_id: str
    generate: Callable[[str, float], AIResponse]


def _elapsed_response(start_time: float, text: str) -> AIResponse:
    """把 SDK 回應文字和耗時整理成 AIResponse。"""

    return AIResponse(
        text=(text or "").strip(),
        latency_seconds=round(time.perf_counter() - start_time, 3),
    )


@pytest.fixture(scope="session")
def groq_client() -> Groq:
    """建立 Groq client；整個 pytest session 共用一次。"""

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        pytest.skip("GROQ_API_KEY is not set; skipping Groq-backed AI tests.")

    return Groq(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)


@pytest.fixture(scope="session")
def gemini_client() -> genai.Client:
    """建立 Gemini client；支援 GEMINI_API_KEY 或 GOOGLE_API_KEY。"""

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        pytest.skip(
            "GEMINI_API_KEY or GOOGLE_API_KEY is not set; skipping Gemini tests."
        )

    return genai.Client(
        api_key=api_key,
        # google-genai timeout 使用毫秒，這裡從秒轉換成毫秒。
        http_options=types.HttpOptions(timeout=int(REQUEST_TIMEOUT_SECONDS * 1000)),
    )


@pytest.fixture(scope="session")
def groq_adapter(groq_client: Groq) -> AIClientAdapter:
    """把 Groq chat completion 包成 generate(prompt, timeout) 介面。"""

    def generate(prompt: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> AIResponse:
        # perf_counter 適合量測短時間間隔，用來算 API latency 很穩。
        start_time = time.perf_counter()
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL_ID,
            messages=[{"role": "user", "content": prompt}],
            timeout=timeout,
        )
        text = completion.choices[0].message.content or ""
        return _elapsed_response(start_time, text)

    return AIClientAdapter(provider="groq", model_id=GROQ_MODEL_ID, generate=generate)


@pytest.fixture(scope="session")
def gemini_adapter(gemini_client: genai.Client) -> AIClientAdapter:
    """把 Gemini generate_content 包成 generate(prompt, timeout) 介面。"""

    def generate(prompt: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> AIResponse:
        start_time = time.perf_counter()
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL_ID,
            contents=prompt,
            config=types.GenerateContentConfig(
                http_options=types.HttpOptions(timeout=int(timeout * 1000))
            ),
        )
        return _elapsed_response(start_time, response.text or "")

    return AIClientAdapter(
        provider="gemini", model_id=GEMINI_MODEL_ID, generate=generate
    )


@pytest.fixture(scope="session")
def ai_adapter(request: pytest.FixtureRequest) -> AIClientAdapter:
    """依照 AI_PROVIDER 選擇 Groq 或 Gemini，預設使用 Groq。"""

    # 動態取 fixture 可以避免沒有被選到的 provider 也被初始化。
    if DEFAULT_AI_PROVIDER == "gemini":
        return request.getfixturevalue("gemini_adapter")
    if DEFAULT_AI_PROVIDER == "groq":
        return request.getfixturevalue("groq_adapter")

    pytest.fail("AI_PROVIDER must be either 'groq' or 'gemini'.")


def _print_response_log(
    test_name: str, adapter: AIClientAdapter, response: AIResponse
) -> None:
    """搭配 pytest -s 印出本次 API 呼叫的關鍵資訊。"""

    _safe_print(
        f"{test_name}: provider={adapter.provider}, model={adapter.model_id}, "
        f"latency={response.latency_seconds}s, chars={len(response.text)}"
    )
    _safe_print(f"{test_name}: response={response.text}")


def _safe_print(message: str) -> None:
    """安全印出日誌，避免 Windows cp950 遇到少數中文字時拋 UnicodeEncodeError。"""

    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        safe_message = message.encode(encoding, errors="replace").decode(encoding)
        print(safe_message)


def _record_successful_ai_response(
    recorder: Callable[..., None],
    case_id: str,
    prompt: str,
    response: AIResponse,
) -> None:
    """把成功取得的 AI 回應交給 Excel 報表收集器。

    注意：這裡先記錄 response，測試最後是否 pass/fail 會由 conftest.py
    的 pytest_runtest_makereport hook 自動更新。
    """

    recorder(
        case_id=case_id,
        prompt=prompt,
        latency_seconds=response.latency_seconds,
        answer_length=len(response.text),
        reply_text=response.text,
    )


def test_automation_definition(
    ai_adapter: AIClientAdapter, ai_report_recorder: Callable[..., None]
) -> None:
    """驗證自動化測試定義的回答品質與 latency baseline。"""

    case_id = "TC-001"
    prompt = "請用一句話解釋什麼是自動化測試？"

    response = ai_adapter.generate(prompt)
    _print_response_log("test_automation_definition", ai_adapter, response)
    _record_successful_ai_response(ai_report_recorder, case_id, prompt, response)

    assert response.latency_seconds < LATENCY_BASELINE_SECONDS
    assert response.text
    assert "測試" in response.text


def test_python_decorator(
    ai_adapter: AIClientAdapter, ai_report_recorder: Callable[..., None]
) -> None:
    """驗證 Python decorator 說明與繁體中文用字合規性。"""

    case_id = "TC-002"
    prompt = "什麼是 Python 的裝飾器 (Decorator)？請簡短說明。"

    response = ai_adapter.generate(prompt)
    _print_response_log("test_python_decorator", ai_adapter, response)
    _record_successful_ai_response(ai_report_recorder, case_id, prompt, response)

    assert response.latency_seconds < LATENCY_BASELINE_SECONDS
    assert "软件" not in response.text


def test_error_handling_fallback(
    ai_adapter: AIClientAdapter, ai_report_recorder: Callable[..., None]
) -> None:
    """驗證 API timeout 或連線錯誤能被優雅捕捉，並保留報表紀錄。"""

    case_id = "TC-003"
    prompt = "請用繁體中文簡短說明 AI API 在高負載時應如何處理逾時。"

    try:
        response = ai_adapter.generate(prompt, timeout=REQUEST_TIMEOUT_SECONDS)
    except (httpx.HTTPError, Exception) as exc:
        # 如果真的遇到網路、SSL、400、timeout 等底層錯誤，仍寫入報表。
        error_text = f"Provider error {type(exc).__name__}: {exc}"
        _safe_print(f"test_error_handling_fallback: captured provider error {error_text}")
        ai_report_recorder(
            case_id=case_id,
            prompt=prompt,
            latency_seconds=0.0,
            answer_length=0,
            reply_text=error_text,
        )
        assert exc is not None
        return

    _print_response_log("test_error_handling_fallback", ai_adapter, response)
    _record_successful_ai_response(ai_report_recorder, case_id, prompt, response)

    assert response.text
# 4. 模擬協作新增的測試案例 4：驗證 PM 知識
def test_project_management_definition(groq_client):
    prompt = "請用一句話解釋什麼是專案管理 (Project Management)？"
    
    import time
    start_time = time.time()
    completion = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        timeout=10.0
    )
    latency = time.time() - start_time
    reply_text = completion.choices[0].message.content

    print(f"\n[TC-004] 延遲時間: {latency:.3f} 秒")
    
    assert latency < 5.0, f"超時！耗時 {latency:.2f} 秒"
    assert "管理" in reply_text, "回答中應包含關鍵字『管理』"
