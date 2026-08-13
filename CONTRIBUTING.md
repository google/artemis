# How to Contribute

We'd love to accept your patches and contributions to ARTEMIS! There are just a few small guidelines you need to follow.

---

## Contributor License Agreement (CLA)

Contributions to this project must be accompanied by a Contributor License Agreement (CLA). You (or your employer) retain the copyright to your contribution; this simply gives us permission to use and redistribute your contributions as part of the project.

- Head over to <https://cla.developers.google.com/> to check your current agreements on file or to sign a new one.
- If you are an individual contributing original source code, you must sign the **Individual CLA**.
- If you are contributing on behalf of a company or organization, a **Corporate CLA** must be executed by your employer.
- You generally only need to submit a CLA once, so if you have already submitted one for another Google project, you do not need to sign it again.

---

## Community Guidelines

This project adheres to [Google's Open Source Community Guidelines](https://opensource.google/conduct/). By participating, all contributors and maintainers are expected to uphold these principles and maintain a welcoming, inclusive, and professional environment.

---

## Code Reviews

All submissions, including submissions by project maintainers, require review. We use standard GitHub pull requests for this purpose:

1. Consult [GitHub Help](https://docs.github.com/articles/about-pull-requests/) for information on using pull requests.
2. Maintainers will review your changes, offer feedback, and approve when ready.
3. Once approved and all CI checks pass, a project maintainer will merge your PR.

---

## Development & Contribution Workflow

### 1. Setting Up Your Development Environment

- **Prerequisites**:
  - Python **3.12+**
  - Android SDK Platform Tools (`adb` available in `PATH`)
  - [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`

- **Clone & Install**:
  ```bash
  git clone https://github.com/<your-username>/artemis.git
  cd artemis

  # Install all dependencies and setup pre-commit hooks
  make setup
  ```
  *(Alternatively: `uv sync --dev` and `uv run pre-commit install`)*

### 2. Making Changes

- Create a descriptive topic branch off `main`:
  ```bash
  git checkout -b feat/my-new-feature
  # or
  git checkout -b fix/issue-description
  ```

- Follow the project's coding and style conventions:
  - **Absolute Imports**: Always use absolute imports (`from artemis.sdk import Agent`). Relative imports are disallowed.
  - **Typing**: Use comprehensive type annotations on all public functions, classes, and methods.
  - **Testing**: Add or update test cases under `tests/` for any modified or new behavior.

### 3. Local Verification

Before creating a pull request, ensure all linters, type checks, and tests pass:

```bash
# Format code (Ruff)
make format

# Lint code (Ruff)
make lint

# Run static type checker (Pyright)
make typecheck

# Run test suite (Pytest)
make test
```

### 4. Submitting a Pull Request

1. Push your branch to your fork:
   ```bash
   git push origin feat/my-new-feature
   ```
2. Open a Pull Request against the `main` branch of the ARTEMIS repository.
3. Complete the PR template, describing:
   - The motivation behind the change.
   - Any related issue numbers (e.g., `Fixes #42`).
   - How the change was tested (device, emulator, unit tests).
4. Ensure the CLA check automatically passes on your PR.

---

## Reporting Bugs and Feature Requests

- **Bug Reports**: Open an issue describing the bug, including steps to reproduce, device/emulator specifications, Android version, logs, and screenshots if applicable.
- **Feature Requests**: Open an issue detailing the use case, proposed API or workflow, and alternatives considered.
