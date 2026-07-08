from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_PROMPTS = Path(__file__).with_name("benchmark_prompts.jsonl")


def _load_prompts(path: Path, max_prompts: int) -> list[dict[str, Any]]:
    prompts = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            prompts.append(json.loads(line))
            if len(prompts) >= max_prompts:
                break
    return prompts


def _parse_sse_events(raw_text: str) -> list[dict[str, Any]]:
    events = []
    for block in raw_text.split("\n\n"):
        if not block.startswith("data: "):
            continue
        payload = block[6:].strip()
        if not payload:
            continue
        try:
            events.append(json.loads(payload))
        except Exception:
            continue
    return events


def run_benchmark(
    base_url: str,
    prompts_file: Path,
    max_prompts: int,
    output_path: Path,
    *,
    auto_confirm_mutations: bool,
) -> dict[str, Any]:
    prompts = _load_prompts(prompts_file, max_prompts)
    results = []
    headers = {"X-CSRF-Protection": "1", "Content-Type": "application/json"}

    with httpx.Client(base_url=base_url, timeout=300.0) as client:
        for item in prompts:
            payload = {"message": item["prompt"], "history": []}
            started = time.perf_counter()
            response = client.post("/api/v1/chat/stream", headers=headers, json=payload)
            total_ms = int((time.perf_counter() - started) * 1000)
            response.raise_for_status()
            events = _parse_sse_events(response.text)

            ttft_ms = None
            request_id = None
            trace_id = None
            artifact_created = False
            pending_confirmations = []
            for event in events:
                content = event.get("content")
                if event.get("type") == "token" and ttft_ms is None:
                    ttft_ms = int((time.perf_counter() - started) * 1000)
                if event.get("type") == "request_trace_id":
                    request_id = content
                if event.get("type") == "retrieval_trace_id":
                    trace_id = content
                if event.get("type") == "tool_result" and content and content.get("artifact_refs"):
                    artifact_created = True
                if event.get("type") == "tool_confirmation" and content:
                    pending_confirmations.append(content)
                if event.get("type") == "done" and isinstance(content, dict):
                    request_id = request_id or content.get("request_id")
                    trace_id = trace_id or content.get("trace_id")

            if auto_confirm_mutations and pending_confirmations:
                for confirmation in pending_confirmations:
                    confirm_resp = client.post(
                        "/api/v1/chat/tool-confirm",
                        headers=headers,
                        json={
                            "tool_name": confirmation.get("tool_name"),
                            "arguments": confirmation.get("arguments") or {},
                            "confirmed": True,
                            "request_id": request_id,
                        },
                    )
                    confirm_resp.raise_for_status()
                    confirm_payload = (confirm_resp.json() or {}).get("result") or {}
                    if confirm_payload.get("artifact_refs"):
                        artifact_created = True

            trace = None
            if request_id:
                trace_resp = client.get(f"/api/v1/request-traces/{request_id}")
                if trace_resp.status_code == 200:
                    trace_data = trace_resp.json()
                    if trace_data.get("success"):
                        trace = trace_data.get("trace")

            results.append(
                {
                    "id": item.get("id"),
                    "category": item.get("category"),
                    "prompt": item.get("prompt"),
                    "request_id": request_id,
                    "retrieval_trace_id": trace_id,
                    "lane": trace.get("lane") if trace else None,
                    "policy_reason": trace.get("policy_reason") if trace else None,
                    "models_used": trace.get("models_used") if trace else None,
                    "llm_calls": trace.get("llm_calls") if trace else None,
                    "retrieval_calls": trace.get("retrieval_calls") if trace else None,
                    "first_token_ms": trace.get("first_token_ms") if trace else ttft_ms,
                    "total_ms": trace.get("total_ms") if trace else total_ms,
                    "failure_mode": trace.get("failure_mode") if trace else None,
                    "artifact_created": artifact_created or bool(trace and trace.get("produced_artifact_type")),
                }
            )

    summary = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "base_url": base_url,
        "prompts_file": str(prompts_file),
        "prompt_count": len(results),
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark chat flows against persisted request traces.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--prompts-file", default=str(DEFAULT_PROMPTS))
    parser.add_argument("--max-prompts", type=int, default=50)
    parser.add_argument("--output", default=str(Path("Output") / "chat_trace_benchmark.json"))
    parser.add_argument("--no-auto-confirm", action="store_true", help="Do not confirm mutating tool calls during the benchmark.")
    args = parser.parse_args()

    summary = run_benchmark(
        base_url=args.base_url,
        prompts_file=Path(args.prompts_file),
        max_prompts=args.max_prompts,
        output_path=Path(args.output),
        auto_confirm_mutations=not args.no_auto_confirm,
    )
    print(json.dumps({
        "prompt_count": summary["prompt_count"],
        "output": args.output,
    }, indent=2))


if __name__ == "__main__":
    main()