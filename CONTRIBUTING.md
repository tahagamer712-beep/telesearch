# Contributing to TeleSearch

Thank you for your interest in contributing! 🎉

## How to Contribute

### Reporting Bugs

- Use GitHub Issues
- Include: Python version, OS, steps to reproduce, expected vs actual behavior
- Include relevant log excerpts from `logs/app.log`

### Suggesting Features

- Open an issue with the `enhancement` label
- Describe the use case and proposed solution

### Code Contributions

1. Fork the repo
2. Create a branch: `git checkout -b feature/your-feature`
3. Make your changes
4. **Important**: Never commit `.env`, `data/`, `database/`, or any real data files
5. Test your changes: `python -m pytest tests/` (if available)
6. Commit: `git commit -m "Add your feature"`
7. Push and open a Pull Request

### Code Style

- Follow PEP 8
- Use type hints where practical
- Add docstrings for new functions/classes
- Keep functions focused and small

### Security

- **NEVER** commit API keys, tokens, or real user data
- All SQL must use parameterized queries
- New features handling user input must validate/sanitize

## Questions?

Open an issue — we're happy to help!
