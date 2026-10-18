# AGENTS.md — Rule-34 Bot

## Project map
- Python Discord bot with `main.py` as the entry point, feature cogs under `cogs/`, persistence helpers in `database.py`, and optional hosting/health behavior in `keep_alive.py`.
- Read `README.md`, `requirements.txt`, `.env.example`, and `render.yaml` before changing startup, commands, persistence, or deployment assumptions.

## Change rules
- Follow the existing Discord.py patterns and keep command handling separate from persistence and external-service calls where the current layout supports it.
- Preserve existing command names, configuration keys, stored data formats, and Render startup behavior unless the task explicitly changes them.
- Validate user input and check permissions before commands that modify server state or access restricted features.
- Handle missing API responses, network errors, Discord rate limits, and deleted/missing resources without crashing the bot.
- Do not add unrelated features, change content policies, or introduce new dependencies as part of a narrow bug fix.

## Security and age-sensitive content
- Keep credentials in environment variables and safe placeholders in `.env.example`. Never log or commit tokens, database secrets, or API keys.
- Respect applicable platform rules, access controls, and age restrictions. Do not expose private user information or bypass safety/access controls.
- Avoid sending raw exception traces or sensitive configuration into Discord messages.

## Validation
- Inspect the actual tests and package versions before choosing commands.
- Run syntax/import checks and focused tests when practical. Do not claim remote services or deployment were verified unless they were actually tested.
- Review the diff for unintended changes to database files, local state, or deployment configuration.