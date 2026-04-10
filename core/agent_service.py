import json
import os
from typing import Any
from urllib import request

from .models import AgentActivity, AuraSession, ThoughtLog


INVENTORY_MAP = {
    1: {"name": "headphones", "stock": 12},
    2: {"name": "microphone", "stock": 7},
    3: {"name": "webcam", "stock": 4},
}


def _split_thinking_block(content: str) -> tuple[str, str]:
    start_tag = "<|think|>"
    end_tag = "</|think|>"

    if start_tag in content and end_tag in content:
        start = content.index(start_tag) + len(start_tag)
        end = content.index(end_tag)
        thought = content[start:end].strip()
        final = (content[: content.index(start_tag)] + content[end + len(end_tag) :]).strip()
        return thought, final

    return "", content.strip()


def _tool_check_inventory(arguments: dict[str, Any]) -> dict[str, Any]:
    item_id = arguments.get("item_id")
    if item_id is None:
        return {"ok": False, "error": "item_id is required"}

    product = INVENTORY_MAP.get(int(item_id))
    if not product:
        return {"ok": False, "error": f"Item {item_id} not found"}

    return {
        "ok": True,
        "item_id": int(item_id),
        "name": product["name"],
        "stock": product["stock"],
    }


def _tool_update_user_mood(arguments: dict[str, Any]) -> dict[str, Any]:
    score = arguments.get("sentiment_score")
    if score is None:
        return {"ok": False, "error": "sentiment_score is required"}
    return {"ok": True, "sentiment_score": float(score)}


def _execute_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "check_inventory":
        return _tool_check_inventory(arguments)
    if tool_name == "update_user_mood":
        return _tool_update_user_mood(arguments)
    return {"ok": False, "error": f"Unknown tool: {tool_name}"}


def _openai_chat_completion(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0,
    }
    req = request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_agent_turn(session_uuid: str, user_prompt: str) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = os.getenv("OPENAI_AGENT_MODEL", "gpt-4o-mini")

    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY is missing"}

    tool_schemas = [
        {
            "type": "function",
            "function": {
                "name": "check_inventory",
                "description": "Fetch current stock for an item id from the app inventory map.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer"},
                    },
                    "required": ["item_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_user_mood",
                "description": "Log user's sentiment score between -1 and 1.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sentiment_score": {"type": "number", "minimum": -1, "maximum": 1},
                    },
                    "required": ["sentiment_score"],
                },
            },
        },
    ]

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are Aura-Stream. Use tools when needed. "
                "If you reason internally, wrap it in <|think|>...</|think|> and then provide final answer."
            ),
        },
        {"role": "user", "content": user_prompt},
    ]

    session = AuraSession.objects.get(uuid=session_uuid)

    for _ in range(4):
        try:
            response = _openai_chat_completion(
                api_key=api_key,
                base_url=base_url,
                model=model,
                messages=messages,
                tools=tool_schemas,
            )
        except Exception as exc:
            return {"ok": False, "error": f"OpenAI request failed: {exc}"}

        choices = response.get("choices", [])
        if not choices:
            return {"ok": False, "error": "OpenAI response had no choices"}

        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls") or []

        if tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": [
                        {
                            "id": tc.get("id"),
                            "type": tc.get("type"),
                            "function": {
                                "name": tc.get("function", {}).get("name"),
                                "arguments": tc.get("function", {}).get("arguments"),
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            for tc in tool_calls:
                try:
                    arguments = json.loads(tc.get("function", {}).get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}

                tool_name = tc.get("function", {}).get("name", "")
                result = _execute_tool(tool_name, arguments)
                AgentActivity.objects.create(session=session, tool_called=tool_name, result=result)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": json.dumps(result),
                    }
                )
            continue

        raw = message.get("content") or ""
        thought, final = _split_thinking_block(raw)
        ThoughtLog.objects.create(session=session, thought_block=thought, final_response=final)

        return {
            "ok": True,
            "model": model,
            "thought": thought,
            "final_response": final,
        }

    return {"ok": False, "error": "Agent loop exceeded max iterations"}
