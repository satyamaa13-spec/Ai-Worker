import itertools
import os
import re
import time


def retry_on_rate_limit(fn, notify=None, sleep=time.sleep, max_tries=8):
    """Calls fn and retries after the suggested delay when the API reports a rate limit."""
    for attempt in range(1, max_tries + 1):
        try:
            return fn()
        except Exception as e:
            msg = str(e)
            if not any(x in msg for x in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE")):
                raise
            if "PerDay" in msg or attempt == max_tries:
                raise
            m = re.search(r"retry in ([\d.]+)s", msg) or re.search(r"retryDelay['\"]?: ?['\"]?(\d+)", msg)
            wait = float(m.group(1)) + 1 if m else 15.0
            if notify:
                notify(f"Rate limited by the model API. Waiting {wait:.0f}s, then retrying (attempt {attempt}/{max_tries - 1}).")
            sleep(wait)


class AnthropicLLM:
    """Model adapter for Claude."""
    def __init__(self, model=None):
        """Creates the Claude client and picks the model name."""
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("WORKER_MODEL", "claude-sonnet-5-5")

    def step(self, system, messages, tools):
        """Sends one turn to Claude and returns its text and tool calls."""
        r = self.client.messages.create(model=self.model, max_tokens=2048, system=system, tools=tools, messages=messages)
        blocks = []
        for b in r.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        return blocks

    def judge(self, prompt):
        """Asks Claude a single question and returns the text reply."""
        r = self.client.messages.create(model=self.model, max_tokens=500, messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content if b.type == "text")


class ScriptedLLM:
    """Fake model that replays fixed turns, used only to test the harness."""

    def __init__(self, turns, verdict='{"achieved": true, "reason": "scripted"}'):
        """Stores the scripted turns and the fixed verifier answer."""
        self.turns, self.verdict, self._ids = iter(turns), verdict, itertools.count()

    def step(self, system, messages, tools):
        """Returns the next scripted turn as text and tool calls."""
        thought, calls = next(self.turns)
        blocks = [{"type": "text", "text": thought}]
        blocks += [{"type": "tool_use", "id": f"toolu_{next(self._ids)}", "name": n, "input": i} for n, i in calls]
        return blocks

    def judge(self, prompt):
        """Returns the fixed verifier answer."""
        return self.verdict


class GeminiLLM:
    """Model adapter for Google Gemini, translating the agent's message format."""

    def __init__(self, model=None):
        """Creates the Gemini client and picks the model name."""
        from google import genai
        from google.genai import types
        self.types = types
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.client = genai.Client(api_key=key)
        self.model = model or os.environ.get("WORKER_MODEL", "gemini-3.5-flash-lite")
        self.notify = None
        self._parts, self._ids = {}, itertools.count()

    def _contents(self, messages):
        """Converts the agent's messages into Gemini's content format."""
        t, names, out = self.types, {}, []
        for m in messages:
            if isinstance(m["content"], str):
                out.append(t.Content(role="user", parts=[t.Part(text=m["content"])]))
            elif m["role"] == "assistant":
                parts = []
                for b in m["content"]:
                    if b["type"] == "text" and b["text"].strip():
                        parts.append(t.Part(text=b["text"]))
                    elif b["type"] == "tool_use":
                        names[b["id"]] = b["name"]
                        parts.append(self._parts.get(b["id"]) or t.Part(function_call=t.FunctionCall(name=b["name"], args=b["input"])))
                out.append(t.Content(role="model", parts=parts))
            else:
                parts = [t.Part(function_response=t.FunctionResponse(name=names.get(b["tool_use_id"], "unknown"),
                                                                     response={"result": b["content"]}))
                         for b in m["content"] if b["type"] == "tool_result"]
                out.append(t.Content(role="user", parts=parts))
        return out

    def step(self, system, messages, tools):
        """Sends one turn to Gemini and returns its text and tool calls."""
        t = self.types
        decls = [t.FunctionDeclaration(name=x["name"], description=x["description"], parameters_json_schema=x["input_schema"])
                 for x in tools]
        cfg = t.GenerateContentConfig(system_instruction=system, tools=[t.Tool(function_declarations=decls)],
                                      automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True))
        contents = self._contents(messages)
        r = retry_on_rate_limit(lambda: self.client.models.generate_content(model=self.model, contents=contents, config=cfg), self.notify)
        cand = r.candidates[0] if r.candidates else None
        parts = (cand.content.parts if cand and cand.content else None) or []
        blocks = []
        for p in parts:
            if p.function_call:
                cid = f"g_{next(self._ids)}"
                self._parts[cid] = p
                blocks.append({"type": "tool_use", "id": cid, "name": p.function_call.name, "input": dict(p.function_call.args or {})})
            elif p.text and not getattr(p, "thought", False):
                blocks.append({"type": "text", "text": p.text})
        return blocks

    def judge(self, prompt):
        """Asks Gemini a single question and returns the text reply."""
        r = retry_on_rate_limit(lambda: self.client.models.generate_content(model=self.model, contents=prompt), self.notify)
        return r.text or ""
