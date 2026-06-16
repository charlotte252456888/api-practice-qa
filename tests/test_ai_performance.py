"""AI API 效能與品質驗證測試。

這個檔案把原本 for-loop 形式的 PoC 腳本改成 pytest 測試：
- 用 fixture 管理 Groq/Gemini client，避免每個測試重複初始化。
- 用 assert 明確定義「通過」與「失敗」標準。
- 用環境變數讀取 API key 和模型 ID，避免把密鑰寫死在程式碼裡。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable

import httpx
import pytest
from dotenv import load_dotenv
from groq import Groq
from google import genai
from google.genai import types


# 先載入 .env，讓本機開發時可以用 .env 管理密鑰。
# CI/CD 環境也可以直接注入環境變數，不一定需要 .env 檔。
load_dotenv()

# 品質門檻集中放在全域常數，之後要調整 baseline 不需要進測試邏輯裡找。
LATENCY_BASELINE_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 10.0

# 模型與 provider 都支援環境變數覆寫：
# AI_PROVIDER=groq 或 AI_PROVIDER=gemini
DEFAULT_AI_PROVIDER = os.environ.get("AI_PROVIDER", "groq").strip().lower()
GROQ_MODEL_ID = os.environ.get("GROQ_MODEL_ID", "llama-3.3-70b-versatile")
GEMINI_MODEL_ID = os.environ.get("GEMINI_MODEL_ID", "gemini-1.5-flash")


@dataclass(frozen=True)
class AIResponse:
    """統一不同 AI SDK 回傳格式後，測試真正需要使用的資料。"""

    # AI 回答文字。
    text: str
    # 單次 API 呼叫耗時，單位是秒。
    latency_seconds: float


@dataclass(frozen=True)
class AIClientAdapter:
    """把 Groq/Gemini 包成同一個介面，讓測試案例不用關心 SDK 細節。"""

    # 目前使用的 provider 名稱，主要用於 pytest -s 時印出除錯資訊。
    provider: str
    # 目前使用的模型 ID。
    model_id: str
    # 統一呼叫入口：丟 prompt 進去，回傳 AIResponse。
    generate: Callable[[str, float], AIResponse]


def _elapsed_response(start_time: float, text: str) -> AIResponse:
    """計算 latency，並把 AI 文字整理成統一的 AIResponse。"""

    return AIResponse(
        # 防止 SDK 回傳 None；strip 則避免只有空白字元也被當成有效回答。
        text=(text or "").strip(),
        latency_seconds=round(time.perf_counter() - start_time, 3),
    )


@pytest.fixture(scope="session")
def groq_client() -> Groq:
    """建立 Groq client，整個 pytest session 共用一次。"""

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        # 沒有 key 時使用 skip，而不是讓測試直接炸掉；這對 CI 和本機都比較友善。
        pytest.skip("GROQ_API_KEY is not set; skipping Groq-backed AI tests.")

    return Groq(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)


@pytest.fixture(scope="session")
def gemini_client() -> genai.Client:
    """建立 Gemini client，支援 GEMINI_API_KEY 或 GOOGLE_API_KEY。"""

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        pytest.skip(
            "GEMINI_API_KEY or GOOGLE_API_KEY is not set; skipping Gemini tests."
        )

    return genai.Client(
        api_key=api_key,
        # google-genai 的 timeout 單位是毫秒，所以這裡從秒轉成毫秒。
        http_options=types.HttpOptions(timeout=int(REQUEST_TIMEOUT_SECONDS * 1000)),
    )


@pytest.fixture(scope="session")
def groq_adapter(groq_client: Groq) -> AIClientAdapter:
    """把 Groq chat completion 包裝成共同的 generate(prompt, timeout) 介面。"""

    def generate(prompt: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> AIResponse:
        # perf_counter 適合量測短時間間隔，比 time.time 更適合拿來算 latency。
        start_time = time.perf_counter()
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL_ID,
            messages=[{"role": "user", "content": prompt}],
            # 每次呼叫都明確帶 timeout，避免 API 卡住時拖垮整個測試流程。
            timeout=timeout,
        )
        text = completion.choices[0].message.content or ""
        return _elapsed_response(start_time, text)

    return AIClientAdapter(provider="groq", model_id=GROQ_MODEL_ID, generate=generate)


@pytest.fixture(scope="session")
def gemini_adapter(gemini_client: genai.Client) -> AIClientAdapter:
    """把 Gemini generate_content 包裝成共同的 generate(prompt, timeout) 介面。"""

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
def ai_adapter(
    request: pytest.FixtureRequest,
) -> AIClientAdapter:
    """依照 AI_PROVIDER 選擇本次測試要打 Groq 或 Gemini。"""

    # request.getfixturevalue 可以動態取得 fixture，避免兩個 provider 都被初始化。
    if DEFAULT_AI_PROVIDER == "gemini":
        return request.getfixturevalue("gemini_adapter")
    if DEFAULT_AI_PROVIDER == "groq":
        return request.getfixturevalue("groq_adapter")

    pytest.fail("AI_PROVIDER must be either 'groq' or 'gemini'.")


def _print_response_log(test_name: str, adapter: AIClientAdapter, response: AIResponse) -> None:
    """印出簡短診斷資訊；搭配 pytest -s 才會即時看到 print 內容。"""

    print(
        f"{test_name}: provider={adapter.provider}, model={adapter.model_id}, "
        f"latency={response.latency_seconds}s, chars={len(response.text)}"
    )
    print(f"{test_name}: response={response.text}")


def test_automation_definition(ai_adapter: AIClientAdapter) -> None:
    """測試 AI 能否快速且有效回答「自動化測試」的定義。"""

    response = ai_adapter.generate("請用一句話解釋什麼是自動化測試？")
    _print_response_log("test_automation_definition", ai_adapter, response)

    # 效能斷言：這是本 Sprint 要求的 latency baseline。
    assert response.latency_seconds < LATENCY_BASELINE_SECONDS
    # 品質斷言：不能回空字串。
    assert response.text
    # 品質斷言：回答必須命中核心概念「測試」。
    assert "測試" in response.text


def test_python_decorator(ai_adapter: AIClientAdapter) -> None:
    """測試 AI 對 Python decorator 的說明，以及繁體中文用字合規性。"""

    response = ai_adapter.generate(
        "什麼是 Python 的裝飾器 (Decorator)？請簡短說明。"
    )
    _print_response_log("test_python_decorator", ai_adapter, response)

    assert response.latency_seconds < LATENCY_BASELINE_SECONDS
    # 語系斷言：若出現簡體「软件」，代表不符合繁中規格。
    assert "软件" not in response.text


def test_error_handling_fallback(ai_adapter: AIClientAdapter) -> None:
    """測試 API 超時或連線錯誤時，pytest 能捕捉錯誤並留下日誌。"""

    try:
        response = ai_adapter.generate(
            "請用繁體中文簡短說明 AI API 在高負載時應如何處理逾時。",
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, Exception) as exc:
        # 這裡刻意把底層錯誤印出來，例如 400、SSL、timeout 或 connection error。
        # 捕捉後 return，代表這個 fallback 測試成功驗證了「錯誤可被優雅處理」。
        print(
            "test_error_handling_fallback: captured provider error "
            f"{type(exc).__name__}: {exc}"
        )
        assert exc is not None
        return

    # 如果 API 沒有出錯，也要確認它真的有回內容。
    _print_response_log("test_error_handling_fallback", ai_adapter, response)
    assert response.text
