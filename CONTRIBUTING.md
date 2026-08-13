# Contributing

Thank you for your interest in contributing to this project! We value all types of contributions—whether it's bug reports, feature requests, documentation improvements, or pull requests.

---

## How to Contribute

1. **Fork the Repository**  
   - Navigate to the project’s GitHub page and click the "Fork" button.  
   - Clone your fork locally:
     ```bash
     git clone https://github.com/alokemajumder/S3-Glacier-Bulk-Folder-Restore.git
     cd S3-Glacier-Bulk-Folder-Restore
     ```

2. **Create a Feature Branch**  
   - It’s best practice to keep your main branch clean. Create a new branch for each feature or bug fix:
     ```bash
     git checkout -b my-feature
     ```

3. **Make Your Changes**  
   - Add or modify code, documentation, or tests.
   - Ensure your changes follow the project’s coding style, and add comments where necessary.

4. **Commit and Push**  
   - Commit your changes with a descriptive message:
     ```bash
     git commit -m "Add feature X to handle Y"
     git push origin my-feature
     ```

5. **Open a Pull Request**  
   - Go to your fork on GitHub and click on the **"Compare & pull request"** button.  
   - Fill out the pull request template (if available), providing as much detail as possible:
     - What problem does this PR solve?
     - How does it solve the problem?
     - Any additional notes or references?

6. **Wait for Review**  
   - A project maintainer or other community members may review your pull request.  
   - Be ready to address any feedback or requested changes:
     - Update your pull request by pushing additional commits to your branch.
     - Reply to review comments if you have questions or clarifications.

7. **Merge**  
   - Once your pull request is approved and passes all checks, it will be merged into the main branch.

---

## Issues and Bug Reports

- **Check existing issues** first to see if the problem is already reported or being worked on.  
- If not, open a new issue describing:
  - The behavior you’re seeing (or a new feature idea).
  - Steps to reproduce the issue (if it’s a bug).
  - Relevant logs or screenshots (if applicable).

---

## Coding Standards

- **Readability & Comments**: Keep the code readable and well-commented, especially in the logic handling the S3 restore process.  
- **PEP 8**: Follow [PEP 8](https://peps.python.org/pep-0008/) style guidelines for Python where practical.

---

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Testing

The suite runs entirely offline -- no AWS account, no network, no credentials.

```bash
pytest                                          # all tests
pytest --cov=s3_glacier_restore --cov-report=term-missing
pytest tests/test_engine.py -k intelligent -v   # one area
```

Two layers, and new code should land in both where relevant:

- `tests/conftest.py` provides `FakeS3`, an in-memory client with realistic
  pagination. Use it for behaviour: which objects get restored, what the
  counters say, how errors are classified.
- `tests/test_integration.py` drives a **real** boto3 client through
  botocore's `Stubber`, which validates every request against the actual S3
  service model. Anything that changes the shape of an API call belongs here
  too -- it is the only layer that catches a misspelled parameter.

Please add a regression test for each bug fix, and name it after the behaviour
rather than the function (`test_glacier_ir_is_not_restorable`, not
`test_classify_3`).

## Linting and formatting

CI runs both, so run them before pushing:

```bash
ruff check .          # add --fix to apply what it can
ruff format .
```

## Things worth knowing before changing the engine

- **Every object costs money.** A bug that issues a redundant `RestoreObject`
  is a billing bug, not just a correctness one. When in doubt, skip and count.
- **Never let a worker raise.** `RestoreEngine._process` must always return an
  outcome; one uncaught exception should not end a six-hour run.
- **Keep it streaming.** Nothing should accumulate per-object state that grows
  with bucket size. Assume fifty million keys.
- **`--dry-run` must stay honest.** It runs the same classification path as a
  live run; the only difference is the final call.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).

Thanks again for helping make this project better!
