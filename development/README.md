# shell-runner

Sandboxed shell execution MCP server with telemetry and adaptive allowlist learning.

## Overview

`shell-runner` is an HTTP MCP server that provides sandboxed shell execution with:
- Command normalization for consistent telemetry
- Adaptive allowlist learning from execution patterns
- Command classification against known-safe patterns

## Development

This project uses [pixi](https://pixi.sh) for environment management.

```bash
# Install environment
pixi install

# Run tests
pixi run test

# Run all quality checks
pixi run check-all
```

## Available Tasks

| Task | Description |
|------|-------------|
| `test` | Run tests (excludes live and debug) |
| `test-unit` | Run unit tests only |
| `test-cov` | Run tests with coverage report |
| `lint` | Run ruff linter with auto-fix |
| `format` | Run black formatter |
| `type-check` | Run mypy type checker |
| `quality` | Run lint + type-check |
| `check-all` | Run test + quality |
| `build` | Build distribution package |
| `http-server` | Start the HTTP MCP server |

## License

MIT License - see [LICENSE](LICENSE) for details.
