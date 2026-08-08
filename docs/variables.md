# Variables

## Defining Variables

Define variables with `:=` and reference them with `{{name}}` in prompts and shell commands.

```
project := "my-web-app"
env := "staging"

task deploy:
    deploy {{project}} to {{env}}
```

Variable values must be double-quoted strings. Escaped quotes (`\"`) and escaped backslashes (`\\`) are supported:

```
greeting := "hello \"world\""
```

## Backtick Variables

Execute a shell command at parse time and capture its stdout:

```
version := `git describe --tags`

task release:
    release {{version}} to production
```

## CLI Overrides

Variables can be overridden from the command line with `--var` / `-V`:

```bash
makethlm deploy -V env=production
```

## String Concatenation

Variables support string concatenation with `+`:

```
prefix := "my"
project := prefix + "-app" + "-v1"     # "my-app-v1"
```

## Conditional Expressions

Justfile-compatible `if/else` expressions:

```
env := "production"
message := if env == "production" { "deploy carefully" } else { "test freely" }
```

Operators: `==`, `!=`.

## Export Variables

Variables prefixed with `export` are passed to the environment of shell commands:

```
export API_KEY := "secret"
export DATABASE_URL := "postgres://..."

task deploy:
    !echo $API_KEY          # accessible in shell commands
    deploy with API key
```

Use `set export` to export **all** variables:

```
set export

project := "myapp"         # exported automatically
version := "1.0"           # exported automatically
```

## Environment Variables

Reference environment variables in prompts with `${VAR}` syntax. An optional default value can be provided with `:-`:

```
task deploy:
    deploy to ${DEPLOY_TARGET}
    use credentials from ${SECRET_PATH:-/etc/secrets/default}
```

| Syntax | Behavior |
|--------|----------|
| `${VAR}` | Replaced with the value of `$VAR`, or empty string if unset |
| `${VAR:-fallback}` | Replaced with `$VAR` if set, otherwise `fallback` |

Environment variables are resolved in **prompt steps only** (not in shell commands, where the shell itself handles `$VAR` expansion).

## Built-in Functions

Justfile-compatible built-in functions, available in `{{ }}` templates:

| Function | Description |
|----------|-------------|
| `{{os()}}` | Current OS: `linux`, `macos`, or `windows` |
| `{{os_family()}}` | OS family: `unix` or `windows` |
| `{{arch()}}` | CPU architecture (e.g., `x86_64`, `aarch64`) |
| `{{num_cpus()}}` | Number of CPU cores |
| `{{home_directory()}}` | User's home directory |

```
task info:
    running on {{os()}} ({{arch()}}) with {{num_cpus()}} cores
    home: {{home_directory()}}
```

## String Functions

String manipulation functions can be used inside `{{ }}` templates on variables.

### String Manipulation

| Function | Example | Result |
|----------|---------|--------|
| `uppercase(s)` | `{{uppercase(name)}}` | `HELLO` |
| `lowercase(s)` | `{{lowercase(name)}}` | `hello` |
| `trim(s)` | `{{trim(padded)}}` | `hello` |
| `trim_start(s)` | `{{trim_start(padded)}}` | `hello ` |
| `trim_end(s)` | `{{trim_end(padded)}}` | ` hello` |
| `replace(s, from, to)` | `{{replace(path, "/", "-")}}` | `src-main` |
| `replace_regex(s, pat, to)` | `{{replace_regex(ver, "\\d+$", "0")}}` | `1.2.0` |
| `quote(s)` | `{{quote(cmd)}}` | `'hello world'` |
| `join(sep, a, b, ...)` | `{{join(", ", "a", "b")}}` | `a, b` |
| `env_var(name[, default])` | `{{env_var("HOME")}}` | `/home/user` |
| `path_exists(p)` | `{{path_exists("README.md")}}` | `true` |
| `len(s)` | `{{len(name)}}` | `5` |
| `substr(s, start[, len])` | `{{substr(name, 0, 3)}}` | `hel` |
| `match(s, regex)` | `{{match(ver, "^\\d+")}}` | `true` |

### Path Functions

| Function | Example | Result |
|----------|---------|--------|
| `file_name(p)` | `{{file_name("/tmp/a.txt")}}` | `a.txt` |
| `file_stem(p)` | `{{file_stem("/tmp/a.txt")}}` | `a` |
| `extension(p)` | `{{extension("a.tar.gz")}}` | `gz` |
| `without_extension(p)` | `{{without_extension("a.tar.gz")}}` | `a.tar` |
| `parent_directory(p)` | `{{parent_directory("/tmp/a.txt")}}` | `/tmp` |

### Boolean Functions

Return `"true"` or `"false"`:

| Function | Description |
|----------|-------------|
| `contains(s, sub)` | Whether `s` contains `sub` |
| `starts_with(s, prefix)` | Whether `s` starts with `prefix` |
| `ends_with(s, suffix)` | Whether `s` ends with `suffix` |

### Version Functions

| Function | Example | Result |
|----------|---------|--------|
| `version_major(v)` | `{{version_major("1.2.3")}}` | `1` |
| `version_minor(v)` | `{{version_minor("1.2.3")}}` | `2` |
| `version_patch(v)` | `{{version_patch("1.2.3")}}` | `3` |
| `bump_major(v)` | `{{bump_major("1.2.3")}}` | `2.0.0` |
| `bump_minor(v)` | `{{bump_minor("1.2.3")}}` | `1.3.0` |
| `bump_patch(v)` | `{{bump_patch("1.2.3")}}` | `1.2.4` |

### Example

```
version := "1.2.3"

task release:
    @echo "Current: {{version}}, next: {{bump_minor(version)}}"
    !git tag v{{bump_minor(version)}}
    deploy version {{uppercase(version)}} to production
```

## Automatic Variables

Every task can reference its own target, dependencies, and files without
repeating them, the way make's automatic variables work:

| Variable | Make equivalent | Value |
|----------|-----------------|-------|
| `{{makethlm_task}}` | `$@` | Name of the running task |
| `{{makethlm_deps}}` | `$^` | All dependency task names, space-separated |
| `{{makethlm_dep}}` | `$<` | First dependency task name |
| `{{makethlm_sources}}` | — | Files matched by `sources`, relative to the working directory |
| `{{makethlm_outputs}}` | — | Files matched by `outputs`, falling back to the declared patterns |
| `{{makethlm_changed}}` | `$?` | Only the sources newer than the oldest output |
| `{{makethlm_file}}` | — | Path to the Promptfile |
| `{{makethlm_dir}}` | — | Directory containing the Promptfile |

```make
task build [sources="src/*.c", outputs="build/app"]:
    !mkdir -p build
    !cc -o {{makethlm_outputs}} {{makethlm_sources}}
```

`makethlm_changed` is the incremental lever — it holds only the sources newer
than the output, so a recipe can act on what actually changed:

```make
task lint [sources="src/**/*.py", outputs=".lint-stamp"]:
    !ruff check {{makethlm_changed}}
    !touch .lint-stamp
```

Paths are rendered relative to the directory the recipe runs in and quoted only
when they need it, so names containing spaces stay safe. The file variables are
empty for a task that declares neither `sources` nor `outputs`.
