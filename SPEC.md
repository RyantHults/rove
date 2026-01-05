# Rove

## Vision and Strategy

### Project Overview

A cli-based service that connects to multiple sources of data to extract context for agents to use

### Problem Statement

Coding agents often don't fully understand the context of what they're building. This app will be able to search through multiple sources of context (slack, JIRA, meeting transcripts, etc) to be able to build a full picture of what a particular ticket or task covers.

### Target Audience

Developers using agentic AI assistants 

### Goals and Objectives

The MVP should be a working demo that can authenticate with slack, and JIRA, and generates a context md file for a given ticket number

## Product Definition

### Features and Requirements

- MVP: Users can pass in a list of JIRA tickets and the app will search through all available context sources for information about those tickets. All of this context will be saved in an .md file for each ticket. 
- FUTURE: have the agent look for holes or gaps in the plan/context that might help make the picture clearer.
- MVP: user can add multiple external accounts as sources of context.
- MVP: external connections are handled through a modular plugin system
- MVP: there is a background service that runs every hour or so and updates any existing context documents with any new information
- MVP: user can kick off context update manually
- MVP: the app will expose an API that will enable agents to query the database for which context files are relevant, given a JIRA ticket number or ticket title
- MVP: create an agent skill that shows the agent how to call the API
- FUTURE: create a web interface that allows you to see what context is being brought in when given a search term
- MVP: tasks should run in the background, with a command to see the current status of the tasks
- FUTURE: add our own agent and power it with the context we have to help it analyze and give feedback on PRs

### User Stories

As a developer, I want my coding agents to have as full of an understanding of our task as possible. As a developer, when I start working with an agentic agent, then I want the agent to already have the necessary context need to complete our task.

### Non-Functional Requirements

app must require the user to log into their 3rd part accounts as infrequently as possible. Ideally, once the user logs in they never have to re-authenticate again 

settings are stored in `.rove/settings.toml` (in the current working directory)

### Out of Scope

- Web GUI
- suggestions to help fill in gaps from the context

## Design

### UI/UX Design

MVP will be all commandline. should work somewhat like:
```
# kick off rove task for ticket (uses default ticket source)
rove --ticket TB-123

# specify a different ticket source (overrides default)
rove --ticket LIN-456 --source linear

# see status of all context building tasks
rove --status

# list all compatible sources that can be connected
rove --source-plugins

# add a new source (JIRA in this case)
rove --add-source JIRA 

# set default ticket source
rove --set-default-source jira

# find context file for a ticket (for agent use)
rove --find TB-123

# search context files by keyword
rove --search "oauth authentication"
```

### Information Architecture

Simple commandline interface. main interactions are:
- starting/stopping the background services
- managing sources of context (add/remove)
- building/updating context based on a ticket number or description
- API access by the coding agent to find the appropriate md context file  

## Technical Architecture

### Tech Stack

Python for backend, maybe fastapi for api management, SQLite for local storage.

### System Architecture

async background tasks handle the bulk of the context-building work. everything else is either an API call or a command being manully run by the user.

### Data Model

#### Context Files

Context is stored as markdown files in the project directory:

```
{project_root}/.context/{TICKET_ID}_{keywords}.md
```

**Filename format:** `{TICKET_ID}_{top_3-5_keywords_joined_by_underscores}.md`

Examples:
- `TB-123_oauth_authentication_user_login.md`
- `TB-456_payment_stripe_webhook_integration.md`
- `TB-789_database_migration_postgres_indexes.md`

#### SQLite Index

The database stores metadata for fast lookup and discovery:

```sql
CREATE TABLE context_files (
    id INTEGER PRIMARY KEY,
    ticket_id TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    keywords TEXT NOT NULL,  -- JSON array of keywords
    last_updated TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ticket_id ON context_files(ticket_id);
CREATE INDEX idx_filename ON context_files(filename);
```

#### Deduplication Strategy

A "trust but verify" approach prevents processing the same data repeatedly:

**Layer 1: Per-Source Fetch Tracking**

Tracks the last fetch timestamp per source per context file (not per item):

