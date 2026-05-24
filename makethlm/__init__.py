"""makethlm — A Makefile/Justfile for LLM prompts."""

from .dispatcher import ClaudeDispatcher, CodexDispatcher, Dispatcher, DryRunDispatcher, ShellDispatcher
from .models import Promptfile, Task, TaskOptions, TaskStep
from .parser import ParseError, parse
from .runner import Runner, RunResult, TaskResult, StepResult

__version__ = "0.1.0"

__all__ = [
    "ClaudeDispatcher",
    "CodexDispatcher",
    "Dispatcher",
    "DryRunDispatcher",
    "ParseError",
    "Promptfile",
    "RunResult",
    "Runner",
    "ShellDispatcher",
    "StepResult",
    "Task",
    "TaskOptions",
    "TaskResult",
    "TaskStep",
    "__version__",
    "parse",
]
