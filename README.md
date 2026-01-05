# Rove

Context extraction for coding agents. Rove gathers relevant context from JIRA, Slack, and GitHub so your AI coding assistants understand what they're building.

## The Problem

Coding agents often work with incomplete context. They see the code but miss the discussions, decisions, and requirements that shaped it. Rove fixes this by collecting context from your team's tools and making it available as markdown files agents can read.

## Installation

```bash
# Clone and install
git clone https://github.com/your-org/rove.git
cd rove
pip install -e .
```

Requires Python 3.11+.

## Quick Start

```bash
# First run creates a config file at .rove/settings.toml
rove

# Make sure to add your credentials to this file before continuing!

# Add your context sources
rove source add jira
rove source add slack
rove source add github

# Gather context for a ticket
rove gather TB-123
```

Context is saved to `.context/{ticket_id}_{keywords}.md`.

## Commands

### Gathering Context

```bash
# Gather context for a ticket
rove gather TB-123

# Use a specific ticket source (overrides default)
rove gather TB-123 --source jira

# Filter by time window
rove gather TB-123 --since "7 days ago"
rove gather TB-123 --since 2024-12-01 --until 2024-12-28
```

### Analyzing Tickets

```bash
# Analyze a ticket and suggest improvements
rove grow TB-123
```

The `grow` command reads the context file (gathering it first if needed), then uses AI to identify gaps and generate questions that need answers. Suggestions are saved to `.context/{ticket_id}.suggestions.md`.

### Finding Context

```bash
# Find the context file for a ticket
rove find TB-123

# Search context files by keyword
rove search "oauth authentication"
```

### Managing Sources

```bash
# List available plugins
rove source plugins

# Add a source
rove source add jira
rove source add slack
rove source add github

# Remove a source
rove source remove slack

# List configured sources and their status
rove source list

# Set the default ticket source
rove source default jira
```

### API Server

```bash
# Start the API server (Unix socket at .rove/api.sock)
rove server start

# Stop the server
rove server stop

# Check server status
rove server status
```

### Task Status

```bash
# Show recent context-building tasks
rove status
```

## Configuration

Settings are stored in `.rove/settings.toml`. Created automatically on first run.

### AI Provider

Rove uses an OpenAI-compatible API for AI-assisted search and analysis:

```toml
[ai]
api_base = "https://api.openai.com/v1"    # or OpenRouter, Ollama, etc.
api_key = "sk-..."
model = "gpt-4o-mini"
max_hops = 3                               # Search depth for following references
```

### Source Authentication

Each source supports either OAuth or API tokens:

**JIRA** - API token (email + token from [Atlassian API tokens](https://id.atlassian.com/manage-profile/security/api-tokens)) or OAuth

**Slack** - OAuth (requires [Slack app](https://api.slack.com/apps) with search:read, channels:read, channels:history scopes)

**GitHub** - Personal Access Token ([create here](https://github.com/settings/tokens)) or OAuth

## How It Works

1. **Fetch the primary ticket** from your default source (JIRA, Linear, etc.)
2. **Extract keywords** and references from the ticket content
3. **Search other sources** (Slack, GitHub) for mentions of the ticket ID and keywords
4. **Follow references** - if a Slack message mentions "see PR #847", fetch that PR too (up to 3 hops)
5. **Aggregate everything** into a markdown document grouped by topic

## Agent Integration

Agents can find context via CLI:

```bash
# Find context file for a ticket
rove find TB-123
# → TB-123_oauth_authentication.md

# Search by keywords
rove search "payment integration"
# → TB-456_payment_stripe_webhook.md
```

Typical agent workflow:
1. Agent receives task mentioning TB-123
2. Agent runs `rove find TB-123`
3. Agent reads the returned context file
4. Agent now has full context for the ticket

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check src/
```

## License

MIT

