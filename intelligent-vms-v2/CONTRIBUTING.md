# Contributing

## Non-negotiable rules

These exist because this is security software. A pull request violating any
of them will be rejected regardless of how useful the feature is.

1. **Never claim something works that has not been verified.** Update
   `docs/requirements/requirements.yaml` with an honest status and real
   evidence. CI validates that every evidence reference names a test or file
   that actually exists.
2. **No placeholder that pretends to succeed.** An unimplemented path raises
   `NotImplementedFeatureError` (HTTP 501) with an explanation. It never
   returns a plausible-looking empty result.
3. **No hardcoded credentials, keys or tokens.** CI greps for them.
4. **Never disable authentication to make development easier.** Use the test
   fixtures.
5. **Every outbound call is time-bounded.** No bare `httpx` client; use
   `app.core.outbound`.
6. **Every camera-supplied address goes through `NetworkGuard`.** No
   exceptions.
7. **Every path built from input goes through `core.paths.resolve_within`.**
8. **Camera credentials and source URLs never enter a DTO.** There is a test
   asserting this; do not weaken it.

## Before opening a pull request

```bash
ruff check packages services tools tests
python -m pytest -q
python tools/generate_feature_matrix.py          # if you touched requirements.yaml
python scripts/export_openapi.py                 # if you changed the API
```

## Commit style

Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`,
`chore:`. Reference the requirement id where one applies:

```
feat(ptz): add absolute positioning (M7-R04)
fix(recording): finalize the current segment before shutdown (M4-R18)
```

## Tests

- **Unit tests** for pure logic, with a fake clock where time matters.
- **Integration tests** through the real FastAPI app and the real migrated
  schema.
- **Pipeline tests** using real FFmpeg. Mark them `@pytest.mark.ffmpeg` and
  skip, with a visible reason, when it is unavailable.

**Do not mock the thing under test.** A recording test that mocks FFmpeg
proves nothing about recording.

A test must fail for exactly one reason, and its name must say what that
reason is. `test_export_refuses_when_source_file_is_missing` is a good name;
`test_export_2` is not.

## Adding a feature

1. Add the requirement to `requirements.yaml` with status
   `not_implemented`.
2. Write a failing test.
3. Implement it.
4. Update the status and the evidence.
5. Regenerate the feature matrix.

## Documentation in code

Comments explain **why**, not what. These are the comments worth writing:

- A non-obvious trade-off (`-ss` after `-i` costs CPU but prevents a silent
  time shift)
- A security property that must not be removed
- A known limitation at the point where someone would otherwise assume it works
- A failure mode the code is specifically defending against

Do not write comments that restate the code.

## Architecture decisions

Anything affecting structure, a dependency or a security property needs an
ADR in `docs/decisions/`, including the **consequences — especially the bad
ones**. A decision record with no downsides listed has not been thought
through and will be sent back.
