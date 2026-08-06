# Examples

Each directory contains a `Promptfile` demonstrating a different use case.

## Quick start

```bash
cd examples/python-project
makethlm --list          # see what tasks are available
makethlm --dry-run test  # preview without calling the LLM
makethlm test            # run it for real
```

## What's here

| Example | What it shows |
|---------|--------------|
| **c-project/** | LLM generates a C library, then shell commands compile and link it |
| **python-project/** | Run pytest, then ask the LLM to explain any failures |
| **npm-project/** | Typical JS build pipeline with LLM-powered code review |
| **blog-generator/** | Non-code use case: generate blog posts, summaries, tweet threads |
| **cmake-project/** | C/C++ CMake configure/build/test workflow with Codex review |
| **compiler-diagnostics/** | Capture compiler failures as artifacts and ask an LLM to explain them |
| **python-ci/** | Python lint, compile, test, and security-review workflow |
| **docker-compose/** | Docker Compose deploy, logs, rollback, and review workflow |
| **systemd-service/** | systemd service deployment with status, logs, and rollback |
| **kubernetes/** | kubectl diff/apply/diagnose/logs workflow |
| **ansible-deploy/** | Ansible inventory import with SSH deploy tasks |
| **python-release/** | Python package check, build, validate, and release flow |
| **cmake-compile-commands/** | CMake `compile_commands.json` generation and analysis |
| **gitlab-ci/** | GitLab CI snippets that call makethlm tasks from pipeline jobs |
| **forgejo-actions/** | Forgejo Actions example workflow without adding repo-level CI config |
| **rollback-patterns/** | Production deploy, verify, and rollback task patterns |

## How Promptfiles work

A Promptfile mixes **shell commands** (lines starting with `!`) and **LLM prompts** (plain text):

```
task test:
    !pytest -v 2>&1 || true          # <-- shell: runs pytest
    If any tests failed, explain     # <-- prompt: sent to the LLM
    the root cause and suggest a fix.
```

Tasks can depend on other tasks, accept arguments, and use different LLM providers. Run `makethlm --list` in any example directory to see what's available.