```sql
CREATE TABLE fetch_history (
    context_file_id INTEGER NOT NULL,
    source TEXT NOT NULL,           -- "jira", "slack", "github"
    last_fetched TIMESTAMP NOT NULL,
    PRIMARY KEY (context_file_id, source),
    FOREIGN KEY (context_file_id) REFERENCES context_files(id)
);
```

When refreshing a context file, use each API's "since" parameter to only fetch new items:
- JIRA: `updated >= 'last_fetched'`
- Slack: `oldest=last_fetched_unix`
- GitHub: `since=last_fetched_iso`

This keeps the database small (3 rows per ticket instead of hundreds).

**Layer 2: AI Semantic Deduplication**

When the SearchAgent finds items to add:
1. Read the existing context file
2. AI determines if this information is already present (even if worded differently)
3. If duplicate: skip
4. If new: AI decides where to insert it in the document structure

This handles cases where:
- The same information appears in Slack and JIRA comments
- Someone quotes or rephrases earlier discussion
- Related but not identical content should be merged vs. kept separate

#### Agent Discovery

Agents can find context files via CLI:

```bash
# Find context file for a ticket
rove --find TB-123
# outputs: /home/user/projects/backend-api/.context/TB-123_oauth_authentication_flow.md

# Or search by keyword
rove --search "oauth authentication"
# outputs matching context files
```

### Search Strategy

Context building uses a multi-phase AI-assisted search approach.

#### Search Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Context Builder                             │
├─────────────────────────────────────────────────────────────────┤
│  1. Fetch primary ticket from JIRA                              │
│  2. Extract searchable fields (title, description, comments)     │
│  3. Phase 1: Explicit search (ticket ID pattern across sources) │
│  4. Phase 2: AI-guided keyword extraction & expanded search      │
│  5. Phase 3: AI filters results for relevance                   │
│  6. Aggregate into context document                              │
└─────────────────────────────────────────────────────────────────┘
```

#### SearchAgent Class

The `SearchAgent` is a separate component that handles AI-assisted search expansion:

- Extracts keywords from primary ticket content
- Identifies references to other tickets, PRs, or discussions
- Determines relevance of search results
- Guides multi-hop exploration (up to 3 hops)

#### Primary Ticket Source

Users configure a **default ticket source** in settings. This is the authoritative source for ticket details:

```toml
# settings.conf
[sources]
default_ticket_source = "jira"   # Where to fetch primary ticket details
```

The primary source provides the "seed" for AI-guided search:
- Full ticket title, description, comments, labels
- Keywords for expanded search
- Related ticket references

Other connected sources (Slack, GitHub, etc.) are searched for references to the ticket.

The `--source` CLI flag overrides the default when needed:
```bash
rove --ticket LIN-456 --source linear
```

#### Multi-Hop Search (Max 3 Hops)

```
Hop 0 (Start): User requests context for TB-123
       │
       ▼
Hop 1: Fetch TB-123 from PRIMARY SOURCE (e.g., JIRA)
       → Extract keywords: "OAuth2", "authentication", "enterprise"
       → Search OTHER sources (Slack, GitHub) for "TB-123" and keywords
       → Found: Slack thread mentioning "see PR #847 for implementation"
       │
       ▼
Hop 2: Fetch PR #847 from GitHub
       → PR description says "Implements TB-123, also related to TB-456"
       → Found reference to TB-456
       │
       ▼
Hop 3: Fetch TB-456 from JIRA
       → Get title, description, comments
       → Add to context
       │
       ▼
Done:  Aggregate all findings into context document
```

#### Time Window Filtering

Time windows can be specified via CLI flags:

```bash
# Default: no time restriction
rove --ticket TB-123

# With absolute dates
rove --ticket TB-123 --since 2024-12-01 --until 2024-12-28

# With relative time
rove --ticket TB-123 --since "30 days ago"
```

#### Configuration (settings.toml)

Settings are stored in `.rove/settings.toml` (in the current working directory).

**Precedence** (highest to lowest):
1. CLI flags (`--since "7 days ago"`)
2. Config file (`.rove/settings.toml`)
3. Built-in defaults

```toml
# .rove/settings.toml

[sources]
# Primary ticket source - where to fetch full ticket details from
default_ticket_source = "jira"

