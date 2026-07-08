"""
Quick test of the 3-stage synthesis pipeline.
Run from LeisureLLM directory: python test_pipeline.py
"""

import asyncio
import sys
from pathlib import Path

# Setup path
sys.path.insert(0, str(Path(__file__).parent))

from services.model_router import BackendConfig, BackendType, ModelRouter, PipelineConfig, PipelineRole, RoleConfig

from config import gpt_key


async def test_full_pipeline():
    print("=" * 60)
    print("TESTING 3-STAGE SYNTHESIS PIPELINE")
    print("=" * 60)
    
    router = ModelRouter()
    
    # Register Ollama (local)
    print("\n[1/4] Registering Ollama backend...")
    ollama_ok = await router.register_backend(BackendConfig(
        backend_type=BackendType.OLLAMA,
        name="ollama",
        endpoint_url="http://localhost:11434",
    ))
    if ollama_ok:
        print(f"  ✓ Ollama connected. Models: {router.backends['ollama'].available_models}")
    else:
        print("  ✗ Ollama not available - is it running?")
        return
    
    # Register OpenAI (cloud)
    print("\n[2/4] Registering OpenAI backend...")
    openai_ok = await router.register_backend(BackendConfig(
        backend_type=BackendType.OPENAI,
        name="openai",
        api_key=gpt_key,
    ))
    if openai_ok:
        print("  ✓ OpenAI connected.")
    else:
        print("  ✗ OpenAI connection failed")
        return
    
    # Configure pipeline
    print("\n[3/4] Configuring hybrid pipeline...")
    router.configure_pipeline(PipelineConfig(
        name="test-hybrid",
        roles={
            PipelineRole.INITIAL: RoleConfig(
                role=PipelineRole.INITIAL,
                backend_name="ollama",
                model="qwen2.5:32b",
                temperature=0.4,
            ),
            PipelineRole.CRITIQUE: RoleConfig(
                role=PipelineRole.CRITIQUE,
                backend_name="openai",
                model="o3-mini",
                temperature=0.2,
            ),
            PipelineRole.SYNTHESIZE: RoleConfig(
                role=PipelineRole.SYNTHESIZE,
                backend_name="openai", 
                model="gpt-5.2",
                temperature=0.3,
            ),
        }
    ))
    print("  ✓ Pipeline configured: Ollama(qwen2.5:32b) → OpenAI(o3-mini) → OpenAI(gpt-5.2)")
    
    # Run the pipeline
    print("\n[4/4] Running synthesis pipeline...")
    print("-" * 60)
    
    test_question = "What are the three most important things to consider when prioritizing tasks for a small creative agency?"
    
    print(f"Question: {test_question}\n")
    
    import time
    start = time.time()
    
    result = await router.generate_pipeline(
        user_prompt=test_question,
        context="This is a test context for the pipeline router.",
        system_prompt="You are a helpful business advisor.",
    )
    
    elapsed = time.time() - start
    
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    
    print("\n--- STAGE 1: Initial (Local - qwen2.5:32b) ---")
    print(result["stages"].get("initial", "N/A")[:500] + "..." if len(result["stages"].get("initial", "")) > 500 else result["stages"].get("initial", "N/A"))
    
    print("\n--- STAGE 2: Critique (Cloud - o3-mini) ---")
    print(result["stages"].get("critique", "N/A")[:500] + "..." if len(result["stages"].get("critique", "")) > 500 else result["stages"].get("critique", "N/A"))
    
    print("\n--- STAGE 3: Final Synthesis (Cloud - gpt-5.2) ---")
    print(result["final"])
    
    print("\n" + "=" * 60)
    print(f"Total time: {elapsed:.1f}s")
    print(f"Models used: {result['models_used']}")
    print("=" * 60)
    
    await router.close()


async def test_local_only():
    """Quick test of just the local model."""
    print("\n" + "=" * 60)
    print("QUICK LOCAL TEST (Ollama only)")
    print("=" * 60)
    
    router = ModelRouter()
    
    await router.register_backend(BackendConfig(
        backend_type=BackendType.OLLAMA,
        name="ollama",
    ))
    
    import time
    start = time.time()
    
    response = await router.generate_single(
        backend_name="ollama",
        model="qwen2.5:32b",
        prompt="What is 2+2? Answer in one word.",
    )
    
    elapsed = time.time() - start
    print(f"Response: {response}")
    print(f"Time: {elapsed:.2f}s")
    
    await router.close()


if __name__ == "__main__":
    print("\nStarting pipeline tests...\n")
    
    # Quick local test first
    asyncio.run(test_local_only())
    
    # Full pipeline test
    asyncio.run(test_full_pipeline())
