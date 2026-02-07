# Syntax Overview

makethlm uses a file called `Promptfile` (also `promptfile`, `Promptfile.pf`, or `promptfile.pf`). You can specify a different file with `-f`.

## File Structure

A Promptfile consists of these top-level constructs:

```
# Comments
name := "value"                  # Variables
export secret := "key"           # Exported variables
set dotenv-load                  # Set directives
include "other.pf"               # Includes
llm claude [model=opus]          # LLM providers
hosts web [user=deploy]:         # Host groups
    web1.example.com
fn security_review:              # Functions
    review for vulnerabilities
task build: dep1 dep2            # Tasks
    !shell command
    natural language prompt
docker api [tag=latest]:         # Docker blocks
    describe the image
alias d := deploy                # Aliases
```

## Comments

Lines starting with `#` are comments:

```
# This is a comment
task build:
    build the project  # this is NOT a comment -- it's part of the prompt
```

## Line Continuation

Long lines can be split with `\`:

```
task build:
    this is a very long prompt \
    that continues on the next line
```

## Indentation

Task bodies, function bodies, host group members, and docker block descriptions are indented (spaces or tabs). The indentation level must be consistent within a block.

## File Format at a Glance

```
# Variables
name := "value"
backtick_var := `command`
concat := "a" + name + "b"
conditional := if name == "value" { "yes" } else { "no" }
export secret := "key"

# Set directives
set dotenv-load
set shell "bash"
set working-dir "/path"
set export
set positional-arguments
set ignore-comments
set quiet

# LLM providers
llm <name> [model=..., key=$..., base_url=..., template=...]

# Host groups
hosts <name> [user=..., port=...]:
    hostname1
    hostname2

# Functions
fn <name>:
    reusable prompt text

# Tasks
task <name>[(arg1, arg2="default", +variadic)] [options]: [dep1 dep2]
    !shell command
    !@silent command
    !@ignore command
    @echo "message"
    natural language prompt
    @use function_name

# Docker
docker <name> [tag=..., context=..., file=...]:
    description of the image

# Includes
include "path/to/file.pf"

# Aliases
alias <short> := <task>
```

See the individual pages for detailed documentation on each construct.