[scheduler]
refresh_interval = "6h"          # How often to refresh context files
retry_attempts = 3               # Retries on failure
retry_delay = "30m"              # Delay between retries
staleness_threshold = "7d"       # Stop refreshing tickets older than this

[ai]
# OpenAI-compatible endpoint (works with OpenAI, OpenRouter, Ollama, etc.)
api_base = "https://api.openai.com/v1"    # or "http://localhost:11434/v1" for Ollama
api_key = "sk-..."
model = "gpt-4o-mini"                       # or "llama3", "claude-3-haiku", etc.
max_hops = 3                                # Maximum search depth

[credentials]
# Credential storage backend
backend = "auto"  # "auto", "keychain", "encrypted_file"

# Per-source overrides (optional - plugins provide sane defaults)
[sources.jira]
rate_limit = 100                 # requests per minute
page_size = 50                   # items per API call

[sources.slack]
rate_limit = 50
page_size = 100

[sources.github]
rate_limit = 60
page_size = 100
```

#### Plugin Configuration

Plugins define sensible defaults; users can override in settings.toml:

```python
# plugins/jira/client.py
class JiraContextClient:
    DEFAULT_CONFIG = {
        "rate_limit": 100,           # requests per minute
        "page_size": 50,             # items per API call
        "token_refresh_buffer": 300, # refresh 5 min before expiry
    }
    
    def __init__(self, config: dict):
        # Merge user config over defaults
        self.config = {**self.DEFAULT_CONFIG, **config}
```

This allows:
- Zero-config for most users (defaults just work)
- Power users can tune per-source settings
- Plugin authors set appropriate defaults for each API

### Credential Storage

OAuth tokens from 3rd party connections are stored securely using the Python `keyring` library.

#### What Gets Stored

| Token Type | Lifespan | Purpose |
|------------|----------|---------|
| Access Token | Short (1 hour typical) | Used for API calls |
| Refresh Token | Long (weeks/indefinite) | Used to get new access tokens |

#### Storage Backends

The `keyring` library automatically selects the appropriate secure storage:

| OS | Backend Used |
|----|--------------|
| macOS | Keychain Services |
| Windows | Credential Manager (DPAPI) |
| Linux | Secret Service API (GNOME Keyring, KWallet) |
| Fallback | Encrypted file (`.rove/credentials.enc`) |

This follows industry standards used by `gh` (GitHub CLI), `gcloud`, AWS CLI, etc.

#### Implementation

```python
import keyring

# Store tokens (called by plugin's authenticate())
keyring.set_password("rove-jira", "access_token", "eyJ...")
keyring.set_password("rove-jira", "refresh_token", "xoxr-...")

# Retrieve tokens (called by plugin's search())
access_token = keyring.get_password("rove-jira", "access_token")

# Delete tokens (called by plugin's disconnect())
keyring.delete_password("rove-jira", "access_token")
keyring.delete_password("rove-jira", "refresh_token")
```

#### Token Refresh

Each plugin handles token refresh internally:
1. Before API calls, check if access token is expired
2. If expired, use refresh token to obtain new access token
3. Store new access token via keyring
4. If refresh fails, prompt user to re-authenticate

### API Design

The API allows agents to discover and search context files.

#### Server Architecture

- **Transport**: Unix socket at `.rove/api.sock` (in the current working directory)
- **Security**: Socket permissions (0600) restrict access to the owning user
- **Lifecycle**: Always-on daemon, started/stopped via CLI
- **Platform**: Linux and macOS (MVP)

#### Starting/Stopping the Server

```bash
# Start the API server (runs as daemon)
rove --start-server

# Stop the API server
rove --stop-server

