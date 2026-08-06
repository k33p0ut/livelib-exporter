# Contributing

1. Create a focused branch.
2. Add or update tests for behavior changes.
3. Run:

```bash
ruff check .
mypy src
pytest
```

Do not commit real private profile data, cookies, credentials, or raw exports containing personal notes. Synthetic fixtures are preferred.
