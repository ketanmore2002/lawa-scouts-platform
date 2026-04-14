"""Browser automation agent using browser-use for autonomous web navigation."""

import logging
from app.config import get_settings

logger = logging.getLogger(__name__)

# Graceful import — browser-use is optional
_BROWSER_USE_AVAILABLE = False
try:
    from browser_use import Agent
    from langchain_openai import ChatOpenAI
    _BROWSER_USE_AVAILABLE = True
except ImportError:
    logger.info("browser-use not installed — browser agent disabled")


def is_available() -> bool:
    return _BROWSER_USE_AVAILABLE


async def browse_and_extract(task: str, max_steps: int = 10) -> dict:
    """Autonomously browse the web and extract data for a given task.

    Args:
        task: Natural language description of what to find/extract.
        max_steps: Maximum browser interactions before stopping.

    Returns:
        dict with keys: text (extracted content), success (bool), error (str|None)
    """
    if not _BROWSER_USE_AVAILABLE:
        return {"text": "", "success": False, "error": "browser-use not installed"}

    try:
        llm = ChatOpenAI(
            model="gpt-5.4-mini-2026-03-17",
            api_key=get_settings().openai_api_key,
        )
        agent = Agent(
            task=task,
            llm=llm,
            max_actions_per_step=3,
        )
        result = await agent.run(max_steps=max_steps)

        # Extract final text from result
        text = ""
        if hasattr(result, "final_result") and result.final_result:
            text = str(result.final_result())
        elif isinstance(result, str):
            text = result
        else:
            text = str(result)

        logger.info(f"Browser agent completed: {len(text)} chars extracted")
        return {"text": text, "success": True, "error": None}

    except Exception as e:
        logger.error(f"Browser agent failed: {e}")
        return {"text": "", "success": False, "error": str(e)}