# Check if server is running
rove --server-status
```

#### Endpoints

**GET /find/{ticket_id}**
Find the context file for a specific ticket.

```bash
curl --unix-socket .rove/api.sock http://localhost/find/TB-123
```

Response:
```json
{
  "ticket_id": "TB-123",
  "filename": "TB-123_oauth_authentication.md",
  "keywords": ["oauth", "authentication", "enterprise"],
  "last_updated": "2024-12-22T10:30:00Z"
}
```

**GET /search?q={keywords}**
Search context files by keyword.

```bash
curl --unix-socket .rove/api.sock "http://localhost/search?q=oauth+authentication"
```

Response:
```json
{
  "query": "oauth authentication",
  "results": [
    {
      "ticket_id": "TB-123",
      "filename": "TB-123_oauth_authentication.md",
      "keywords": ["oauth", "authentication", "enterprise"],
      "score": 0.95
    },
    {
      "ticket_id": "TB-456",
      "filename": "TB-456_sso_integration.md",
      "keywords": ["sso", "authentication", "okta"],
      "score": 0.72
    }
  ]
}
```

**GET /health**
Health check endpoint.

```bash
curl --unix-socket .rove/api.sock http://localhost/health
```

Response:
```json
{
  "status": "ok",
  "version": "1.0.0"
}
```

#### Agent Integration

Agents integrate via CLI commands. This approach is simple, universal, and works with any agent that can execute shell commands.

**Available commands for agents:**

```bash
# Find context file for a ticket
rove --api-find TB-123
# Output: TB-123_oauth_authentication.md

# Search by keywords
rove --api-search "oauth authentication"
# Output: TB-123_oauth_authentication.md, TB-456_sso_integration.md

# Check if context exists for a ticket
rove --api-find TB-123 && echo "found" || echo "not found"
```

**Typical agent workflow:**

1. Agent receives a task mentioning ticket TB-123
2. Agent runs: `rove --api-find TB-123`
3. Agent gets filename: `TB-123_oauth_authentication.md`
4. Agent locates and reads the file:
   ```bash
   find . -name "TB-123_oauth_authentication.md" -exec cat {} \;
   ```
5. Agent now has full context for the ticket

**Why CLI over protocols like MCP:**
- Works with any agent (Cursor, Aider, Claude, custom agents)
- No protocol versioning issues
- Simple to debug and test
- No additional server/handshake required

### Third-Party Integrations

Modular plugin-based system for connecting to third-party services. Each provider is contained in its own plugin module.

#### MVP Plugin Set
- JIRA
- Slack  
- GitHub

#### ContextClient Interface

Each plugin must implement a `ContextClient` that provides a standardized interface for authentication and search:

```python
from typing import Protocol
from datetime import datetime
from dataclasses import dataclass

@dataclass
class ContextItem:
    """Standardized format for context returned by any plugin."""
    source: str              # "jira", "slack", "github"
    item_type: str           # "ticket", "message", "pr", "issue", "comment"
    title: str               # Human-readable title
    content: str             # The actual context text
    url: str                 # Link to original item
    timestamp: datetime      # When this was created/updated
    author: str              # Who created this item
    metadata: dict           # Source-specific extra data (channel, labels, etc.)


@dataclass 
class SearchableField:
    """Describes a field that can be searched within a source."""
    name: str           # "ticket_title", "comments", "channel_messages"
    field_type: str     # "text", "id_reference", "keyword"
    description: str    # Human-readable description for AI context


class ContextClient(Protocol):
    """Interface all plugins must implement."""
    
    def source_name(self) -> str:
        """Return human-readable name (e.g., 'JIRA', 'Slack')."""
        ...
    
    def get_config_schema(self) -> dict:
        """Return JSON schema of required configuration."""
        ...
    
    def get_searchable_fields(self) -> list[SearchableField]:
        """Return list of fields this source can search."""
        ...
    
    def authenticate(self, credentials: dict) -> bool:
        """Authenticate with stored or provided credentials. Returns success."""
        ...
    
    def is_authenticated(self) -> bool:
        """Check if currently authenticated with valid credentials."""
        ...
    
    def test_connection(self) -> bool:
        """Verify the connection is working."""
        ...
    
    def search(
        self, 
        query: str, 
        since: datetime = None, 
        until: datetime = None,
        fields: list[str] = None,  # Which fields to search
        **kwargs
    ) -> list[ContextItem]:
        """Search for context matching query. Returns list of context items."""
        ...
    
    def get_item_details(self, item_id: str) -> ContextItem:
        """Fetch full details of a specific item (for AI to analyze)."""
        ...
    
    def disconnect(self) -> None:
        """Clear stored credentials."""
        ...
