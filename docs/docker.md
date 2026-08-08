# Docker Support

## Docker Blocks

The `docker` block lets you describe a Docker image in natural language. The LLM generates a Dockerfile, and makethlm builds it automatically.

```
docker api-server [tag=latest]:
    A Python 3.11 slim image.
    Install requirements.txt with pip, no cache.
    Copy the app/ directory to /app.
    Set the working directory to /app.
    Expose port 8080.
    Run with gunicorn, 4 workers, binding 0.0.0.0:8080.
```

Running `makethlm api-server` will:

1. Send the description to the LLM with instructions to output a raw Dockerfile.
2. Write the generated Dockerfile to the configured path.
3. Run `docker build` with the specified tag and context.

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `tag` | `latest` | Image tag |
| `context` | `.` | Build context directory |
| `file` | `Dockerfile` | Dockerfile path |

```
docker frontend [tag=v2, context=./client, file=Dockerfile.prod]:
    Node 20 alpine image.
    Run npm ci, then npm run build.
    Serve with nginx on port 80.
```

## As Dependencies

Docker blocks appear in the task list and can be used as dependencies:

```
docker api:
    Python 3.11 slim image. Install requirements.txt.

task deploy: api:
    push the api image to the registry
```
