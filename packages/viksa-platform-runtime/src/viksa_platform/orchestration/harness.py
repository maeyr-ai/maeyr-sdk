"""The canonical native tool-calling orchestration loop.

Loop shape (per turn):

    messages = [system, user, ...]
    while iteration < max_iterations:
        assistant = llm_call(messages, tools)          # native tool calling
        if not assistant.tool_calls:                   # model is done
            yield FINAL(assistant.content); return
        messages.append(assistant.message)             # assistant w/ tool_calls
        # run ALL tool calls concurrently, stream their events:
        results = gather(run_tool(c) for c in assistant.tool_calls)
        for r in results: messages.append(tool_message(r))
        # if any call needs approval/input -> emit pause event + snapshot, pause

The harness is provider- and backend-agnostic: ``llm_call`` and ``run_tool``
are injected by the host service.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from .budget import BudgetPolicy, BudgetReason, BudgetState
from .protocols import (
    AssistantTurn,
    HarnessEvent,
    HarnessEventType,
    LLMCall,
    RunTool,
    ToolCall,
    ToolResult,
)


def _tool_message(result: ToolResult) -> Dict[str, Any]:
    content = result.content
    if content is None:
        content = ""
    if not isinstance(content, str):
        content = json.dumps(content, default=str)
    return {
        "role": "tool",
        "tool_call_id": result.call_id,
        "content": content,
    }


def _placeholder_tool_message(call: ToolCall, note: str) -> Dict[str, Any]:
    """A tool response for a call deferred by an interactive pause.

    Providers require every tool_call in an assistant message to have a matching
    tool response before the next assistant turn. When a call is deferred we emit
    a placeholder so the message list stays well-formed; on resume the real
    result is what actually gets surfaced to the model (the placeholder is only
    used if the run is abandoned).
    """
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": json.dumps({"status": "deferred", "note": note}),
    }


class OrchestrationHarness:
    """Drives the native tool-calling loop. One instance per run is fine."""

    def __init__(
        self,
        *,
        llm_call: LLMCall,
        run_tool: RunTool,
        max_iterations: int = 25,
        max_parallel_tools: int = 8,
        budget_policy: Optional[BudgetPolicy] = None,
        budget_state: Optional[BudgetState] = None,
        logger: Any = None,
    ) -> None:
        self._llm_call = llm_call
        self._run_tool = run_tool
        self._max_iterations = max(1, int(max_iterations))
        self._max_parallel = max(1, int(max_parallel_tools))
        self._budget_policy = budget_policy
        self._budget_state = budget_state or BudgetState()
        self._log = logger

    async def run(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        start_iteration: int = 0,
    ) -> AsyncGenerator[HarnessEvent, None]:
        """Execute the loop, yielding :class:`HarnessEvent`s.

        ``messages`` is mutated in place (assistant + tool messages appended) so
        the caller can persist it for resume. ``start_iteration`` lets a resumed
        run continue the iteration counter.
        """
        iteration = start_iteration
        while iteration < self._max_iterations:
            estimated_prompt_tokens = 0
            if self._budget_policy is not None:
                decision = self._budget_policy.prepare_request(
                    messages,
                    tools,
                    self._budget_state,
                )
                # The caller persists this same list for pause/resume, so apply
                # compaction in place rather than swapping the list object.
                messages[:] = decision.messages
                for budget_event in decision.events:
                    if self._log:
                        self._log.info(
                            "Orchestration budget event=%s before=%s after=%s "
                            "compacted_tools=%s dropped_messages=%s",
                            budget_event.get("event"),
                            budget_event.get("tokens_before"),
                            budget_event.get("tokens_after"),
                            budget_event.get("compacted_tool_results"),
                            budget_event.get("dropped_messages"),
                        )
                    yield HarnessEvent(
                        HarnessEventType.BUDGET_EVENT,
                        {
                            **budget_event,
                            "iteration": iteration,
                            "budget_state": self._budget_state.to_dict(),
                        },
                    )
                if not decision.allowed:
                    yield self._budget_terminal(
                        decision.reason or BudgetReason.CONTEXT_WINDOW,
                        estimated_prompt_tokens=decision.estimated_prompt_tokens,
                        iteration=iteration,
                    )
                    return
                estimated_prompt_tokens = decision.estimated_prompt_tokens

            iteration += 1
            yield HarnessEvent(
                HarnessEventType.ITERATION_START,
                {"iteration": iteration, "max_iterations": self._max_iterations},
            )

            try:
                if self._budget_policy is None:
                    assistant: AssistantTurn = await self._llm_call(messages, tools)
                else:
                    remaining = self._budget_policy.remaining_seconds(
                        self._budget_state
                    )
                    assistant = await asyncio.wait_for(
                        self._llm_call(messages, tools),
                        timeout=max(0.001, remaining),
                    )
            except asyncio.TimeoutError as exc:
                if self._budget_policy is not None:
                    yield self._budget_terminal(
                        BudgetReason.WALL_TIME,
                        iteration=iteration,
                    )
                    return
                if self._log:
                    self._log.error("Harness llm_call timed out")
                yield HarnessEvent(HarnessEventType.ERROR, {"error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 - surface to host
                if self._log:
                    self._log.error(
                        "Harness llm_call failed error_type=%s",
                        type(exc).__name__,
                    )
                yield HarnessEvent(HarnessEventType.ERROR, {"error": str(exc)})
                return

            if self._budget_policy is not None:
                self._budget_state.add_usage(assistant.tokens_used)
            actual_budget_reason: Optional[str] = None
            if self._budget_policy is not None:
                if (
                    assistant.tool_calls
                    and self._budget_policy.token_limit_reached(
                        self._budget_state
                    )
                ):
                    actual_budget_reason = BudgetReason.CUMULATIVE_TOKENS
                elif (
                    not assistant.tool_calls
                    and self._budget_policy.token_limit_exceeded(
                        self._budget_state
                    )
                ):
                    actual_budget_reason = BudgetReason.CUMULATIVE_TOKENS
            yield HarnessEvent(
                HarnessEventType.MODEL_RESPONSE,
                {
                    "iteration": iteration,
                    "content": assistant.content,
                    "tool_calls": assistant.tool_calls,
                    "tokens_used": assistant.tokens_used,
                    "prompt_tokens": assistant.prompt_tokens,
                    "completion_tokens": assistant.completion_tokens,
                    "model": assistant.model,
                    "fallback_used": assistant.fallback_used,
                    "finish_reason": assistant.finish_reason,
                    "budget_terminal": actual_budget_reason is not None,
                },
            )

            if actual_budget_reason is not None:
                yield self._budget_terminal(
                    actual_budget_reason,
                    estimated_prompt_tokens=estimated_prompt_tokens,
                    iteration=iteration,
                    last_content=assistant.content or "",
                )
                return

            if not assistant.tool_calls:
                yield HarnessEvent(
                    HarnessEventType.FINAL,
                    {"content": assistant.content or "", "iteration": iteration},
                )
                return

            # Actual provider usage is authoritative.  If this round exhausted
            # the cumulative allowance, do not execute newly proposed tools.
            # In particular, do not append the assistant tool-call message:
            # doing so would leave an orphaned provider transcript.
            # Append the assistant message (with tool_calls) BEFORE results so the
            # provider sees a well-formed tool_call/tool_result pairing.
            messages.append(assistant.message or self._fallback_assistant_message(assistant))

            # Run all tool calls concurrently, streaming their events as they come.
            results: Dict[str, ToolResult] = {}
            approvals: Dict[str, Tuple[ToolCall, Any]] = {}
            input_requests: Dict[str, Tuple[ToolCall, Any]] = {}
            tool_timed_out = False
            try:
                async for ev in self._stream_tools_with_timeout(
                    assistant.tool_calls
                ):
                    if ev.type == HarnessEventType.TOOL_RESULT:
                        res: ToolResult = ev.payload["tool_result"]
                        if self._budget_policy is not None:
                            bounded, tool_budget_event = (
                                self._budget_policy.limit_tool_result(res.content)
                            )
                            res.content = bounded
                            if tool_budget_event is not None:
                                self._budget_state.compacted_tool_results += 1
                                yield HarnessEvent(
                                    HarnessEventType.BUDGET_EVENT,
                                    {
                                        **tool_budget_event,
                                        "iteration": iteration,
                                        "budget_state": self._budget_state.to_dict(),
                                    },
                                )
                        results[res.call_id] = res
                        yield ev
                    elif ev.type == HarnessEventType.APPROVAL_REQUIRED:
                        call = ev.payload["tool_call"]
                        approvals[call.id] = (call, ev.payload.get("approval"))
                        yield ev
                    elif ev.type == HarnessEventType.USER_INPUT_REQUIRED:
                        call = ev.payload["tool_call"]
                        input_requests[call.id] = (
                            call,
                            ev.payload.get("input_request"),
                        )
                        yield ev
                    else:
                        yield ev
            except asyncio.TimeoutError:
                tool_timed_out = True

            # Append tool result messages in the original call order so the
            # provider can pair them with the assistant's tool_calls.
            for call in assistant.tool_calls:
                if call.id in results:
                    messages.append(_tool_message(results[call.id]))
                elif call.id in approvals:
                    messages.append(
                        _placeholder_tool_message(call, "awaiting user approval")
                    )
                elif call.id in input_requests:
                    messages.append(
                        _placeholder_tool_message(call, "awaiting user input")
                    )
                else:
                    # Defensive fallback for a malformed executor terminal event.
                    messages.append(
                        _placeholder_tool_message(
                            call,
                            (
                                "not completed before orchestration time budget"
                                if tool_timed_out
                                else "tool execution deferred"
                            ),
                        )
                    )

            if tool_timed_out:
                yield self._budget_terminal(
                    BudgetReason.WALL_TIME,
                    iteration=iteration,
                )
                return

            ordered_approvals = [
                approvals[call.id]
                for call in assistant.tool_calls
                if call.id in approvals
            ]
            ordered_inputs = [
                input_requests[call.id]
                for call in assistant.tool_calls
                if call.id in input_requests
            ]

            if ordered_approvals:
                # Approval takes priority in a mixed batch. The host persists
                # both queues and prompts one item at a time; once approvals are
                # settled it can serialize any queued input requests.
                yield HarnessEvent(
                    HarnessEventType.APPROVAL_REQUIRED,
                    {
                        "pending": ordered_approvals,
                        "pending_inputs": ordered_inputs,
                        "messages": messages,
                        "iteration": iteration,
                        "max_iterations": self._max_iterations,
                        "paused": True,
                        "budget_state": self._budget_state.to_dict(),
                    },
                )
                return
            if ordered_inputs:
                yield HarnessEvent(
                    HarnessEventType.USER_INPUT_REQUIRED,
                    {
                        "pending": ordered_inputs,
                        "messages": messages,
                        "iteration": iteration,
                        "max_iterations": self._max_iterations,
                        "paused": True,
                        "budget_state": self._budget_state.to_dict(),
                    },
                )
                return

        yield HarnessEvent(
            HarnessEventType.MAX_ITERATIONS,
            {"iteration": iteration, "max_iterations": self._max_iterations},
        )

    # ------------------------------------------------------------------
    # Parallel tool streaming (fan-in)
    # ------------------------------------------------------------------

    async def _stream_tools_with_timeout(
        self,
        tool_calls: List[ToolCall],
    ) -> AsyncGenerator[HarnessEvent, None]:
        if self._budget_policy is None:
            async for event in self._stream_tools(tool_calls):
                yield event
            return

        remaining = self._budget_policy.remaining_seconds(self._budget_state)
        if remaining <= 0:
            raise asyncio.TimeoutError
        deadline = asyncio.get_running_loop().time() + remaining
        stream = self._stream_tools(tool_calls)
        try:
            while True:
                wait_seconds = deadline - asyncio.get_running_loop().time()
                if wait_seconds <= 0:
                    raise asyncio.TimeoutError
                try:
                    event = await asyncio.wait_for(anext(stream), wait_seconds)
                except StopAsyncIteration:
                    break
                yield event
        finally:
            await stream.aclose()

    async def _stream_tools(
        self, tool_calls: List[ToolCall]
    ) -> AsyncGenerator[HarnessEvent, None]:
        """Run every tool call concurrently and stream all their events.

        Each call's ``run_tool`` generator runs in its own task and pushes events
        into a shared queue; this method drains the queue so events from
        different calls interleave in real time (true parallelism), bounded by a
        semaphore.
        """
        queue: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        sem = asyncio.Semaphore(self._max_parallel)
        _DONE: None = None

        async def _drive(call: ToolCall) -> None:
            async with sem:
                # Emit a started marker first so the UI can show all calls at once.
                await queue.put(
                    HarnessEvent(
                        HarnessEventType.TOOL_CALL_STARTED,
                        {"tool_call": call},
                    )
                )
                produced_terminal = False
                try:
                    async for ev in self._run_tool(call):
                        if ev.type in (
                            HarnessEventType.TOOL_RESULT,
                            HarnessEventType.APPROVAL_REQUIRED,
                            HarnessEventType.USER_INPUT_REQUIRED,
                        ):
                            produced_terminal = True
                        await queue.put(ev)
                except Exception as exc:  # noqa: BLE001
                    if self._log:
                        self._log.error(
                            "Tool execution failed tool=%s error_type=%s",
                            call.name,
                            type(exc).__name__,
                        )
                    await queue.put(
                        HarnessEvent.tool_result(
                            ToolResult(
                                call_id=call.id,
                                name=call.name,
                                content=json.dumps({"error": str(exc)}),
                                is_error=True,
                                raw={"error": str(exc)},
                            )
                        )
                    )
                    produced_terminal = True
                finally:
                    if not produced_terminal:
                        # Guarantee a terminal result so the loop can pair messages.
                        await queue.put(
                            HarnessEvent.tool_result(
                                ToolResult(
                                    call_id=call.id,
                                    name=call.name,
                                    content=json.dumps({"status": "no_result"}),
                                    is_error=True,
                                    raw=None,
                                )
                            )
                        )
                    await queue.put(_DONE)

        tasks = [asyncio.create_task(_drive(c)) for c in tool_calls]
        remaining = len(tasks)
        try:
            while remaining > 0:
                item = await queue.get()
                if item is _DONE:
                    remaining -= 1
                    continue
                yield item
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _budget_terminal(
        self,
        reason: str,
        *,
        iteration: int,
        estimated_prompt_tokens: int = 0,
        last_content: str = "",
    ) -> HarnessEvent:
        if self._budget_policy is None:
            payload = {
                "reason": reason,
                "message": "The orchestration run reached a configured budget.",
                "incomplete": True,
                "partial": True,
            }
        else:
            payload = self._budget_policy.terminal_summary(
                reason=reason,
                state=self._budget_state,
                estimated_prompt_tokens=estimated_prompt_tokens,
            )
        payload.update(
            {
                "iteration": iteration,
                "max_iterations": self._max_iterations,
                "last_content": last_content,
            }
        )
        if self._log:
            self._log.warning(
                "Orchestration budget exit reason=%s iteration=%s "
                "cumulative_tokens=%s elapsed_seconds=%s",
                reason,
                iteration,
                payload.get("cumulative_tokens"),
                payload.get("elapsed_seconds"),
            )
        return HarnessEvent(HarnessEventType.BUDGET_EXCEEDED, payload)

    @staticmethod
    def _fallback_assistant_message(assistant: AssistantTurn) -> Dict[str, Any]:
        return {
            "role": "assistant",
            "content": assistant.content or None,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": c.raw_arguments or json.dumps(c.arguments),
                    },
                }
                for c in assistant.tool_calls
            ],
        }