```

#### Searchable Fields by Plugin

| Plugin | Searchable Fields |
|--------|-------------------|
| JIRA | ticket_id, ticket_title, ticket_description, comments, related_tickets, labels |
| Slack | message_content, channel_name, thread_replies |
| GitHub | pr_title, pr_description, pr_comments, commit_messages, issue_title, issue_body |

#### Plugin Directory Structure

```
src/
├── plugins/
│   ├── __init__.py          # Plugin discovery logic
│   ├── base.py               # ContextClient Protocol & ContextItem definition
│   ├── jira/
│   │   ├── __init__.py       # Exports create_client(), PLUGIN_NAME, PLUGIN_VERSION
│   │   ├── client.py         # JiraContextClient implementation
│   │   └── auth.py           # JIRA-specific OAuth handling
│   ├── slack/
│   │   ├── __init__.py
│   │   ├── client.py
│   │   └── auth.py
│   └── github/
│       ├── __init__.py
│       ├── client.py
│       └── auth.py
```

#### Plugin Registration

Each plugin's `__init__.py` exports a standard interface:

```python
# src/plugins/jira/__init__.py
from .client import JiraContextClient

PLUGIN_NAME = "jira"
PLUGIN_VERSION = "1.0.0"

def create_client(config: dict) -> JiraContextClient:
    return JiraContextClient(config)
```

#### Plugin Discovery

Plugins are auto-discovered by scanning the plugins directory:

```python
# src/plugins/__init__.py
import importlib
from pathlib import Path
from .base import ContextClient

def discover_plugins() -> dict[str, callable]:
    """Discover all plugins in the plugins directory."""
    plugins = {}
    plugin_dir = Path(__file__).parent
    
    for item in plugin_dir.iterdir():
        if item.is_dir() and not item.name.startswith('_'):
            try:
                module = importlib.import_module(f".{item.name}", package="rove.plugins")
                if hasattr(module, 'create_client') and hasattr(module, 'PLUGIN_NAME'):
                    plugins[module.PLUGIN_NAME] = module.create_client
            except ImportError:
                continue  # Skip invalid plugins
    
    return plugins
```

#### Context Aggregation

The `search()` method returns individual `ContextItem` objects with full source attribution. A separate context builder component aggregates items from multiple plugins and generates the final markdown document.

Generated context files follow this structure:
- Grouped by topic/relevance (not by source)
- Inline attribution after each item (author, date, source)
- Reference-style links at the bottom (keeps content clean for agents)
- Sources table listing all sources consulted

Example output format:

```markdown
# Context: TB-123 - User Authentication Flow

## Related Discussions

### Initial Requirements Discussion
> We need to support OAuth2 and API key auth for enterprise customers... [1]

— *Sarah Chen in #backend-team, Dec 15, 2024*

### Technical Decision  
> Decided to use python-jose for JWT handling... [2]

— *Comment on TB-123 by Mike Torres, Dec 18, 2024*

## Related Code

### PR #847: Add OAuth2 provider base class [3]
Implements the foundation for OAuth2 authentication...

— *Opened by Sarah Chen, Dec 20, 2024*

---

## Sources Consulted
| Source | Items Found | Last Updated |
|--------|-------------|--------------|
| JIRA | 3 | 2024-12-22 |
| Slack | 7 | 2024-12-22 |
| GitHub | 2 | 2024-12-22 |

## References
[1]: https://myworkspace.slack.com/archives/C123/p456 "Slack: #backend-team"
[2]: https://mycompany.atlassian.net/browse/TB-123?focusedCommentId=789 "JIRA: TB-123 comment"
[3]: https://github.com/myorg/backend/pull/847 "GitHub: PR #847"
``` 

## Infrastructure

### Deployment and Hosting

for MVP, this app will run locally on the developers machine

### Monitoring and Logging

We should have separate error/debug and performace logs. performances logs should be one-line outputs of all performance metrics for a task run.

### Backup and Recovery

MVP: all data lives locally on user's machine. no backups.

## Quality Assurance

### Testing Strategy

Unit tests for business logic with pytest, integration tests for API calls. 

### Code Quality Standards

TBD

### Error Handling

Display user-friendly error messages. TBD

### Performance Optimization

TBD

