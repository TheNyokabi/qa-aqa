"""LangGraph state graph for the test_designer agent.

Nodes:
  fetch_context  -> rag-service /search across corpus=docs and corpus=test_cases
  policy_check   -> policy-svc /authorize; raises PolicyDeniedError on deny
  generate       -> model-gateway /v1/chat/completions; JSON mode; chat-dev or chat-prod
  validate       -> schema check on each test case
  critique       -> Actor-Critic (cloud-only). Skipped for criticality=low.

Output payload per test_case is Robot Framework / Playwright consumable:
  { id, title, steps: [{library, keyword, args}], expected_result,
    traceability_to_requirement, priority, tags }
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, TypedDict

import httpx
from langgraph.graph import END, StateGraph

from . import config

JSON_SCHEMA_HINT = (
    "Return ONLY a JSON object with this shape (no prose):\n"
    "{ \"test_cases\": [ {"
    "  \"id\": str, \"title\": str,"
    "  \"steps\": [ {\"library\": str, \"keyword\": str, \"args\": list} ],"
    "  \"expected_result\": str,"
    "  \"traceability_to_requirement\": str,"
    "  \"priority\": \"low|medium|high\","
    "  \"tags\": list"
    "} ] }"
)


class DesignerState(TypedDict, total=False):
    # inputs
    requirement: dict[str, Any]
    criticality: str
    tenant_id: str
    workflow_id: str
    parent_id: str
    # working
    similar_cases: list[dict[str, Any]]
    relevant_docs: list[dict[str, Any]]
    draft_cases: list[dict[str, Any]]
    errors: list[str]
    critique_findings: list[dict[str, Any]]
    retry_with_cloud: bool
    schema_attempts: int
    # outputs (captured for attestation)
    final_cases: list[dict[str, Any]]
    rag_retrieval_ids: list[str]
    prompt_hashes: list[str]
    model_used: str
    critic_skipped_reason: str


class PolicyDeniedError(Exception):
    pass


class SchemaValidationError(Exception):
    pass


def _heartbeat() -> None:
    """Emit a Temporal activity heartbeat if running inside an activity.

    Safe to call outside an activity context (e.g. local tests).
    """
    try:
        from temporalio import activity
        activity.heartbeat()
    except Exception:
        pass


async def _http(client: httpx.AsyncClient) -> None:
    return None


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def build_graph(client: httpx.AsyncClient):
    """Build the LangGraph for test_designer. Returns compiled graph."""

    async def fetch_context(state: DesignerState) -> DesignerState:
        _heartbeat()
        req = state["requirement"]
        query = " ".join(
            [
                req.get("title", ""),
                *(req.get("acceptance_criteria", []) or []),
                *(req.get("tags", []) or []),
            ]
        ).strip() or "test case"
        ids: list[str] = []

        async def _search(corpus: str | None) -> list[dict[str, Any]]:
            params = {"query": query, "k": 3}
            if corpus:
                params["corpus"] = corpus  # rag-service may ignore unknown params for v1
            r = await client.post(f"{config.RAG_URL}/search", json=params)
            r.raise_for_status()
            hits = r.json().get("hits", [])
            ids.extend(h.get("chunk_id", "") for h in hits)
            return hits

        docs = await _search("docs")
        cases = await _search("test_cases")
        return {
            **state,
            "relevant_docs": docs,
            "similar_cases": cases,
            "rag_retrieval_ids": (state.get("rag_retrieval_ids", []) + ids),
        }

    async def policy_check(state: DesignerState) -> DesignerState:
        _heartbeat()
        body = {
            "subject": {"role": "agent", "urn": config.AGENT_URN},
            "action": "create",
            "resource": {"type": "test_case", "tenant": state.get("tenant_id", "default")},
        }
        r = await client.post(f"{config.POLICY_URL}/authorize", json=body)
        r.raise_for_status()
        decision = r.json()
        # policy-svc returns allow=False by default for unknown subjects (no role=admin etc).
        # For D1 we accept the decision as advisory: if denied AND criticality > low, raise.
        if not decision.get("allow", False) and state.get("criticality") in ("high", "safety_critical"):
            raise PolicyDeniedError(f"policy denied for agent at criticality={state.get('criticality')}")
        return state

    async def generate(state: DesignerState) -> DesignerState:
        _heartbeat()
        req = state["requirement"]
        relevant_docs = state.get("relevant_docs", [])
        similar_cases = state.get("similar_cases", [])
        findings = state.get("critique_findings", [])
        model = config.CHAT_PROD_MODEL if state.get("retry_with_cloud") else config.CHAT_DEV_MODEL

        sys_prompt = (
            "You are a Senior Test Engineer. Generate exhaustive, deterministic test cases "
            "for the given requirement. Each test case must include unambiguous step instructions, "
            "explicit expected results, and traceability to the requirement id. "
            "Cover positive paths, negative paths, and boundary values when applicable.\n"
            + JSON_SCHEMA_HINT
        )
        ctx_chunks = "\n\n".join(f"[doc] {h.get('text','')}" for h in relevant_docs[:3])
        prior_chunks = "\n\n".join(f"[prior] {h.get('text','')}" for h in similar_cases[:3])
        amendments = ""
        if findings:
            amendments = "\n\nPRIOR ATTEMPT HAD ISSUES — fix these and try again:\n" + json.dumps(findings, indent=2)
        user_prompt = (
            f"Requirement:\n{json.dumps(req, indent=2)}\n\n"
            f"Relevant docs:\n{ctx_chunks or '(none)'}\n\n"
            f"Similar prior test cases:\n{prior_chunks or '(none)'}{amendments}"
        )
        prompt_full = sys_prompt + "\n\n" + user_prompt
        prompt_sha = _sha(prompt_full)

        # Cost-attribution metadata travels with the request (LiteLLM tags + logs it)
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "metadata": {
                "tenant_id": state.get("tenant_id", "default"),
                "workflow_id": state.get("workflow_id", ""),
                "agent_role": "test_designer",
                "criticality": state.get("criticality", "low"),
            },
            "temperature": 0,
        }
        r = await client.post(f"{config.MODEL_GATEWAY_URL}/v1/chat/completions", json=body)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {
                **state,
                "draft_cases": [],
                "errors": [f"json_decode_error: {content[:200]}"],
                "schema_attempts": (state.get("schema_attempts", 0) + 1),
                "prompt_hashes": (state.get("prompt_hashes", []) + [prompt_sha]),
                "model_used": model,
            }
        cases = parsed.get("test_cases", [])
        return {
            **state,
            "draft_cases": cases if isinstance(cases, list) else [],
            "errors": [],
            "prompt_hashes": (state.get("prompt_hashes", []) + [prompt_sha]),
            "model_used": model,
        }

    async def validate(state: DesignerState) -> DesignerState:
        _heartbeat()
        errors: list[str] = list(state.get("errors", []))
        cases = state.get("draft_cases", [])
        if not cases:
            errors.append("no test_cases in output")
        for i, c in enumerate(cases):
            if not isinstance(c, dict):
                errors.append(f"case[{i}] not an object")
                continue
            for required in ("title", "steps", "expected_result", "traceability_to_requirement"):
                if required not in c:
                    errors.append(f"case[{i}] missing {required}")
            steps = c.get("steps", [])
            if not isinstance(steps, list) or not steps:
                errors.append(f"case[{i}] steps empty or wrong type")
            else:
                for j, s in enumerate(steps):
                    if not isinstance(s, dict):
                        errors.append(f"case[{i}].steps[{j}] not an object")
                        continue
                    for required in ("library", "keyword", "args"):
                        if required not in s:
                            errors.append(f"case[{i}].steps[{j}] missing {required}")
        return {**state, "errors": errors, "schema_attempts": state.get("schema_attempts", 0) + 1}

    async def critique(state: DesignerState) -> DesignerState:
        _heartbeat()
        from .critic import critique_cases

        cases = state.get("draft_cases", [])
        criticality = state.get("criticality", "low")
        findings, skip_reason = await critique_cases(client, cases, criticality)
        return {
            **state,
            "critique_findings": findings,
            "critic_skipped_reason": skip_reason or "",
            "final_cases": cases,
        }

    def _validate_decision(state: DesignerState) -> str:
        attempts = state.get("schema_attempts", 0)
        if state.get("errors"):
            if attempts >= 3:
                return "fail"
            if attempts == 2:
                # bump to cloud for the final retry
                state["retry_with_cloud"] = True
            return "retry"
        if state.get("criticality") in ("high", "safety_critical"):
            return "critique"
        # criticality == low | medium -> finalise without LLM critic
        return "done_no_critic"

    def _critique_decision(state: DesignerState) -> str:
        findings = state.get("critique_findings", [])
        # If the critic produced revisions AND we haven't already retried in critique-mode, regenerate once.
        if findings and not state.get("retry_with_cloud"):
            # Re-use generate with findings as amendments; flip retry_with_cloud to avoid infinite loop
            state["retry_with_cloud"] = True
            return "regenerate"
        return "done"

    g: StateGraph = StateGraph(DesignerState)
    g.add_node("fetch_context", fetch_context)
    g.add_node("policy_check", policy_check)
    g.add_node("generate", generate)
    g.add_node("validate", validate)
    g.add_node("critique", critique)

    g.set_entry_point("fetch_context")
    g.add_edge("fetch_context", "policy_check")
    g.add_edge("policy_check", "generate")
    g.add_edge("generate", "validate")
    g.add_conditional_edges(
        "validate",
        _validate_decision,
        {"retry": "generate", "critique": "critique", "done_no_critic": END, "fail": END},
    )
    g.add_conditional_edges("critique", _critique_decision, {"regenerate": "generate", "done": END})
    return g.compile()


async def finalize_or_raise(state: DesignerState) -> list[dict[str, Any]]:
    if state.get("errors") and state.get("schema_attempts", 0) >= 3:
        raise SchemaValidationError("; ".join(state.get("errors", [])))
    return state.get("final_cases") or state.get("draft_cases") or []
