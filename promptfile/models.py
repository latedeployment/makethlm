"""Data models for the Promptfile AST."""

from dataclasses import dataclass, field


@dataclass
class TaskOptions:
    """Optional metadata attached to a task via [key=value, ...] syntax."""

    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    def merge(self, overrides: "TaskOptions") -> "TaskOptions":
        """Return a new TaskOptions with non-None overrides applied."""
        return TaskOptions(
            model=overrides.model if overrides.model is not None else self.model,
            temperature=overrides.temperature
            if overrides.temperature is not None
            else self.temperature,
            max_tokens=overrides.max_tokens
            if overrides.max_tokens is not None
            else self.max_tokens,
        )


@dataclass
class Task:
    """A single task definition from a Promptfile."""

    name: str
    prompt: str
    dependencies: list[str] = field(default_factory=list)
    options: TaskOptions = field(default_factory=TaskOptions)
    line_number: int = 0


@dataclass
class Promptfile:
    """The parsed representation of an entire Promptfile."""

    variables: dict[str, str] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    task_order: list[str] = field(default_factory=list)

    @property
    def default_task(self) -> str | None:
        """The first defined task is the default, like Make."""
        return self.task_order[0] if self.task_order else None

    def resolve_prompt(self, task_name: str) -> str:
        """Return the prompt for a task with {{variables}} interpolated."""
        task = self.tasks[task_name]
        prompt = task.prompt
        for key, value in self.variables.items():
            prompt = prompt.replace("{{" + key + "}}", value)
        return prompt
