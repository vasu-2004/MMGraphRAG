from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..core.base import logger


def _load_project_env() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("python-dotenv is not installed; skipping .env loading from %s", env_path)
        return

    load_dotenv(env_path, override=True)


_load_project_env()


GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip().strip('"')
PROJECT_ID = os.environ.get("MODEL_PROJECT_ID", "").strip().strip('"')
LOCATION = os.environ.get("MODEL_LOCATION", "us-central1").strip().strip('"')
MODEL_NAME = "gemini-2.5-flash"

if PROJECT_ID:
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", PROJECT_ID)
    os.environ.setdefault("GCLOUD_PROJECT", PROJECT_ID)


def _extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
            else:
                text_parts.append(str(item))
        return "\n".join(part for part in text_parts if part)
    return str(content or "")


@lru_cache(maxsize=1)
def _get_message_types():
    try:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    except ImportError as exc:
        raise RuntimeError(
            "langchain-google-genai and langchain-core are required. "
            "Install with: pip install langchain-google-genai google-cloud-aiplatform google-generativeai python-dotenv"
        ) from exc

    return SystemMessage, HumanMessage, AIMessage


@lru_cache(maxsize=1)
def get_chat_client():
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError as exc:
        raise RuntimeError(
            "langchain-google-genai is required. "
            "Install with: pip install langchain-google-genai google-cloud-aiplatform google-generativeai python-dotenv"
        ) from exc

    if GOOGLE_API_KEY:
        return ChatGoogleGenerativeAI(
            model=MODEL_NAME,
            google_api_key=GOOGLE_API_KEY,
            max_output_tokens=1000,
        )

    if PROJECT_ID:
        import google.auth
        import google.auth.transport.requests

        try:
            creds, _ = google.auth.default(
                scopes=[
                    "https://www.googleapis.com/auth/cloud-platform",
                    "https://www.googleapis.com/auth/generative-language",
                ]
            )
            creds.refresh(google.auth.transport.requests.Request())
        except Exception as exc:
            raise RuntimeError(
                "ADC refresh failed. Run: gcloud auth application-default login"
            ) from exc

        return ChatGoogleGenerativeAI(
            model=MODEL_NAME,
            credentials=creds,
            project=PROJECT_ID,
            location=LOCATION,
            max_output_tokens=2048,
        )

    raise RuntimeError(
        "Neither GOOGLE_API_KEY nor MODEL_PROJECT_ID is set. "
        "Set one of them in .env or the shell environment."
    )


def build_text_messages(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list[dict] | None = None,
):
    SystemMessage, HumanMessage, AIMessage = _get_message_types()
    messages: list[Any] = []

    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))

    for message in history_messages or []:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "system":
            messages.append(SystemMessage(content=_extract_text_content(content)))
        elif role == "assistant":
            messages.append(AIMessage(content=_extract_text_content(content)))
        else:
            messages.append(HumanMessage(content=content))

    messages.append(HumanMessage(content=prompt))
    return messages


def build_multimodal_messages(
    user_prompt: str,
    img_base: str,
    system_prompt: str,
    history_messages: list[dict] | None = None,
    image_mime: str = "image/jpeg",
):
    SystemMessage, HumanMessage, AIMessage = _get_message_types()
    messages: list[Any] = []

    for message in history_messages or []:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "system":
            messages.append(SystemMessage(content=_extract_text_content(content)))
        elif role == "assistant":
            messages.append(AIMessage(content=_extract_text_content(content)))
        else:
            messages.append(HumanMessage(content=content))

    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))

    messages.append(
        HumanMessage(
            content=[
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{img_base}"}},
            ]
        )
    )
    return messages


def build_messages_from_openai_style(messages: list[dict]):
    SystemMessage, HumanMessage, AIMessage = _get_message_types()
    converted: list[Any] = []

    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "system":
            converted.append(SystemMessage(content=_extract_text_content(content)))
        elif role == "assistant":
            converted.append(AIMessage(content=_extract_text_content(content)))
        else:
            converted.append(HumanMessage(content=content))

    return converted


def extract_response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    return _extract_text_content(content).strip()


async def ainvoke_messages(messages: list[Any]) -> str:
    response = await get_chat_client().ainvoke(messages)
    return extract_response_text(response)


def invoke_messages(messages: list[Any]) -> str:
    response = get_chat_client().invoke(messages)
    return extract_response_text(response)


def invoke_openai_messages(messages: list[dict]) -> str:
    return invoke_messages(build_messages_from_openai_style(messages))