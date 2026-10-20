"""Explicit Responses -> Chat Completions translation, including streaming tools."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid


class ProtocolError(ValueError):
    pass


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex


def tool_identity(name, namespace=None):
    return f"{namespace}.{name}" if namespace else name


def safe_name(name):
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        return name
    return "tool_" + hashlib.sha256(name.encode()).hexdigest()[:32]


def content_parts(content, role="user"):
    if isinstance(content, str):
        return content
    result = []
    for part in content or []:
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            result.append({"type": "text", "text": part.get("text", "")})
        elif kind in ("input_image", "image_url") and role == "user":
            image = part.get("image_url")
            if isinstance(image, str):
                image = {"url": image}
            if not image:
                raise ProtocolError("Chat Completions 适配器需要可用的图片 URL 或 data URL。")
            result.append({"type": "image_url", "image_url": image})
        elif kind == "refusal":
            result.append({"type": "text", "text": part.get("refusal", "")})
        else:
            raise ProtocolError(f"此适配器不支持内容类型 {kind}，请使用原生 Responses 提供商。")
    if all(p["type"] == "text" for p in result):
        return "".join(p["text"] for p in result)
    return result


def flatten_tools(tools, namespace=None):
    result, mapping = [], {}
    for tool in tools or []:
        kind = tool.get("type")
        if kind in ("web_search", "web_search_preview"):
            # Codex may include its hosted search even for catalog entries with
            # supports_search_tool=false. It isn't a client-executable function.
            # Do not advertise a hosted capability this provider cannot execute.
            continue
        if kind == "namespace":
            nested, inner = flatten_tools(tool.get("tools", []), tool["name"])
            result.extend(nested)
            mapping.update(inner)
            continue
        if kind not in ("function", "custom"):
            raise ProtocolError(f"Chat Completions 不支持 {kind} 工具；请关闭该工具或选择 Responses 提供商。")
        identity = tool_identity(tool["name"], namespace)
        name = safe_name(identity)
        if name in mapping:
            raise ProtocolError("工具名冲突。")
        mapping[name] = {"name": tool["name"], "namespace": namespace, "custom": kind == "custom"}
        parameters = tool.get("parameters") or {"type": "object", "properties": {}}
        if kind == "custom":
            parameters = {"type": "object", "properties": {"input": {"type": "string"}},
                          "required": ["input"], "additionalProperties": False}
        result.append({"type": "function", "function": {
            "name": name, "description": tool.get("description", ""),
            "parameters": parameters,
        }})
    if len({x["function"]["name"] for x in result}) != len(result):
        raise ProtocolError("命名空间展开后的工具名冲突。")
    return result, mapping


def to_chat(body, model):
    tools, mapping = flatten_tools(body.get("tools", []))
    messages = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    inputs = body.get("input", [])
    if isinstance(inputs, str):
        inputs = [{"type": "message", "role": "user", "content": inputs}]
    for item in inputs:
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role", "user")
            if role not in ("system", "developer", "user", "assistant"):
                raise ProtocolError("不支持的消息角色。")
            messages.append({"role": "system" if role == "developer" else role,
                             "content": content_parts(item.get("content", ""), role)})
        elif kind in ("function_call", "custom_tool_call"):
            identity = tool_identity(item["name"], item.get("namespace"))
            args = item.get("arguments", "{}")
            if kind == "custom_tool_call":
                args = json.dumps({"input": item.get("input", "")}, ensure_ascii=False)
            call = {"id": item["call_id"], "type": "function",
                    "function": {"name": safe_name(identity), "arguments": args}}
            if messages and messages[-1]["role"] == "assistant" and messages[-1].get("tool_calls"):
                messages[-1]["tool_calls"].append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind in ("function_call_output", "custom_tool_call_output"):
            output = item.get("output", "")
            if isinstance(output, list):
                output = content_parts(output, "tool")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": output})
        elif kind == "reasoning":
            # Encrypted provider-specific reasoning isn't portable. Visible assistant
            # messages and tool calls remain in the request.
            continue
        else:
            raise ProtocolError(f"历史含有不支持的 {kind} 项。请新建此提供商的会话，或使用 Responses 协议。")
    out = {"model": model["id"], "messages": messages, "stream": True,
           "stream_options": {"include_usage": True},
           "max_tokens": min(int(body.get("max_output_tokens") or model.get("max_output_tokens", 8192)),
                             int(model.get("max_output_tokens", 8192)))}
    if tools:
        out["tools"] = tools
        if "parallel_tool_calls" in body:
            out["parallel_tool_calls"] = body["parallel_tool_calls"]
        choice = body.get("tool_choice")
        if isinstance(choice, str):
            out["tool_choice"] = choice
        elif choice and choice.get("type") in ("function", "custom"):
            name = safe_name(tool_identity(choice["name"], choice.get("namespace")))
            out["tool_choice"] = {"type": "function", "function": {"name": name}}
        elif choice:
            raise ProtocolError("不支持此 tool_choice 形式。")
    if model.get("send_reasoning") and body.get("reasoning", {}).get("effort"):
        out["reasoning_effort"] = body["reasoning"]["effort"]
    for key in ("temperature", "top_p"):
        if key in body:
            out[key] = body[key]
    fmt = body.get("text", {}).get("format")
    if fmt and fmt.get("type") == "json_schema":
        out["response_format"] = {"type": "json_schema", "json_schema": {
            k: v for k, v in fmt.items() if k != "type"
        }}
    elif fmt and fmt.get("type") == "json_object":
        out["response_format"] = fmt
    return out, mapping


def to_responses(body, model):
    allowed = ("input", "instructions", "tools", "tool_choice", "parallel_tool_calls",
               "text", "temperature", "top_p", "truncation", "previous_response_id")
    out = {k: copy.deepcopy(body[k]) for k in allowed if k in body}
    if "tools" in out:
        out["tools"] = [t for t in out["tools"] if t.get("type") not in ("web_search", "web_search_preview")]
    out.update(model=model["id"], stream=bool(body.get("stream", True)), store=False)
    out["max_output_tokens"] = min(
        int(body.get("max_output_tokens") or model.get("max_output_tokens", 8192)),
        int(model.get("max_output_tokens", 8192)))
    if model.get("send_reasoning") and body.get("reasoning"):
        out["reasoning"] = body["reasoning"]
    return out


class ChatStream:
    def __init__(self, public_model, mapping):
        self.mapping = mapping
        self.sequence = 0
        self.output = []
        self.text_item = None
        self.tools = {}
        self.finish_reason = None
        self.response = {
            "id": uid("resp"), "object": "response", "created_at": int(time.time()),
            "status": "in_progress", "model": public_model, "output": self.output,
            "error": None, "incomplete_details": None,
        }

    def event(self, kind, **data):
        event = {"type": kind, "sequence_number": self.sequence, **data}
        self.sequence += 1
        return copy.deepcopy(event)

    def start(self):
        return [self.event("response.created", response=self.response),
                self.event("response.in_progress", response=self.response)]

    def chunk(self, chunk):
        if "error" in chunk:
            raise ProtocolError("上游在流中返回错误。")
        events = []
        if chunk.get("usage"):
            u = chunk["usage"]
            self.response["usage"] = {
                "input_tokens": u.get("prompt_tokens", 0),
                "output_tokens": u.get("completion_tokens", 0),
                "total_tokens": u.get("total_tokens", 0),
                "input_tokens_details": {"cached_tokens": u.get("prompt_tokens_details", {}).get("cached_tokens", 0)},
                "output_tokens_details": {"reasoning_tokens": u.get("completion_tokens_details", {}).get("reasoning_tokens", 0)},
            }
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                raise ProtocolError("适配器只支持单候选生成。")
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta", {})
            text = delta.get("content") or delta.get("refusal")
            if text:
                if not isinstance(text, str):
                    raise ProtocolError("上游文本增量格式错误。")
                if self.text_item is None:
                    self.text_item = {"id": uid("msg"), "type": "message", "role": "assistant",
                                      "status": "in_progress", "content": []}
                    self.output.append(self.text_item)
                    i = len(self.output) - 1
                    events.append(self.event("response.output_item.added", output_index=i, item=self.text_item))
                    self.text_item["content"].append({"type": "output_text", "text": "", "annotations": []})
                    events.append(self.event("response.content_part.added", item_id=self.text_item["id"],
                                             output_index=i, content_index=0, part=self.text_item["content"][0]))
                i = self.output.index(self.text_item)
                self.text_item["content"][0]["text"] += text
                events.append(self.event("response.output_text.delta", item_id=self.text_item["id"],
                                         output_index=i, content_index=0, delta=text, logprobs=[]))
            for tc in delta.get("tool_calls", []):
                index = tc.get("index", 0)
                state = self.tools.setdefault(index, {"id": None, "name": "", "args": "", "item": None})
                state["id"] = tc.get("id") or state["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    state["name"] += fn["name"]
                args = fn.get("arguments", "")
                state["args"] += args
                info = self.mapping.get(state["name"])
                if info and state["id"] and state["item"] is None:
                    item = {"id": uid("ct" if info["custom"] else "fc"),
                            "type": "custom_tool_call" if info["custom"] else "function_call",
                            "call_id": state["id"], "name": info["name"], "status": "in_progress"}
                    if info["namespace"]:
                        item["namespace"] = info["namespace"]
                    item["input" if info["custom"] else "arguments"] = ""
                    state["item"] = item
                    self.output.append(item)
                    events.append(self.event("response.output_item.added", output_index=len(self.output) - 1, item=item))
                    # Buffered arguments may have arrived before the name.
                    args = state["args"]
                if state["item"] is not None and not info["custom"] and args:
                    item = state["item"]
                    item["arguments"] += args
                    events.append(self.event("response.function_call_arguments.delta",
                                             item_id=item["id"], output_index=self.output.index(item), delta=args))
        return events

    def finish(self):
        if self.finish_reason is None:
            raise ProtocolError("上游流提前断开，未收到 finish_reason。")
        if self.finish_reason not in ("stop", "tool_calls", "length", "content_filter"):
            raise ProtocolError(f"不支持的结束原因：{self.finish_reason}")
        events = []
        for state in self.tools.values():
            item = state["item"]
            if item is None:
                raise ProtocolError("上游返回未知或不完整的工具调用。")
            i = self.output.index(item)
            if item["type"] == "custom_tool_call":
                try:
                    value = json.loads(state["args"])["input"]
                    if not isinstance(value, str):
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise ProtocolError("上游自定义工具参数不是有效的 input 字符串。") from None
                item["input"] = value
                events.append(self.event("response.custom_tool_call_input.delta", item_id=item["id"], output_index=i, delta=value))
                events.append(self.event("response.custom_tool_call_input.done", item_id=item["id"], output_index=i, input=value))
            else:
                try:
                    json.loads(item["arguments"])
                except ValueError:
                    raise ProtocolError("上游工具参数 JSON 不完整。") from None
                events.append(self.event("response.function_call_arguments.done", item_id=item["id"],
                                         output_index=i, arguments=item["arguments"]))
        if self.text_item:
            item = self.text_item
            i = self.output.index(item)
            events.append(self.event("response.output_text.done", item_id=item["id"], output_index=i,
                                     content_index=0, text=item["content"][0]["text"], logprobs=[]))
            events.append(self.event("response.content_part.done", item_id=item["id"], output_index=i,
                                     content_index=0, part=item["content"][0]))
        for i, item in enumerate(self.output):
            item["status"] = "completed"
            events.append(self.event("response.output_item.done", output_index=i, item=item))
        if self.finish_reason in ("length", "content_filter"):
            self.response["status"] = "incomplete"
            self.response["incomplete_details"] = {
                "reason": "max_output_tokens" if self.finish_reason == "length" else "content_filter"
            }
            events.append(self.event("response.incomplete", response=self.response))
        else:
            self.response["status"] = "completed"
            events.append(self.event("response.completed", response=self.response))
        return events


async def sse_events(content):
    """Read SSE independent of TCP chunks, CRLF boundaries and multi-line data."""
    data = []
    size = 0
    async for raw in content:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data, size = [], 0
        elif line.startswith("data:"):
            value = line[5:].lstrip(" ")
            data.append(value)
            size += len(value)
            if size > 8 * 1024 * 1024:
                raise ProtocolError("上游单个 SSE 事件超过大小限制。")
    if data:
        yield "\n".join(data)


def encode_event(event):
    return ("event: " + event["type"] + "\ndata: " +
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()
