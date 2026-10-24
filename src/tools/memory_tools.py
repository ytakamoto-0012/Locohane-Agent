"""永続メモリー（create_memory 等）ツール群。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import logging

from .. import memory

from . import _state

logger = logging.getLogger(__name__)


def _require_memory_root() -> Path:
    """_state._MEMORY_ROOT を返す。init_tools() が未実行なら RuntimeError。"""
    if _state._MEMORY_ROOT is None:
        raise RuntimeError("init_tools() has not been run")
    return _state._MEMORY_ROOT


@tool
def create_memory(name: str, description: str, memory_type: str, content: str) -> str:
    """Save a new persistent memory.

    Use it when you learn a valuable fact worth carrying over to future conversations across
    threads. For when to save each memory_type and what must not be saved, see the Memory System
    section of the system prompt. If a memory with the same name already exists it is an error,
    so use update_memory to update an existing memory (if in doubt, first check for duplicates with
    search_memory / list_memories).

    Args:
        name: A unique name (letters, digits, hyphens and underscores only, up to 64 characters).
        description: A one-line description (shown as is in the index MEMORY.md).
        memory_type: One of "user" | "feedback" | "project" | "reference".
        content: The memory body. For the feedback/project types, it is best to include
            "**Why:**" and "**How to apply:**" lines after the rule/fact.

    Returns:
        A text telling the saved file path. If name/memory_type is invalid,
        description/content is empty, or a memory with the same name already exists,
        it returns an "Error: ..." string instead of raising.
    """
    try:
        path = memory.create_memory(_require_memory_root(), name, description, memory_type, content)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("create_memory: %s", name)
    return f"Saved the memory: {path}"


@tool
def update_memory(name: str, content: str) -> str:
    """Update the body of an existing persistent memory (name/description/memory_type do not change).

    Args:
        name: The name of the memory to update.
        content: The new body (replaces the existing body entirely).

    Returns:
        A text telling the updated file path. If content is empty or no memory has that name,
        it returns an "Error: ..." string instead of raising.
    """
    try:
        path = memory.update_memory(_require_memory_root(), name, content)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("update_memory: %s", name)
    return f"Updated the memory: {path}"


@tool
def delete_memory(name: str) -> str:
    """Delete a persistent memory.

    Args:
        name: The name of the memory to delete.

    Returns:
        A text telling the deleted file path. If no memory has that name,
        it returns an "Error: ..." string instead of raising.
    """
    try:
        path = memory.delete_memory(_require_memory_root(), name)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("delete_memory: %s", name)
    return f"Deleted the memory: {path}"


@tool
def read_memory(name: str) -> str:
    """Read one persistent memory in full, including its body.

    search_memory / list_memories return only a list (name+description), so always read the full
    text with this tool before checking or quoting the content of a memory.

    Args:
        name: The name of the memory to read.

    Returns:
        The full text in the form "[type] name\\ndescription\\n\\ncontent". If no memory has that
        name, it returns an "Error: ..." string instead of raising.
    """
    try:
        mem = memory.read_memory(_require_memory_root(), name)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("read_memory: %s", name)
    return f"[{mem.memory_type}] {mem.name}\n{mem.description}\n\n{mem.content}"


@tool
def search_memory(query: str, memory_type: str | None = None) -> str:
    """Search persistent memories by keyword substring match on name/description/body.

    Returns only a list (name+description), not the bodies (to save context).
    If you need the content itself, pass the name of a hit to read_memory.

    Args:
        query: The search keyword (case-insensitive).
        memory_type: If given, narrow to one of "user"|"feedback"|"project"|"reference"
            (all types if omitted).

    Returns:
        A list of hits as "- [type] name: description" and the count.
        With no hits, "No matching memories." If query is empty or memory_type is invalid,
        it returns an "Error: ..." string instead of raising.
    """
    try:
        hits = memory.search_memories(_require_memory_root(), query, memory_type)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("search_memory: query=%r type=%s hits=%d", query, memory_type, len(hits))
    if not hits:
        return "No matching memories."
    lines = [f"- [{m.memory_type}] {m.name}: {m.description}" for m in hits]
    return f"{len(hits)} hits.\n" + "\n".join(lines)


@tool
def list_memories(memory_type: str | None = None) -> str:
    """List the saved persistent memories.

    Args:
        memory_type: If given, narrow to one of "user"|"feedback"|"project"|"reference"
            (all types if omitted).

    Returns:
        A list of "- [type] name: description". With none,
        "No memories saved." If memory_type is invalid,
        it returns an "Error: ..." string instead of raising.
    """
    try:
        mems = memory.list_memories(_require_memory_root(), memory_type)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("list_memories: type=%s count=%d", memory_type, len(mems))
    if not mems:
        return "No memories saved."
    return "\n".join(f"- [{m.memory_type}] {m.name}: {m.description}" for m in mems)
